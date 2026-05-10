import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from database import get_session
from models import (
    CycleDishForecast,
    CycleIngredientsNeeded,
    Distributor,
    DistributorQuote,
    DistributorQuoteItem,
    Ingredient,
    Notification,
    ProcurementCycle,
    PurchaseReceipt,
    Recipe,
    RecipeIngredient,
    RestaurantProfile,
)

router = APIRouter(tags=["procurement"])


# ─── Request models ──────────────────────────────────────────────────────────

class InitiateCycleRequest(BaseModel):
    dish_forecasts: Dict[str, int]


class ApproveCycleRequest(BaseModel):
    selected_distributor_id: str


# ─── Aggregation ──────────────────────────────────────────────────────────────

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


# ─── Distributor matching ─────────────────────────────────────────────────────

_CATEGORY_TO_VENDOR_KEYS: Dict[str, List[str]] = {
    "Dairy": ["dairy"],
    "Proteins": ["meat", "seafood"],
    "Produce": ["produce"],
    "Bakery": ["dry goods", "bakery"],
    "Condiments": ["dry goods"],
    "Dry Goods": ["dry goods"],
    "Pantry": ["dry goods"],
    "Frozen": ["frozen"],
}


def _ingredient_matches_distributor(
    ing_category: Optional[str], supplied_categories: Optional[List[str]]
) -> bool:
    """Best-effort: if we don't know what the distributor stocks, send everything."""
    if not supplied_categories:
        return True
    if not ing_category:
        return True
    keys = _CATEGORY_TO_VENDOR_KEYS.get(ing_category, [])
    if not keys:
        return True
    sup = {s.lower() for s in supplied_categories}
    return any(k in sup for k in keys)


def _filter_ingredients_for_distributor(
    base_list: List[Dict[str, Any]],
    supplied_categories: Optional[List[str]],
) -> List[Dict[str, Any]]:
    return [
        item
        for item in base_list
        if _ingredient_matches_distributor(item.get("category"), supplied_categories)
    ]


# ─── USDA enrichment ─────────────────────────────────────────────────────────

def _backfill_usda_ids(session: Session, ingredient_ids: List[uuid.UUID]) -> None:
    """Look up USDA FDC ids for any ingredient that doesn't have one yet."""
    from services.usda_client import search_fdc_id

    if not ingredient_ids:
        return
    rows: List[Ingredient] = session.exec(
        select(Ingredient).where(Ingredient.id.in_(ingredient_ids))
    ).all()
    for ing in rows:
        if ing.usda_fdc_id:
            continue
        fdc_id = search_fdc_id(ing.name)
        if fdc_id:
            ing.usda_fdc_id = fdc_id
            session.add(ing)
    session.commit()


# ─── Background pipeline ──────────────────────────────────────────────────────

def _background_procurement(cycle_id: str, profile_id: str) -> None:
    """Discover distributors, backfill USDA ids, compute benchmarks, dispatch RFPs.

    Idempotent: if this cycle already has DistributorQuote rows, the function
    short-circuits. This protects against duplicate sends if BackgroundTasks
    fires the function twice (e.g. a hot-reload mid-task or the user double-
    clicking Procure from the UI).
    """
    from database import engine
    from sqlmodel import Session as DBSession
    from services.email_daemon import send_rfp_email
    from services.places_discovery import discover_distributors
    from services.usda_client import build_benchmarks

    with DBSession(engine) as session:
        cycle = session.get(ProcurementCycle, uuid.UUID(cycle_id))
        profile = session.get(RestaurantProfile, uuid.UUID(profile_id))
        if not cycle or not profile:
            return

        # Idempotent guard
        existing_quotes = session.exec(
            select(DistributorQuote)
            .where(DistributorQuote.procurement_cycle_id == cycle.id)
        ).all()
        if existing_quotes:
            print(
                f"[procurement] cycle {cycle_id} already has {len(existing_quotes)} "
                "quote(s); skipping duplicate background run"
            )
            return

        # 1. Distributor discovery (Google Places)
        try:
            discover_distributors(profile, session)
        except Exception as exc:
            print(f"[procurement] distributor discovery failed: {exc}")

        # 2. Load distributors and ingredients-needed for this cycle.
        #    Dedupe by lowercased name in case discovery rounds (or earlier
        #    seed data) created multiple rows for the same vendor — without
        #    this we'd send the same RFP multiple times.
        all_distributors: List[Distributor] = session.exec(
            select(Distributor).where(Distributor.restaurant_profile_id == profile.id)
        ).all()
        seen_names: set = set()
        distributors: List[Distributor] = []
        for d in all_distributors:
            key = (d.name or "").strip().lower()
            if not key or key in seen_names:
                continue
            seen_names.add(key)
            distributors.append(d)
        if len(distributors) != len(all_distributors):
            print(
                f"[procurement] deduped distributors {len(all_distributors)} -> "
                f"{len(distributors)} (removed dupes by name)"
            )

        needed = session.exec(
            select(CycleIngredientsNeeded).where(
                CycleIngredientsNeeded.procurement_cycle_id == cycle.id
            )
        ).all()

        # 3. Backfill USDA ids for ingredients in this cycle
        try:
            _backfill_usda_ids(session, [cin.ingredient_id for cin in needed])
        except Exception as exc:
            print(f"[procurement] USDA backfill failed: {exc}")

        # 3b. Backfill USDA AMS Market News *price* points for any cycle
        # ingredient that doesn't have data yet. The menu-upload daemon
        # thread normally handles this, but if the user runs Procure right
        # after upload it may not have completed. We do a synchronous
        # top-up here for items with an AMS mapping but no stored prices,
        # so the RFP "Reference Benchmark" column can use real data.
        try:
            from services.ams_pricing import (
                fetch_and_store_prices_for_ingredient,
                find_mapping_for,
            )
            from models import IngredientPrice
            for cin in needed:
                ing = session.get(Ingredient, cin.ingredient_id)
                if not ing or not find_mapping_for(ing.name):
                    continue
                already = session.exec(
                    select(IngredientPrice).where(
                        IngredientPrice.ingredient_id == ing.id
                    ).limit(1)
                ).first()
                if already:
                    continue
                try:
                    fetch_and_store_prices_for_ingredient(session, ing)
                except Exception as exc:
                    print(f"[procurement] AMS price fetch failed for {ing.name}: {exc}")
        except Exception as exc:
            print(f"[procurement] AMS top-up step failed: {exc}")

        # 4. Build the master ingredient list once.
        #    Each entry is decorated with a pack-size plan so the RFP can
        #    show vendors a realistic order quantity ("1 × #10 can") instead
        #    of the raw culinary need ("10 fl oz of pizza sauce"). Resolution
        #    order: per-restaurant override on the Ingredient row → inferred
        #    default in `services/pack_inference._PACK_RULES` → fallback to
        #    raw culinary qty/unit. The plan is also persisted onto the
        #    `cycle_ingredients_needed` row so audit trails / future cycles
        #    can replay it without re-running inference.
        from services.pack_inference import compute_purchase

        ingredient_list: List[Dict[str, Any]] = []
        for cin in needed:
            ing = session.get(Ingredient, cin.ingredient_id)
            if not ing:
                continue
            recipe_unit = ing.culinary_unit or "unit"
            recipe_qty = float(cin.culinary_qty_needed or 0.0)
            plan = compute_purchase(
                ing.name,
                ing.category,
                recipe_qty,
                recipe_unit,
                override_qty=ing.pack_qty_override,
                override_unit=ing.pack_unit_override,
                override_label=ing.pack_label_override,
            )

            # Persist the resolved plan onto the cycle row.
            if plan:
                cin.pack_count = plan["packs_needed"]
                cin.pack_unit = plan["pack_unit"]
                cin.pack_label = plan["pack_label"]
                cin.pack_total_qty = plan["total_in_pack_unit"]
                cin.pack_source = plan["source"]
                cin.purchasing_qty_needed = float(plan["packs_needed"])
            else:
                cin.pack_count = None
                cin.pack_unit = None
                cin.pack_label = None
                cin.pack_total_qty = None
                cin.pack_source = None
                cin.purchasing_qty_needed = recipe_qty
            session.add(cin)

            ingredient_list.append(
                {
                    "name": ing.name,
                    "ingredient_id": ing.id,
                    "qty": cin.purchasing_qty_needed,
                    "unit": recipe_unit,
                    "shelf_life_days": ing.shelf_life_days or 99,
                    "category": ing.category,
                    "fdc_id": ing.usda_fdc_id,
                    "purchase_plan": plan,
                }
            )
        session.commit()

        # Pass the session so the benchmark builder can look up real USDA
        # AMS Market News prices per ingredient where available, and only
        # fall back to category $/lb estimates for mass-based recipe units.
        benchmarks = build_benchmarks(ingredient_list, session=session)

        if not distributors:
            session.add(
                Notification(
                    title="No Distributors Found",
                    message=(
                        "Discovery returned no distributors for your zip. Check "
                        "GOOGLE_PLACES_API_KEY and the profile zip code, then re-run procure."
                    ),
                )
            )
            cycle.status = "COLLECTING_QUOTES"
            session.add(cycle)
            session.commit()
            return

        # 5. Per-distributor RFP
        for dist in distributors:
            per_dist_items = _filter_ingredients_for_distributor(
                ingredient_list, dist.supplied_categories
            )
            if not per_dist_items:
                continue

            quote = DistributorQuote(
                procurement_cycle_id=cycle.id,
                distributor_id=dist.id,
                quote_status="PENDING",
            )
            session.add(quote)
            session.flush()

            try:
                from services.places_discovery import build_demo_routing_email
                to_email = (
                    dist.demo_routing_email
                    or build_demo_routing_email(profile.email, dist.name)
                )
                send_rfp_email(
                    to_email=to_email,
                    distributor_name=dist.name,
                    ingredient_list=per_dist_items,
                    cycle_id=cycle_id,
                    quote_id=str(quote.id),
                    benchmarks=benchmarks,
                )
            except Exception as exc:
                print(f"[procurement] RFP email to {dist.name} failed: {exc}")

        # 6. Discovery + RFP dispatch is complete — flip the status so the UI
        #    knows we're now waiting on vendors (and stops showing the
        #    "discovering" banner).
        cycle.status = "COLLECTING_QUOTES"
        session.add(cycle)
        session.commit()


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.post("/procurement/cycle/initiate", status_code=201)
def initiate_cycle(
    payload: InitiateCycleRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    profile = session.exec(select(RestaurantProfile)).first()
    if not profile:
        raise HTTPException(status_code=400, detail="Create a profile first")

    ingredient_totals = _compute_ingredients_needed(payload.dish_forecasts, session)

    try:
        # Start in DISCOVERING_DISTRIBUTORS so the UI doesn't flash a misleading
        # "no distributors found" banner during the 5-15s window where the
        # background task is geocoding + querying Places. The background task
        # transitions to COLLECTING_QUOTES once RFPs are dispatched.
        cycle = ProcurementCycle(
            restaurant_profile_id=profile.id,
            status="DISCOVERING_DISTRIBUTORS",
            week_start_date=date.today(),
        )
        session.add(cycle)
        session.flush()

        for dish_id_str, qty in payload.dish_forecasts.items():
            try:
                dish_id = uuid.UUID(dish_id_str)
            except ValueError:
                continue
            session.add(CycleDishForecast(
                procurement_cycle_id=cycle.id,
                dish_id=dish_id,
                forecasted_quantity=qty,
            ))

        for ing_id_str, culinary_qty in ingredient_totals.items():
            session.add(CycleIngredientsNeeded(
                procurement_cycle_id=cycle.id,
                ingredient_id=uuid.UUID(ing_id_str),
                culinary_qty_needed=culinary_qty,
                purchasing_qty_needed=culinary_qty,  # 1:1; pack-size logic later
            ))

        session.commit()
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create cycle: {exc}")

    background_tasks.add_task(
        _background_procurement,
        str(cycle.id),
        str(profile.id),
    )

    return {"cycle_id": str(cycle.id), "status": "DISCOVERING_DISTRIBUTORS"}


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

    distributors_for_cycle = session.exec(
        select(Distributor).where(Distributor.restaurant_profile_id == profile.id)
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
        "week_start_date": cycle.week_start_date.isoformat() if cycle.week_start_date else None,
        "distributor_count": len(distributors_for_cycle),
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

    try:
        from services.email_daemon import send_followup_email
        send_followup_email(
            to_email=dist.demo_routing_email if dist else "",
            distributor_name=dist.name if dist else "Vendor",
            cycle_id=str(quote.procurement_cycle_id),
            quote_id=quote_id,
        )
    except Exception as exc:
        print(f"[ping] follow-up email failed: {exc}")

    quote.quote_status = "FOLLOW_UP_SENT"
    session.add(quote)
    session.add(Notification(
        title="Follow-up Sent",
        message=f"Follow-up dispatched to {dist.name if dist else 'vendor'}.",
    ))
    session.commit()

    return {"quote_status": "FOLLOW_UP_SENT"}


@router.get("/procurement/cycle/active/comparison")
def get_active_comparison(session: Session = Depends(get_session)):
    """Return an ingredient × vendor comparison + the optimal multi-vendor cart.

    Shape:
      {
        "cycle_id": "...",
        "rows": [
          {
            "ingredient_id": "...",
            "ingredient_name": "Mozzarella",
            "offers": [
              {"distributor_id": "...", "distributor_name": "Heritage Dairy",
               "unit_price": 4.25, "is_winner": true}
            ],
            "winner": {...},
            "single_source": false
          }
        ],
        "vendors": [{"distributor_id", "distributor_name", "items_quoted",
                     "items_won", "won_total"}],
        "grand_total": 12.34,
        "ingredients_with_no_quotes": ["Pizza Dough"],
        "auto_match_in_progress": int  # how many price-match emails sent so far
      }
    """
    from agents.scoring_engine import build_optimal_cart

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
        return None

    quotes = session.exec(
        select(DistributorQuote)
        .where(DistributorQuote.procurement_cycle_id == cycle.id)
        .where(DistributorQuote.quote_status == "RECEIVED")
    ).all()

    cart_items: List[Dict[str, Any]] = []
    for q in quotes:
        dist = session.get(Distributor, q.distributor_id)
        for item in session.exec(
            select(DistributorQuoteItem)
            .where(DistributorQuoteItem.distributor_quote_id == q.id)
        ).all():
            if item.quoted_price_per_unit is None:
                continue
            ing = session.get(Ingredient, item.ingredient_id)
            cart_items.append({
                "distributor_id": str(q.distributor_id),
                "distributor_name": dist.name if dist else "",
                "ingredient_id": str(item.ingredient_id),
                "ingredient_name": ing.name if ing else "",
                "unit_price": float(item.quoted_price_per_unit),
            })

    cart = build_optimal_cart(cart_items)

    rows = []
    for ing_id, entry in cart["by_ingredient"].items():
        winner_id = entry["winner"]["distributor_id"]
        offers = [
            {**o, "is_winner": o["distributor_id"] == winner_id}
            for o in entry["all_offers"]
        ]
        rows.append({
            "ingredient_id": ing_id,
            "ingredient_name": entry["ingredient_name"],
            "offers": offers,
            "winner": entry["winner"],
            "single_source": entry["runner_up"] is None,
        })
    rows.sort(key=lambda r: r["ingredient_name"].lower())

    vendors = [
        {"distributor_id": did, **{k: v for k, v in info.items() if k != "losing_total"}}
        for did, info in cart["by_vendor"].items()
    ]

    # Find ingredients we asked for but no vendor quoted
    needed = session.exec(
        select(CycleIngredientsNeeded)
        .where(CycleIngredientsNeeded.procurement_cycle_id == cycle.id)
    ).all()
    quoted_ids = {r["ingredient_id"] for r in rows}
    no_quote = []
    for cin in needed:
        if str(cin.ingredient_id) in quoted_ids:
            continue
        ing = session.get(Ingredient, cin.ingredient_id)
        if ing:
            no_quote.append(ing.name)

    return {
        "cycle_id": str(cycle.id),
        "rows": rows,
        "vendors": vendors,
        "grand_total": cart["grand_total"],
        "ingredient_count": cart["ingredient_count"],
        "ingredients_with_no_quotes": no_quote,
    }


@router.post("/procurement/cycle/active/approve-optimal")
def approve_optimal_cart(session: Session = Depends(get_session)):
    """Split the order across vendors based on the optimal cart and dispatch
    one PO email per chosen vendor (only their winning items)."""
    from agents.scoring_engine import build_optimal_cart
    from services.email_daemon import send_po_email

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

    quotes = session.exec(
        select(DistributorQuote)
        .where(DistributorQuote.procurement_cycle_id == cycle.id)
        .where(DistributorQuote.quote_status == "RECEIVED")
    ).all()
    if not quotes:
        raise HTTPException(status_code=400, detail="No received quotes to approve")

    # Build the cart with the same logic the comparison endpoint uses
    cart_items: List[Dict[str, Any]] = []
    quote_by_dist: Dict[str, DistributorQuote] = {}
    for q in quotes:
        quote_by_dist[str(q.distributor_id)] = q
        dist = session.get(Distributor, q.distributor_id)
        for item in session.exec(
            select(DistributorQuoteItem)
            .where(DistributorQuoteItem.distributor_quote_id == q.id)
        ).all():
            if item.quoted_price_per_unit is None:
                continue
            ing = session.get(Ingredient, item.ingredient_id)
            cart_items.append({
                "distributor_id": str(q.distributor_id),
                "distributor_name": dist.name if dist else "",
                "ingredient_id": str(item.ingredient_id),
                "ingredient_name": ing.name if ing else "",
                "unit_price": float(item.quoted_price_per_unit),
            })
    cart = build_optimal_cart(cart_items)

    # Group winning items by vendor
    items_by_vendor: Dict[str, List[Dict[str, Any]]] = {}
    for entry in cart["by_ingredient"].values():
        w = entry["winner"]
        items_by_vendor.setdefault(w["distributor_id"], []).append({
            "ingredient": entry["ingredient_name"],
            "unit_price": w["unit_price"],
        })

    if not items_by_vendor:
        raise HTTPException(status_code=400, detail="Cart is empty after building")

    pos_dispatched = []
    for did, items in items_by_vendor.items():
        q = quote_by_dist.get(did)
        if not q:
            continue
        dist = session.get(Distributor, q.distributor_id)
        if not dist:
            continue
        total = round(sum(i["unit_price"] for i in items), 2)
        po_payload = {
            "distributor": dist.name,
            "cycle_id": str(cycle.id),
            "po_id": str(q.id),
            "total": total,
            "items": items,
        }
        try:
            send_po_email(
                to_email=dist.demo_routing_email or "",
                distributor_name=dist.name,
                po_payload=po_payload,
            )
        except Exception as exc:
            print(f"[approve-optimal] PO email to {dist.name} failed: {exc}")

        q.quote_status = "APPROVED"
        q.total_quoted_price = total  # store the *won* total, not the original quote total
        session.add(q)
        pos_dispatched.append({
            "distributor_id": did,
            "distributor_name": dist.name,
            "po_id": str(q.id),
            "total": total,
            "item_count": len(items),
        })

    # Mark losing quotes (vendors who quoted but didn't win anything) as DECLINED
    winning_dids = set(items_by_vendor.keys())
    for q in quotes:
        if str(q.distributor_id) not in winning_dids:
            q.quote_status = "DECLINED"
            session.add(q)

    cycle.status = "AWAITING_RECEIPT"
    session.add(cycle)
    session.add(Notification(
        title="Optimal Cart Approved",
        message=(
            f"Sent {len(pos_dispatched)} PO(s) — grand total "
            f"${cart['grand_total']:.2f} across {cart['ingredient_count']} ingredients."
        ),
    ))
    session.commit()

    return {
        "cycle_id": str(cycle.id),
        "status": "AWAITING_RECEIPT",
        "grand_total": cart["grand_total"],
        "pos": pos_dispatched,
    }


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
    if not quote:
        raise HTTPException(status_code=404, detail="No quote for that distributor on this cycle")

    items_raw = session.exec(
        select(DistributorQuoteItem).where(DistributorQuoteItem.distributor_quote_id == quote.id)
    ).all()
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
        "po_id": str(quote.id),
        "total": quote.total_quoted_price,
        "items": po_items,
    }

    try:
        from services.email_daemon import send_po_email
        send_po_email(
            to_email=dist.demo_routing_email or "",
            distributor_name=dist.name,
            po_payload=po_payload,
        )
    except Exception as exc:
        print(f"[approve] PO email failed: {exc}")

    cycle.status = "AWAITING_RECEIPT"
    session.add(cycle)
    quote.quote_status = "APPROVED"
    session.add(quote)
    session.add(Notification(
        title="Purchase Order Sent",
        message=(
            f"PO confirmed with {dist.name}. Total: "
            f"${quote.total_quoted_price if quote.total_quoted_price is not None else 'N/A'}. "
            f"Awaiting receipt."
        ),
    ))
    session.commit()

    return {
        "cycle_id": str(cycle.id),
        "po_id": str(quote.id),
        "status": "AWAITING_RECEIPT",
        "po_payload": po_payload,
    }


@router.get("/purchase-history")
def purchase_history(session: Session = Depends(get_session)):
    profile = session.exec(select(RestaurantProfile)).first()
    if not profile:
        return []

    cycles = session.exec(
        select(ProcurementCycle)
        .where(ProcurementCycle.restaurant_profile_id == profile.id)
        .where(ProcurementCycle.status.in_(["AWAITING_RECEIPT", "COMPLETED"]))
        .order_by(ProcurementCycle.created_at.desc())
    ).all()

    results = []
    for cycle in cycles:
        approved_quotes = session.exec(
            select(DistributorQuote)
            .where(DistributorQuote.procurement_cycle_id == cycle.id)
            .where(DistributorQuote.quote_status == "APPROVED")
        ).all()
        if not approved_quotes:
            continue

        per_vendor = []
        grand_total = 0.0
        for aq in approved_quotes:
            d = session.get(Distributor, aq.distributor_id)
            grand_total += float(aq.total_quoted_price or 0)
            per_vendor.append({
                "distributor_name": d.name if d else "Unknown",
                "total": aq.total_quoted_price,
                "po_id": str(aq.id),
            })

        receipts_raw = session.exec(
            select(PurchaseReceipt)
            .where(PurchaseReceipt.procurement_cycle_id == cycle.id)
            .order_by(PurchaseReceipt.received_at.desc())
        ).all()
        receipts = [
            {
                "id": str(r.id),
                "distributor_id": str(r.distributor_id),
                "receipt_number": r.receipt_number,
                "total_amount": r.total_amount,
                "received_at": r.received_at.isoformat(),
                "subject": r.raw_email_subject,
            }
            for r in receipts_raw
        ]

        results.append({
            "id": str(cycle.id),
            "vendors": per_vendor,
            "distributor_name": (
                per_vendor[0]["distributor_name"]
                if len(per_vendor) == 1
                else f"{len(per_vendor)} vendors"
            ),
            "total_quoted_cost": round(grand_total, 2),
            "purchased_at": cycle.created_at.isoformat(),
            "status": cycle.status,
            "receipts": receipts,
            # back-compat single-receipt shape (latest receipt)
            "receipt": receipts[0] if receipts else None,
        })

    return results


@router.get("/purchase-history/{cycle_id}")
def purchase_history_detail(
    cycle_id: str,
    session: Session = Depends(get_session),
):
    """Per-vendor breakdown for one completed/awaiting cycle.

    Returns:
      - per-vendor PO with the items each vendor won (re-derived from the
        optimal cart so we don't need a new persistence column)
      - per-vendor receipt status (matched / pending) and the receipt's
        own line items if the LLM extracted them
      - whatever ingredients ended up unmatched to a vendor (rare but
        useful when the cart split was incomplete)
    """
    from agents.scoring_engine import build_optimal_cart

    profile = session.exec(select(RestaurantProfile)).first()
    if not profile:
        raise HTTPException(status_code=404, detail="No profile")
    cycle_uuid = uuid.UUID(cycle_id)
    cycle = session.get(ProcurementCycle, cycle_uuid)
    if not cycle or cycle.restaurant_profile_id != profile.id:
        raise HTTPException(status_code=404, detail="Cycle not found")

    # ── Pull every quote from this cycle (includes RECEIVED / DECLINED so
    #    we can re-derive who won what) ──────────────────────────────────
    all_quotes = session.exec(
        select(DistributorQuote)
        .where(DistributorQuote.procurement_cycle_id == cycle.id)
    ).all()

    cart_items: List[Dict[str, Any]] = []
    quote_by_dist: Dict[str, DistributorQuote] = {}
    for q in all_quotes:
        if q.quote_status not in ("RECEIVED", "APPROVED"):
            continue
        quote_by_dist[str(q.distributor_id)] = q
        dist = session.get(Distributor, q.distributor_id)
        for item in session.exec(
            select(DistributorQuoteItem)
            .where(DistributorQuoteItem.distributor_quote_id == q.id)
        ).all():
            if item.quoted_price_per_unit is None:
                continue
            ing = session.get(Ingredient, item.ingredient_id)
            cart_items.append({
                "distributor_id": str(q.distributor_id),
                "distributor_name": dist.name if dist else "",
                "ingredient_id": str(item.ingredient_id),
                "ingredient_name": ing.name if ing else "",
                "unit_price": float(item.quoted_price_per_unit),
            })
    cart = build_optimal_cart(cart_items) if cart_items else {
        "by_ingredient": {},
        "by_vendor": {},
        "grand_total": 0.0,
        "ingredient_count": 0,
    }

    # ── Match receipts to vendors so the UI can show "received vs pending"
    receipts_raw = session.exec(
        select(PurchaseReceipt)
        .where(PurchaseReceipt.procurement_cycle_id == cycle.id)
        .order_by(PurchaseReceipt.received_at.desc())
    ).all()
    receipt_by_dist: Dict[str, PurchaseReceipt] = {}
    for r in receipts_raw:
        # First receipt per distributor wins (most recent due to ORDER BY DESC)
        receipt_by_dist.setdefault(str(r.distributor_id), r)

    # ── Build per-vendor view (only APPROVED quotes are real POs) ─────────
    vendors_view: List[Dict[str, Any]] = []
    grand_total = 0.0
    today = datetime.utcnow().date()
    for q in all_quotes:
        if q.quote_status != "APPROVED":
            continue
        dist = session.get(Distributor, q.distributor_id)
        won_items = []
        for entry in cart["by_ingredient"].values():
            w = entry["winner"]
            if w["distributor_id"] != str(q.distributor_id):
                continue
            won_items.append({
                "ingredient_id": entry["ingredient_id"],
                "ingredient_name": entry["ingredient_name"],
                "unit_price": w["unit_price"],
            })

        receipt = receipt_by_dist.get(str(q.distributor_id))
        receipt_payload = None
        if receipt:
            receipt_payload = {
                "id": str(receipt.id),
                "receipt_number": receipt.receipt_number,
                "total_amount": receipt.total_amount,
                "received_at": receipt.received_at.isoformat() if receipt.received_at else None,
                "subject": receipt.raw_email_subject,
                "line_items": receipt.line_items,
            }

        approved_at = q.received_at or cycle.created_at
        days_since = max(0, (today - approved_at.date()).days) if approved_at else None
        grand_total += float(q.total_quoted_price or 0)
        vendors_view.append({
            "po_id": str(q.id),
            "distributor_id": str(q.distributor_id),
            "distributor_name": dist.name if dist else "Unknown",
            "distributor_email": (dist.demo_routing_email if dist else None),
            "po_total": q.total_quoted_price,
            "approved_at": approved_at.isoformat() if approved_at else None,
            "days_since_po": days_since,
            "items": won_items,
            "receipt": receipt_payload,
        })

    # ── Ingredients that needed buying but no vendor won them ─────────────
    needed_ing_ids = {
        str(cin.ingredient_id)
        for cin in session.exec(
            select(CycleIngredientsNeeded)
            .where(CycleIngredientsNeeded.procurement_cycle_id == cycle.id)
        ).all()
    }
    won_ing_ids = {entry["ingredient_id"] for entry in cart["by_ingredient"].values()}
    unmatched = []
    for ing_id in (needed_ing_ids - won_ing_ids):
        ing = session.get(Ingredient, uuid.UUID(ing_id))
        if ing:
            unmatched.append({"ingredient_id": ing_id, "ingredient_name": ing.name})

    return {
        "cycle_id": str(cycle.id),
        "status": cycle.status,
        "purchased_at": cycle.created_at.isoformat(),
        "grand_total": round(grand_total, 2),
        "vendor_count": len(vendors_view),
        "ingredient_count": cart["ingredient_count"],
        "vendors": vendors_view,
        "unmatched_ingredients": unmatched,
    }


class RequestReceiptRequest(BaseModel):
    note: Optional[str] = None  # currently unused; reserved for free-form additions


@router.post("/purchase-history/{cycle_id}/vendors/{distributor_id}/request-receipt")
def request_receipt(
    cycle_id: str,
    distributor_id: str,
    payload: Optional[RequestReceiptRequest] = None,  # noqa: ARG001
    session: Session = Depends(get_session),
):
    """Email the vendor asking for the invoice / receipt for an APPROVED PO."""
    from services.email_daemon import send_receipt_request_email
    from services.places_discovery import build_demo_routing_email

    profile = session.exec(select(RestaurantProfile)).first()
    if not profile:
        raise HTTPException(status_code=404, detail="No profile")

    cycle_uuid = uuid.UUID(cycle_id)
    dist_uuid = uuid.UUID(distributor_id)
    cycle = session.get(ProcurementCycle, cycle_uuid)
    if not cycle or cycle.restaurant_profile_id != profile.id:
        raise HTTPException(status_code=404, detail="Cycle not found")
    dist = session.get(Distributor, dist_uuid)
    if not dist:
        raise HTTPException(status_code=404, detail="Distributor not found")

    quote = session.exec(
        select(DistributorQuote)
        .where(DistributorQuote.procurement_cycle_id == cycle.id)
        .where(DistributorQuote.distributor_id == dist.id)
        .where(DistributorQuote.quote_status == "APPROVED")
    ).first()
    if not quote:
        raise HTTPException(
            status_code=404,
            detail="No approved PO for this vendor in this cycle",
        )

    to_email = (
        dist.demo_routing_email
        or build_demo_routing_email(profile.email, dist.name)
    )
    approved_at = quote.received_at or cycle.created_at
    days_since = (
        max(0, (datetime.utcnow().date() - approved_at.date()).days)
        if approved_at else None
    )

    try:
        send_receipt_request_email(
            to_email=to_email,
            distributor_name=dist.name,
            cycle_id=str(cycle.id),
            po_id=str(quote.id),
            po_total=quote.total_quoted_price,
            days_since_po=days_since,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Email send failed: {exc}")

    session.add(Notification(
        title=f"Invoice requested from {dist.name}",
        message=(
            f"We pinged {dist.name} for the invoice on PO {str(quote.id)[:6]}. "
            "They'll reply to your inbox; the receipt will appear here once parsed."
        ),
    ))
    session.commit()

    return {
        "ok": True,
        "to_email": to_email,
        "distributor_name": dist.name,
        "po_id": str(quote.id),
        "days_since_po": days_since,
    }
