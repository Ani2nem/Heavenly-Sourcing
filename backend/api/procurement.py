import uuid
from datetime import datetime, date
from typing import Dict
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlmodel import Session, select
from pydantic import BaseModel

from database import get_session
from models import (
    RestaurantProfile, Dish, Ingredient, Recipe, RecipeIngredient,
    ProcurementCycle, CycleDishForecast, CycleIngredientsNeeded,
    Distributor, DistributorQuote, DistributorQuoteItem, Notification,
)

router = APIRouter(tags=["procurement"])


class InitiateCycleRequest(BaseModel):
    dish_forecasts: Dict[str, int]
    preferred_delivery_window: str = "Morning"


class ApproveCycleRequest(BaseModel):
    selected_distributor_id: str


def _compute_ingredients_needed(
    dish_forecasts: Dict[str, int],
    session: Session,
) -> Dict[str, float]:
    """Aggregate culinary qty needed across all forecasted dishes."""
    totals: Dict[str, float] = {}
    for dish_id_str, qty in dish_forecasts.items():
        try:
            dish_id = uuid.UUID(dish_id_str)
        except ValueError:
            continue
        recipe = session.exec(select(Recipe).where(Recipe.dish_id == dish_id)).first()
        if not recipe:
            continue
        ris = session.exec(
            select(RecipeIngredient).where(RecipeIngredient.recipe_id == recipe.id)
        ).all()
        for ri in ris:
            key = str(ri.ingredient_id)
            totals[key] = totals.get(key, 0.0) + (ri.quantity_required or 0.0) * qty
    return totals


def _background_procurement(cycle_id: str, profile_id: str, preferred_window: str):
    """Runs after cycle initiation: discover distributors, fetch USDA prices, dispatch RFPs."""
    from database import engine
    from sqlmodel import Session as DBSession

    with DBSession(engine) as session:
        cycle = session.get(ProcurementCycle, uuid.UUID(cycle_id))
        profile = session.get(RestaurantProfile, uuid.UUID(profile_id))
        if not cycle or not profile:
            return

        # 1. Distributor discovery
        try:
            from services.places_discovery import discover_distributors
            discover_distributors(profile, session)
        except Exception as e:
            print(f"[procurement] distributor discovery failed: {e}")

        # 2. Dispatch RFP emails to each distributor
        distributors = session.exec(
            select(Distributor).where(Distributor.restaurant_profile_id == profile.id)
        ).all()

        needed = session.exec(
            select(CycleIngredientsNeeded).where(
                CycleIngredientsNeeded.procurement_cycle_id == cycle.id
            )
        ).all()

        ingredient_list = []
        for cin in needed:
            ing = session.get(Ingredient, cin.ingredient_id)
            if ing:
                ingredient_list.append({
                    "name": ing.name,
                    "qty": cin.purchasing_qty_needed,
                    "unit": ing.culinary_unit or "unit",
                    "shelf_life_days": ing.shelf_life_days or 99,
                })

        for dist in distributors:
            # Create a quote record
            quote = DistributorQuote(
                procurement_cycle_id=cycle.id,
                distributor_id=dist.id,
                quote_status="PENDING",
            )
            session.add(quote)
            session.flush()

            try:
                from services.email_daemon import send_rfp_email
                import asyncio
                asyncio.run(send_rfp_email(
                    to_email=dist.demo_routing_email or dist.name.lower().replace(" ", "_") + f"+demo@{profile.email.split('@')[1]}",
                    distributor_name=dist.name,
                    ingredient_list=ingredient_list,
                    preferred_window=preferred_window,
                    cycle_id=cycle_id,
                    quote_id=str(quote.id),
                ))
            except Exception as e:
                print(f"[procurement] RFP email to {dist.name} failed: {e}")

        session.commit()


@router.post("/procurement/cycle/initiate", status_code=201)
def initiate_cycle(
    payload: InitiateCycleRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    profile = session.exec(select(RestaurantProfile)).first()
    if not profile:
        raise HTTPException(status_code=400, detail="Create a profile first")

    # Compute ingredient totals
    ingredient_totals = _compute_ingredients_needed(payload.dish_forecasts, session)

    try:
        cycle = ProcurementCycle(
            restaurant_profile_id=profile.id,
            status="COLLECTING_QUOTES",
            week_start_date=date.today(),
            preferred_delivery_window=payload.preferred_delivery_window,
        )
        session.add(cycle)
        session.flush()

        for dish_id_str, qty in payload.dish_forecasts.items():
            try:
                dish_id = uuid.UUID(dish_id_str)
            except ValueError:
                continue
            forecast = CycleDishForecast(
                procurement_cycle_id=cycle.id,
                dish_id=dish_id,
                forecasted_quantity=qty,
            )
            session.add(forecast)

        for ing_id_str, culinary_qty in ingredient_totals.items():
            cin = CycleIngredientsNeeded(
                procurement_cycle_id=cycle.id,
                ingredient_id=uuid.UUID(ing_id_str),
                culinary_qty_needed=culinary_qty,
                purchasing_qty_needed=culinary_qty,  # 1:1 default; split logic applied in scoring
            )
            session.add(cin)

        session.commit()
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create cycle: {str(e)}")

    background_tasks.add_task(
        _background_procurement,
        str(cycle.id),
        str(profile.id),
        payload.preferred_delivery_window,
    )

    return {"cycle_id": str(cycle.id), "status": "COLLECTING_QUOTES"}


@router.get("/procurement/cycle/active")
def get_active_cycle(session: Session = Depends(get_session)):
    profile = session.exec(select(RestaurantProfile)).first()
    if not profile:
        return None

    cycle = session.exec(
        select(ProcurementCycle)
        .where(ProcurementCycle.restaurant_profile_id == profile.id)
        .where(ProcurementCycle.status != "COMPLETED")
        .order_by(ProcurementCycle.created_at.desc())
    ).first()

    if not cycle:
        return None

    quotes_raw = session.exec(
        select(DistributorQuote).where(DistributorQuote.procurement_cycle_id == cycle.id)
    ).all()

    quotes = []
    for q in quotes_raw:
        dist = session.get(Distributor, q.distributor_id)
        items_raw = session.exec(
            select(DistributorQuoteItem).where(DistributorQuoteItem.distributor_quote_id == q.id)
        ).all()
        items = []
        for item in items_raw:
            ing = session.get(Ingredient, item.ingredient_id)
            items.append({
                "ingredient_id": str(item.ingredient_id),
                "ingredient_name": ing.name if ing else "",
                "quoted_price_per_unit": item.quoted_price_per_unit,
            })
        quotes.append({
            "quote_id": str(q.id),
            "distributor_id": str(q.distributor_id),
            "distributor_name": dist.name if dist else "",
            "quote_status": q.quote_status,
            "total_quoted_price": q.total_quoted_price,
            "score": q.score,
            "recommendation_text": q.recommendation_text,
            "received_at": q.received_at.isoformat() if q.received_at else None,
            "items": items,
        })

    return {
        "cycle_id": str(cycle.id),
        "status": cycle.status,
        "preferred_delivery_window": cycle.preferred_delivery_window,
        "week_start_date": cycle.week_start_date.isoformat() if cycle.week_start_date else None,
        "quotes": quotes,
    }


@router.post("/procurement/quotes/{quote_id}/ping")
def ping_quote(quote_id: str, session: Session = Depends(get_session)):
    try:
        qid = uuid.UUID(quote_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid quote_id")

    quote = session.get(DistributorQuote, qid)
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")

    dist = session.get(Distributor, quote.distributor_id)
    profile = session.exec(select(RestaurantProfile)).first()

    try:
        from services.email_daemon import send_followup_email
        import asyncio
        asyncio.run(send_followup_email(
            to_email=dist.demo_routing_email if dist else "",
            distributor_name=dist.name if dist else "Vendor",
            quote_id=quote_id,
        ))
    except Exception as e:
        print(f"[ping] follow-up email failed: {e}")

    quote.quote_status = "FOLLOW_UP_SENT"
    session.add(quote)
    session.commit()

    notif = Notification(
        title="Follow-up Sent",
        message=f"Follow-up email dispatched to {dist.name if dist else 'vendor'}.",
    )
    session.add(notif)
    session.commit()

    return {"quote_status": "FOLLOW_UP_SENT"}


@router.post("/procurement/cycle/active/approve")
def approve_cycle(payload: ApproveCycleRequest, session: Session = Depends(get_session)):
    profile = session.exec(select(RestaurantProfile)).first()
    if not profile:
        raise HTTPException(status_code=400, detail="No profile found")

    cycle = session.exec(
        select(ProcurementCycle)
        .where(ProcurementCycle.restaurant_profile_id == profile.id)
        .where(ProcurementCycle.status != "COMPLETED")
        .order_by(ProcurementCycle.created_at.desc())
    ).first()
    if not cycle:
        raise HTTPException(status_code=404, detail="No active cycle")

    try:
        dist_id = uuid.UUID(payload.selected_distributor_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid distributor_id")

    dist = session.get(Distributor, dist_id)
    if not dist:
        raise HTTPException(status_code=404, detail="Distributor not found")

    quote = session.exec(
        select(DistributorQuote)
        .where(DistributorQuote.procurement_cycle_id == cycle.id)
        .where(DistributorQuote.distributor_id == dist_id)
    ).first()

    # Build PO payload
    items_raw = session.exec(
        select(DistributorQuoteItem).where(DistributorQuoteItem.distributor_quote_id == quote.id)
    ).all() if quote else []

    po_items = []
    for item in items_raw:
        ing = session.get(Ingredient, item.ingredient_id)
        po_items.append({
            "ingredient": ing.name if ing else str(item.ingredient_id),
            "unit_price": item.quoted_price_per_unit,
        })

    po_payload = {
        "distributor": dist.name,
        "cycle_id": str(cycle.id),
        "total": quote.total_quoted_price if quote else None,
        "items": po_items,
        "preferred_delivery_window": cycle.preferred_delivery_window,
    }

    try:
        from services.email_daemon import send_po_email
        import asyncio
        asyncio.run(send_po_email(
            to_email=dist.demo_routing_email or "",
            distributor_name=dist.name,
            po_payload=po_payload,
        ))
    except Exception as e:
        print(f"[approve] PO email failed: {e}")

    cycle.status = "COMPLETED"
    session.add(cycle)

    if quote:
        quote.quote_status = "APPROVED"
        session.add(quote)

    purchase_receipt_id = str(uuid.uuid4())

    notif = Notification(
        title="Purchase Order Sent",
        message=f"PO confirmed with {dist.name}. Total: ${quote.total_quoted_price or 'N/A'}.",
    )
    session.add(notif)
    session.commit()

    return {"purchase_receipt_id": purchase_receipt_id, "po_payload": po_payload}


@router.get("/purchase-history")
def purchase_history(session: Session = Depends(get_session)):
    profile = session.exec(select(RestaurantProfile)).first()
    if not profile:
        return []

    completed_cycles = session.exec(
        select(ProcurementCycle)
        .where(ProcurementCycle.restaurant_profile_id == profile.id)
        .where(ProcurementCycle.status == "COMPLETED")
        .order_by(ProcurementCycle.created_at.desc())
    ).all()

    results = []
    for cycle in completed_cycles:
        approved_quote = session.exec(
            select(DistributorQuote)
            .where(DistributorQuote.procurement_cycle_id == cycle.id)
            .where(DistributorQuote.quote_status == "APPROVED")
        ).first()
        if approved_quote:
            dist = session.get(Distributor, approved_quote.distributor_id)
            results.append({
                "id": str(cycle.id),
                "distributor_name": dist.name if dist else "Unknown",
                "total_quoted_cost": approved_quote.total_quoted_price,
                "purchased_at": cycle.created_at.isoformat(),
            })

    return results
