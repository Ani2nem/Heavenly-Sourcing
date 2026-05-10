"""
Admin / maintenance endpoints.

Currently:

- POST /api/admin/usda/backfill — retry USDA enrichment for every ingredient
  with a NULL `usda_fdc_id` and (optionally) for every AMS-mappable ingredient
  that has zero stored price rows. Useful after fixing a bug in the USDA
  client without re-running the full menu upload.

Kept in its own router so it's easy to gate behind auth later.
"""
from __future__ import annotations

import threading
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from database import get_session
from models import Ingredient, IngredientPrice

router = APIRouter(tags=["admin"])


class UsdaBackfillRequest(BaseModel):
    # If False, only ingredients with NULL fdc_id and no existing price rows
    # are touched. If True, retry EVERY ingredient (forces a re-fetch even if
    # we previously cached a slug). Defaults to False so calling this is safe.
    force: bool = False
    # Run the backfill synchronously (waits and returns the full report) vs
    # background (returns immediately with "started"). Sync is great for the
    # demo; background is better for >100 ingredients.
    background: bool = False


def _run_backfill(force: bool) -> Dict[str, Any]:
    """Re-runnable backfill loop. Importable so tests can call it directly."""
    from database import engine
    from sqlmodel import Session as _Session
    from services.usda_client import search_fdc_id
    from services.ams_pricing import (
        fetch_and_store_prices_for_ingredient,
        find_mapping_for,
    )

    stats = {
        "fdc_attempted": 0,
        "fdc_resolved": 0,
        "ams_attempted": 0,
        "ams_rows_inserted": 0,
        "ams_skipped_no_mapping": 0,
        "errors": [],
    }

    with _Session(engine) as session:
        ingredients: List[Ingredient] = session.exec(select(Ingredient)).all()

        for ing in ingredients:
            # ── FDC pass ────────────────────────────────────────────────────
            if force or not ing.usda_fdc_id:
                stats["fdc_attempted"] += 1
                try:
                    fid = search_fdc_id(ing.name)
                    if fid:
                        ing.usda_fdc_id = fid
                        session.add(ing)
                        stats["fdc_resolved"] += 1
                except Exception as exc:
                    stats["errors"].append(f"FDC {ing.name!r}: {exc}")

            # ── AMS pass ────────────────────────────────────────────────────
            if not find_mapping_for(ing.name):
                stats["ams_skipped_no_mapping"] += 1
                continue

            existing_price_rows = session.exec(
                select(IngredientPrice).where(
                    IngredientPrice.ingredient_id == ing.id
                ).limit(1)
            ).first()
            if existing_price_rows and not force:
                continue

            stats["ams_attempted"] += 1
            try:
                stats["ams_rows_inserted"] += fetch_and_store_prices_for_ingredient(
                    session, ing
                )
            except Exception as exc:
                stats["errors"].append(f"AMS {ing.name!r}: {exc}")

        session.commit()

    # Cap error list so the response stays small
    if len(stats["errors"]) > 20:
        stats["errors"] = stats["errors"][:20] + [
            f"... ({len(stats['errors']) - 20} more)"
        ]
    return stats


@router.post("/admin/usda/backfill")
def admin_usda_backfill(
    payload: Optional[UsdaBackfillRequest] = None,
    session: Session = Depends(get_session),  # noqa: ARG001 - session unused; helper opens its own
):
    req = payload or UsdaBackfillRequest()
    if req.background:
        threading.Thread(
            target=lambda: _run_backfill(req.force),
            daemon=True,
        ).start()
        return {"ok": True, "started": True, "background": True, "force": req.force}

    stats = _run_backfill(req.force)
    return {
        "ok": True,
        "background": False,
        "force": req.force,
        "stats": stats,
    }


@router.get("/admin/usda/coverage")
def admin_usda_coverage(session: Session = Depends(get_session)):
    """Quick read-only summary of how much USDA data we actually have."""
    from services.ams_pricing import find_mapping_for

    ings: List[Ingredient] = session.exec(select(Ingredient)).all()
    total = len(ings)
    with_fdc = sum(1 for i in ings if i.usda_fdc_id)
    with_ams_mapping = sum(1 for i in ings if find_mapping_for(i.name))
    price_rows = session.exec(select(IngredientPrice)).all()
    by_ing = {}
    for p in price_rows:
        by_ing.setdefault(p.ingredient_id, 0)
        by_ing[p.ingredient_id] += 1
    with_prices = len(by_ing)
    return {
        "ingredients_total": total,
        "ingredients_with_fdc_id": with_fdc,
        "ingredients_with_ams_mapping": with_ams_mapping,
        "ingredients_with_price_rows": with_prices,
        "total_price_rows": len(price_rows),
    }
