"""
Quote scoring + multi-vendor "optimal cart" builder.

The naive aggregate-total approach is wrong when each vendor only quotes the
items they carry — Heritage Dairy quoting only mozzarella for $4.25 is not
"better" than Riverbend Produce quoting 5 items for $5.87. They are not
substitutes. So we work at the **ingredient level** instead and produce:

  * a per-ingredient table of which vendor priced each item at what price,
  * an "optimal cart" that picks the cheapest vendor per ingredient,
  * per-vendor stats (items quoted, items chosen, savings vs runner-up),
  * a natural-language recommendation that talks about the multi-vendor split.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from openai import OpenAI

from config import settings

_client = OpenAI(api_key=settings.openai_api_key)


# ─── Optimal-cart builder ────────────────────────────────────────────────────

def build_optimal_cart(quote_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Given a flat list of (vendor, ingredient, price) tuples, pick the
    cheapest vendor per ingredient and return a structured cart.

    quote_items expected shape:
        [
          {"distributor_id": "...", "distributor_name": "...",
           "ingredient_id": "...", "ingredient_name": "...",
           "unit_price": 4.25}
        ]

    Returns:
        {
          "by_ingredient": {
            ingredient_id: {
              "ingredient_name": str,
              "winner": {"distributor_id", "distributor_name", "unit_price"},
              "runner_up": {"distributor_id", "distributor_name", "unit_price"} | None,
              "all_offers": [...],
              "spread": float | None,   # winner_price - runner_up_price (negative)
            }
          },
          "by_vendor": {
            distributor_id: {
              "distributor_name": str,
              "items_quoted": int,
              "items_won": int,
              "won_total": float,
              "losing_total": float,   # what they would have charged for items they lost
            }
          },
          "grand_total": float,
          "ingredient_count": int,
        }
    """
    by_ingredient: Dict[str, Dict[str, Any]] = {}
    for item in quote_items:
        if item.get("unit_price") is None:
            continue
        ing_id = str(item.get("ingredient_id"))
        if not ing_id:
            continue
        bucket = by_ingredient.setdefault(
            ing_id,
            {
                "ingredient_name": item.get("ingredient_name") or "",
                "all_offers": [],
            },
        )
        bucket["all_offers"].append({
            "distributor_id": str(item.get("distributor_id")),
            "distributor_name": item.get("distributor_name") or "",
            "unit_price": float(item["unit_price"]),
        })

    by_vendor: Dict[str, Dict[str, Any]] = {}

    def _vendor_bucket(did: str, dname: str) -> Dict[str, Any]:
        return by_vendor.setdefault(did, {
            "distributor_name": dname,
            "items_quoted": 0,
            "items_won": 0,
            "won_total": 0.0,
            "losing_total": 0.0,
        })

    grand_total = 0.0
    for ing_id, bucket in by_ingredient.items():
        offers = sorted(bucket["all_offers"], key=lambda o: o["unit_price"])
        winner = offers[0]
        runner_up = offers[1] if len(offers) > 1 else None
        bucket["winner"] = winner
        bucket["runner_up"] = runner_up
        bucket["spread"] = (
            None if runner_up is None
            else round(winner["unit_price"] - runner_up["unit_price"], 4)
        )
        grand_total += winner["unit_price"]

        for o in offers:
            v = _vendor_bucket(o["distributor_id"], o["distributor_name"])
            v["items_quoted"] += 1
            if o["distributor_id"] == winner["distributor_id"]:
                v["items_won"] += 1
                v["won_total"] += winner["unit_price"]
            else:
                v["losing_total"] += o["unit_price"]

    return {
        "by_ingredient": by_ingredient,
        "by_vendor": by_vendor,
        "grand_total": round(grand_total, 2),
        "ingredient_count": len(by_ingredient),
    }


# ─── Per-vendor heuristic score (for the existing quotes table) ──────────────

def score_quotes(
    quotes: List[Dict[str, Any]],
    usda_benchmarks: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Light heuristic for the per-vendor row score in the Quote Tracker.

    With multi-vendor optimal carts the *primary* signal is now win-rate
    (how many ingredients this vendor was the cheapest on). We blend it
    with reliability and split-drop ability.

      score = 0.70 * win_rate + 0.15 * reliability + 0.15 * perishable

    win_rate is filled in by the caller via the optional ``win_rate`` key
    (0..1). If absent, we fall back to a price-only proxy so the function
    still works for the legacy single-vendor case.
    """
    if not quotes:
        return []

    has_win_rate = any("win_rate" in q for q in quotes)
    if not has_win_rate:
        prices = [q.get("total_quoted_price") or 0 for q in quotes]
        min_price = min((p for p in prices if p > 0), default=1) or 1

    for q in quotes:
        if has_win_rate:
            primary = float(q.get("win_rate", 0) or 0) * 100
        else:
            price = q.get("total_quoted_price") or 0
            primary = (min_price / price * 100) if price > 0 else 0

        reliability = q.get("reliability_score", 100)
        perishable = 100 if q.get("handles_split_drop", False) else 50

        q["score"] = round(0.70 * primary + 0.15 * reliability + 0.15 * perishable, 2)

    return sorted(quotes, key=lambda q: q["score"], reverse=True)


# ─── Recommendation prompt (multi-vendor aware) ──────────────────────────────

RECOMMENDATION_PROMPT = """\
You are a restaurant procurement advisor reviewing a multi-vendor optimal cart.
Given the JSON below, write a 3-5 sentence recommendation that explains the
proposed split.

Required content:
1. The total cost across all vendors and how many ingredients are covered.
2. Which vendor wins which categories of items (e.g. "Riverbend handles all
   produce, Heritage covers dairy"). Give 1-2 specific examples with prices.
3. Any gap / red flag: ingredients with only one vendor quoting them
   (single-source risk) or items where the spread is unusually small (could
   negotiate further) or large (cheapest is a clear winner).
4. If price-match requests have been auto-sent, mention that the cart may
   improve once vendors respond.

Return ONLY natural language text — no JSON, no markdown, no bullet points.
"""


def generate_recommendation(
    cart_summary: Dict[str, Any],
    *,
    auto_match_sent: int = 0,
    usda_benchmarks: Optional[List[Dict[str, Any]]] = None,
    manager_preferences: str = "",
) -> str:
    """Produce a plain-text recommendation from the multi-vendor cart."""
    by_ingredient = cart_summary.get("by_ingredient") or {}
    by_vendor = cart_summary.get("by_vendor") or {}
    if not by_ingredient or not by_vendor:
        return "No quotes available yet to recommend a cart."

    payload = {
        "grand_total": cart_summary.get("grand_total"),
        "ingredient_count": cart_summary.get("ingredient_count"),
        "auto_price_match_emails_sent": auto_match_sent,
        "vendors": [
            {
                "name": v["distributor_name"],
                "items_quoted": v["items_quoted"],
                "items_won": v["items_won"],
                "won_total": round(v["won_total"], 2),
            }
            for v in by_vendor.values()
        ],
        "ingredients": [
            {
                "name": entry["ingredient_name"],
                "winner": {
                    "vendor": entry["winner"]["distributor_name"],
                    "price": entry["winner"]["unit_price"],
                },
                "runner_up": (
                    {
                        "vendor": entry["runner_up"]["distributor_name"],
                        "price": entry["runner_up"]["unit_price"],
                    }
                    if entry.get("runner_up")
                    else None
                ),
                "single_source": entry.get("runner_up") is None,
            }
            for entry in by_ingredient.values()
        ],
        "manager_preferences": manager_preferences,
        "usda_benchmarks": usda_benchmarks or [],
    }

    response = _client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": RECOMMENDATION_PROMPT},
            {"role": "user", "content": json.dumps(payload)},
        ],
        temperature=0.3,
        max_tokens=400,
    )
    return (response.choices[0].message.content or "").strip()


# ─── Split delivery helper (unchanged) ───────────────────────────────────────

def check_split_delivery(
    ingredient_name: str,
    culinary_qty_needed: float,
    purchasing_qty_lbs: float,
    shelf_life_days: int,
) -> Dict[str, Any]:
    """Returns split-delivery schedule if perishable span exceeds shelf life."""
    daily_usage = culinary_qty_needed / 7 if culinary_qty_needed > 0 else 0
    spanned_days = (purchasing_qty_lbs / daily_usage) if daily_usage > 0 else 999

    if spanned_days > shelf_life_days and shelf_life_days <= 4:
        return {
            "requires_split": True,
            "drop_1_qty": purchasing_qty_lbs / 2,
            "drop_2_qty": purchasing_qty_lbs / 2,
        }
    return {"requires_split": False}
