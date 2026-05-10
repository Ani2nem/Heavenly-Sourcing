"""
Ingredient-level configuration endpoints.

Currently exposes per-restaurant pack-size overrides used by the procurement
RFP. When all three of pack_qty / pack_unit / pack_label are set on an
ingredient, the procurement pipeline uses them verbatim instead of consulting
``services/pack_inference._PACK_RULES``.

Pack overrides are global to the restaurant (the demo schema treats
ingredients as a shared catalog deduped by name). If a vendor publishes a
custom pack catalog or the kitchen has a standing preference, set the
override here once and every future RFP will use it.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from database import get_session
from models import Ingredient
from services.pack_inference import compute_purchase, infer_pack

router = APIRouter(tags=["ingredients"])


class PackOverrideRequest(BaseModel):
    """Set or clear a pack-size override.

    To **set** an override, send all three fields:
        {"pack_qty": 6.0, "pack_unit": "lb", "pack_label": "6-lb bag (custom SKU)"}

    To **clear** the override and fall back to the inferred default, send:
        {"pack_qty": null, "pack_unit": null, "pack_label": null}
    """
    pack_qty: Optional[float] = None
    pack_unit: Optional[str] = None
    pack_label: Optional[str] = None


def _serialize_ingredient(ing: Ingredient) -> Dict[str, Any]:
    inferred_rule = infer_pack(ing.name, ing.category, ing.culinary_unit)
    inferred_default = None
    if inferred_rule:
        inferred_default = {
            "pack_qty": float(inferred_rule["qty"]),
            "pack_unit": inferred_rule["unit"],
            "pack_label": inferred_rule["label"],
        }
    return {
        "ingredient_id": str(ing.id),
        "name": ing.name,
        "category": ing.category,
        "culinary_unit": ing.culinary_unit,
        "shelf_life_days": ing.shelf_life_days,
        "pack_override": {
            "pack_qty": ing.pack_qty_override,
            "pack_unit": ing.pack_unit_override,
            "pack_label": ing.pack_label_override,
            "is_set": all([
                ing.pack_qty_override is not None,
                ing.pack_unit_override,
                ing.pack_label_override,
            ]),
        },
        "pack_default_inferred": inferred_default,
    }


@router.get("/ingredients")
def list_ingredients(session: Session = Depends(get_session)):
    """Browse every ingredient with its inferred default pack and any override."""
    rows: List[Ingredient] = session.exec(select(Ingredient).order_by(Ingredient.name)).all()
    return {"ingredients": [_serialize_ingredient(r) for r in rows]}


@router.get("/ingredients/{ingredient_id}")
def get_ingredient(ingredient_id: str, session: Session = Depends(get_session)):
    try:
        ing_uuid = uuid.UUID(ingredient_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid ingredient_id")
    ing = session.get(Ingredient, ing_uuid)
    if not ing:
        raise HTTPException(status_code=404, detail="Ingredient not found")
    return _serialize_ingredient(ing)


@router.patch("/ingredients/{ingredient_id}/pack")
def set_pack_override(
    ingredient_id: str,
    payload: PackOverrideRequest,
    session: Session = Depends(get_session),
):
    """Set or clear the pack-size override for an ingredient.

    Sends back the resolved ingredient (with both override and inferred
    default) plus a preview of the purchase plan we'd use today against a
    1-unit recipe need. Use the preview to sanity-check that the override
    converts cleanly with the recipe unit (e.g. don't set a pack measured
    in `lb` for an ingredient whose recipe is in `fl oz`).
    """
    try:
        ing_uuid = uuid.UUID(ingredient_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid ingredient_id")
    ing = session.get(Ingredient, ing_uuid)
    if not ing:
        raise HTTPException(status_code=404, detail="Ingredient not found")

    # Treat partial input as a clear; an override is only respected when all
    # three fields are non-empty.
    is_set = payload.pack_qty is not None and payload.pack_unit and payload.pack_label
    if is_set:
        if payload.pack_qty <= 0:
            raise HTTPException(status_code=422, detail="pack_qty must be > 0")
        ing.pack_qty_override = float(payload.pack_qty)
        ing.pack_unit_override = payload.pack_unit.strip()
        ing.pack_label_override = payload.pack_label.strip()
    else:
        ing.pack_qty_override = None
        ing.pack_unit_override = None
        ing.pack_label_override = None

    session.add(ing)
    session.commit()
    session.refresh(ing)

    # Preview: what plan would the system use TODAY for a single recipe-unit
    # need? Helps the caller catch unit-mismatch issues immediately.
    preview = compute_purchase(
        ing.name,
        ing.category,
        culinary_qty=1.0,
        culinary_unit=ing.culinary_unit or "unit",
        override_qty=ing.pack_qty_override,
        override_unit=ing.pack_unit_override,
        override_label=ing.pack_label_override,
    )

    return {
        **_serialize_ingredient(ing),
        "preview_plan_for_one_unit": preview,
    }
