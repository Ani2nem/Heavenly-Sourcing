"""
USDA FoodData Central client + benchmark builder.

Two distinct data sources in play:

  1. **USDA FoodData Central (FDC)** — https://api.nal.usda.gov/fdc/v1/
     Nutrition + food identifiers ONLY. No prices. We use it to resolve and
     persist `Ingredient.usda_fdc_id` so menus link to a stable USDA reference.

  2. **USDA AMS Market News (MARS)** — see `services/ams_pricing.py`.
     Real wholesale price data per commodity (mozzarella, cheese, butter,
     beef, chicken, etc.). Mapped through `INGREDIENT_TO_AMS` and stored as
     `IngredientPrice` rows in the DB.

Benchmark resolution priority (used in the RFP "Reference Benchmark" column):

  1. **Real USDA AMS** — if the ingredient has stored `IngredientPrice` rows,
     use the recent average in USDA's published unit (lb / gal / head / each).
     Labelled honestly as `(USDA AMS, <date>)`.

  2. **Category estimate** — only if the recipe's unit is mass-compatible
     (lb / oz / g / kg) so a `$/lb` number doesn't appear next to fl oz of
     sauce or 2-L drinks. Labelled as `(industry est)` — NOT USDA.

  3. **No signal** — render as `—`. Better silent than wrong.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from config import settings

FDC_BASE = "https://api.nal.usda.gov/fdc/v1"

# Heuristic regional benchmark prices ($/lb) by category. These are NOT from
# any USDA API — they are static editorial estimates used only when (a) we
# have no real AMS data for the ingredient and (b) the recipe unit is mass
# based. Tag any rendered string with "(industry est)" so the RFP doesn't
# misattribute them to USDA.
_CATEGORY_BENCHMARK_PER_LB: Dict[str, float] = {
    "Dairy": 4.50,
    "Proteins": 6.00,
    "Produce": 2.50,
    "Bakery": 1.80,
    "Condiments": 5.00,
    "Dry Goods": 1.20,
    "Pantry": 4.00,
    "Frozen": 4.50,
}

# Recipe units for which a category $/lb estimate is at least defensible.
# For everything else (fl oz, ml, l, cup, tbsp, tsp, each) we render `—`.
_MASS_UNITS = {"lb", "oz", "g", "kg"}


# Per-ingredient overrides for items that have no AMS commodity match AND
# whose natural recipe unit isn't mass-based, so the category $/lb fallback
# doesn't fire. These are hand-curated industry midpoints in the
# ingredient's *own* unit — pizza dough is sold by the ball, sauces by
# fluid ounce out of a #10 can / 1-gal jug. Same honesty rules as the
# category table: NOT USDA-sourced, rendered with `industry est`, NEVER
# tagged as USDA.
#
# Match is case-insensitive substring against the ingredient name, so
# "Whole-Wheat Pizza Dough" still hits `pizza dough`. Keep the keys narrow
# enough that they don't collide with unrelated dishes.
_INGREDIENT_OVERRIDES: Dict[str, Dict[str, Any]] = {
    # Bakery, per ball (~9–12 oz raw)
    "pizza dough":     {"value": 0.60, "unit": "each"},
    # Condiments, per fluid ounce. Ballpark wholesale (per #10 can ≈ 104 fl oz
    # or 1-gal jug ≈ 128 fl oz):
    #   pizza sauce    ~$6  / #10 can          → $0.06/fl oz
    #   marinara sauce ~$8  / #10 can          → $0.08/fl oz
    #   salsa sauce    ~$8  / #10 can          → $0.08/fl oz
    #   bbq sauce      ~$11 / 1-gal jug        → $0.09/fl oz
    #   buffalo sauce  ~$11 / 1-gal jug        → $0.09/fl oz
    #   hot sauce      ~$9  / 1-gal jug        → $0.07/fl oz
    #   ranch sauce    ~$13 / 1-gal jug        → $0.10/fl oz
    #   alfredo sauce  ~$17 / 1-gal jug (premium dairy) → $0.13/fl oz
    #   pesto sauce    ~$23 / 1-gal jug (oil + basil + cheese) → $0.18/fl oz
    "pizza sauce":     {"value": 0.06, "unit": "fl oz"},
    "marinara sauce":  {"value": 0.08, "unit": "fl oz"},
    "salsa sauce":     {"value": 0.08, "unit": "fl oz"},
    "bbq sauce":       {"value": 0.09, "unit": "fl oz"},
    "buffalo sauce":   {"value": 0.09, "unit": "fl oz"},
    "hot sauce":       {"value": 0.07, "unit": "fl oz"},
    "ranch sauce":     {"value": 0.10, "unit": "fl oz"},
    "alfredo sauce":   {"value": 0.13, "unit": "fl oz"},
    "pesto sauce":     {"value": 0.18, "unit": "fl oz"},
}


def _lookup_ingredient_override(name: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return the (value, unit) override for an ingredient name, or None.

    Substring match — case-insensitive — against ``_INGREDIENT_OVERRIDES``
    so variations like "Homemade Pizza Sauce" still hit the right entry.
    Longest matching key wins so "alfredo sauce" beats "sauce" if we ever
    add a generic entry.
    """
    if not name:
        return None
    low = name.lower().strip()
    hits = [(k, v) for k, v in _INGREDIENT_OVERRIDES.items() if k in low]
    if not hits:
        return None
    return max(hits, key=lambda kv: len(kv[0]))[1]


def search_fdc_id(name: str) -> Optional[str]:
    """Return the top-ranked USDA FDC id for an ingredient name, or None.

    Uses the **POST** form of the FDC search endpoint with a JSON body. The
    GET form returns intermittent HTTP 400s from USDA's nginx layer for
    multi-word queries (e.g. "Pizza Sauce", "Cream Cheese"), regardless of
    whether `dataType` is a list or a comma-string — verified empirically
    against the live API. The POST form returns 200 reliably.
    """
    if not settings.usda_api_key or not name:
        return None
    try:
        resp = requests.post(
            f"{FDC_BASE}/foods/search",
            params={"api_key": settings.usda_api_key},
            json={
                "query": name,
                "pageSize": 1,
                "dataType": ["Foundation", "SR Legacy", "Survey (FNDDS)"],
            },
            timeout=10,
        )
        if resp.status_code != 200:
            # Surface non-200 separately from network errors so future
            # regressions don't get swallowed under a blanket Exception.
            print(
                f"[usda] FDC search HTTP {resp.status_code} for {name!r}: "
                f"{resp.text[:200]}"
            )
            return None
        data = resp.json() or {}
        foods: List[Dict[str, Any]] = data.get("foods") or []
        if not foods:
            return None
        fdc_id = foods[0].get("fdcId")
        return str(fdc_id) if fdc_id is not None else None
    except Exception as exc:
        print(f"[usda] FDC search failed for {name!r}: {exc}")
        return None


def get_benchmark_price_per_lb(name: str, category: Optional[str] = None) -> Optional[float]:
    """Return a static category $/lb estimate (None if no category match).

    NOTE: This is NOT USDA data. Callers should label any rendered output as
    "industry est", never "USDA".
    """
    if category and category in _CATEGORY_BENCHMARK_PER_LB:
        return _CATEGORY_BENCHMARK_PER_LB[category]
    return None


def _format_ams_date(latest: Optional[Dict[str, Any]]) -> str:
    if not latest:
        return ""
    raw = latest.get("as_of_date")
    if not raw:
        return ""
    # Stored as YYYY-MM-DD. Render as e.g. "Apr 28".
    try:
        from datetime import datetime
        return datetime.strptime(str(raw)[:10], "%Y-%m-%d").strftime("%b %d")
    except Exception:
        return str(raw)


def build_benchmarks(
    ingredients: List[Dict[str, Any]],
    *,
    session: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Build per-ingredient benchmark records for the RFP / scoring engine.

    Each input dict must include at least ``name``, ``category``, and ``unit``
    (the recipe unit). If ``session`` and ``ingredient_id`` are provided we
    look up real USDA AMS Market News data first.

    Returned dicts include:
      - ``name``, ``fdc_id``, ``category``
      - ``source``: ``"ams"`` | ``"category"`` | ``None``
      - ``value``: numeric price (None if no signal)
      - ``unit``: unit the value is denominated in (None if no signal)
      - ``label``: pre-rendered display string for the RFP (None if no signal)
      - ``benchmark_per_lb``: legacy field kept for backward compat (None when
        the value is not in $/lb).
    """
    # Local import to avoid a circular reference at module load time.
    summarize_fn = None
    if session is not None:
        try:
            from services.ams_pricing import summarize_ingredient_prices as _sum
            summarize_fn = _sum
        except Exception as exc:
            print(f"[usda] could not import ams_pricing: {exc}")

    out: List[Dict[str, Any]] = []
    for item in ingredients:
        name = item.get("name") or ""
        cat = item.get("category")
        recipe_unit = (item.get("unit") or "").strip().lower()
        ing_id = item.get("ingredient_id")
        fdc_id = item.get("fdc_id")

        record: Dict[str, Any] = {
            "name": name,
            "fdc_id": fdc_id,
            "category": cat,
            "source": None,
            "value": None,
            "unit": None,
            "label": None,
            "benchmark_per_lb": None,
        }

        # ── 1. Real USDA AMS Market News ─────────────────────────────────────
        if summarize_fn is not None and ing_id is not None:
            try:
                summary = summarize_fn(session, ing_id) or {}
            except Exception as exc:
                print(f"[usda] AMS summary failed for {name!r}: {exc}")
                summary = {}
            if summary.get("has_data") and summary.get("avg") is not None:
                ams_unit = (summary.get("unit") or "lb").strip()
                avg = float(summary["avg"])
                date_str = _format_ams_date(summary.get("latest"))
                date_tag = f", {date_str}" if date_str else ""
                record.update({
                    "source": "ams",
                    "value": avg,
                    "unit": ams_unit,
                    "label": f"${avg:.2f}/{ams_unit} (USDA AMS{date_tag})",
                    "benchmark_per_lb": avg if ams_unit == "lb" else None,
                })
                out.append(record)
                continue

        # ── 2a. Per-ingredient override (named items not on AMS, not in lb) ──
        # Pizza dough is sold by the ball, sauces by fluid ounce — neither
        # passes the mass-unit gate below, so a name-keyed override lets us
        # surface an honest industry estimate in the item's own unit.
        override = _lookup_ingredient_override(name)
        if override is not None:
            v = float(override["value"])
            u = override["unit"]
            cat_tag = (name or "").lower().strip() or (cat.lower() if cat else "estimate")
            record.update({
                "source": "category",
                "value": v,
                "unit": u,
                "label": f"~${v:.2f}/{u} (industry est, {cat_tag})",
                "benchmark_per_lb": None,
                "category": cat_tag,
            })
            out.append(record)
            continue

        # ── 2b. Category estimate — gated on mass-compatible recipe unit ─────
        # For fl oz / each / cup / etc. a $/lb number is more misleading than
        # helpful; render as "—" instead.
        if cat and cat in _CATEGORY_BENCHMARK_PER_LB and recipe_unit in _MASS_UNITS:
            est = _CATEGORY_BENCHMARK_PER_LB[cat]
            cat_tag = cat.lower()
            record.update({
                "source": "category",
                "value": est,
                "unit": "lb",
                "label": f"~${est:.2f}/lb (industry est, {cat_tag})",
                "benchmark_per_lb": est,
            })

        out.append(record)

    return out
