"""
Canonical culinary units for aggregation and vendor-facing qty:

  • Mass   → lb
  • Volume → fl oz (US)
  • Count  → each

Parsed menu rows are normalized before persistence so procurement rollups stay consistent.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# Aliases → internal bucket before canonical conversion
_CANON_ALIASES: Dict[str, str] = {
    "lb": "lb",
    "lbs": "lb",
    "pound": "lb",
    "pounds": "lb",
    "#": "lb",
    "oz": "oz",
    "ounce": "oz",
    "ounces": "oz",
    "g": "g",
    "gram": "g",
    "grams": "g",
    "kg": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "ml": "ml",
    "milliliter": "ml",
    "milliliters": "ml",
    "l": "l",
    "liter": "l",
    "liters": "l",
    "litre": "l",
    "litres": "l",
    "fl oz": "fl oz",
    "floz": "fl oz",
    "fluid oz": "fl oz",
    "fluid ounce": "fl oz",
    "fluid ounces": "fl oz",
    "cup": "cup",
    "cups": "cup",
    "tbsp": "tbsp",
    "tablespoon": "tbsp",
    "tablespoons": "tbsp",
    "tsp": "tsp",
    "teaspoon": "tsp",
    "teaspoons": "tsp",
    "each": "each",
    "ea": "each",
    "unit": "each",
    "units": "each",
    "piece": "each",
    "pieces": "each",
    "whole": "each",
    "bottle": "each",
    "bottles": "each",
    "can": "each",
    "cans": "each",
}

_GRAMS_PER_LB = 453.59237

# When the model says "portion" or omits unit, use category heuristics (per ONE serving).
_DEFAULT_QTY_OZ_MASS: Dict[str, float] = {
    "Bakery": 10.0,
    "Dairy": 6.0,
    "Proteins": 4.5,
    "Produce": 3.0,
    "Dry Goods": 3.0,
    "Pantry": 2.0,
    "Frozen": 5.0,
}
_DEFAULT_FL_OZ_CONDIMENT = 3.0


def _normalize_unit_token(unit: Optional[str]) -> Optional[str]:
    if unit is None:
        return None
    s = str(unit).strip().lower()
    s = re.sub(r"\s+", " ", s)
    if not s:
        return None
    return _CANON_ALIASES.get(s, s)


def _coerce_q(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    try:
        q = float(raw)
    except (TypeError, ValueError):
        return None
    if q < 0 or q > 1_000_000:
        return None
    return q


def _to_lb(q: float, bucket: str) -> float:
    if bucket == "lb":
        return q
    if bucket == "oz":
        return q / 16.0
    if bucket == "g":
        return q / _GRAMS_PER_LB
    if bucket == "kg":
        return q * 1000.0 / _GRAMS_PER_LB
    raise ValueError(bucket)


def _to_fl_oz(q: float, bucket: str) -> float:
    if bucket == "fl oz":
        return q
    if bucket == "ml":
        return q / 29.5735295625
    if bucket == "l":
        return (q * 1000.0) / 29.5735295625
    if bucket == "cup":
        return q * 8.0
    if bucket == "tbsp":
        return q * 0.5
    if bucket == "tsp":
        return q / 6.0
    raise ValueError(bucket)


def _default_mass_for_category(cat: Optional[str]) -> Tuple[float, str]:
    c = (cat or "").strip()
    oz = _DEFAULT_QTY_OZ_MASS.get(c, 5.0)
    return _to_lb(oz, "oz"), "lb"


def _default_volume_condiment() -> float:
    return _to_fl_oz(_DEFAULT_FL_OZ_CONDIMENT, "fl oz")


def canonicalize_ingredient_row(
    ing: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Return a copy of ing with q + unit in canonical (lb | fl oz | each), or None to drop line.
    """
    name = (ing.get("name") or "").strip()
    if not name:
        return None

    cat = ing.get("cat")
    if isinstance(cat, str):
        cat = cat.strip()
    else:
        cat = None

    q = _coerce_q(ing.get("q"))
    unit_key = _normalize_unit_token(ing.get("unit"))

    # Vague "portion" / missing unit → category heuristics
    if unit_key in (None, "portion", "portions", "serving", "servings"):
        if cat == "Condiments":
            base = _default_volume_condiment()
            mult = 1.0 if q is None else q
            return {**ing, "q": round(base * mult, 6), "unit": "fl oz"}
        if cat == "Bakery":
            qn = 1.0 if q is None else q
            return {**ing, "q": qn, "unit": "each"}
        lb_amt, _ = _default_mass_for_category(cat)
        mult = 1.0 if q is None else q
        return {**ing, "q": round(lb_amt * mult, 6), "unit": "lb"}

    if q is None:
        q = 1.0

    if unit_key == "each":
        return {**ing, "q": q, "unit": "each"}

    if unit_key in ("oz", "lb", "g", "kg"):
        lb_amt = _to_lb(q, unit_key)
        return {**ing, "q": round(lb_amt, 6), "unit": "lb"}

    if unit_key in ("ml", "l", "fl oz", "cup", "tbsp", "tsp"):
        floz = _to_fl_oz(q, unit_key)
        return {**ing, "q": round(floz, 6), "unit": "fl oz"}

    # Unknown token — assume avoirdupois oz
    try:
        lb_amt = _to_lb(q, "oz")
        return {**ing, "q": round(lb_amt, 6), "unit": "lb"}
    except ValueError:
        return None


def sanitize_dish_ingredients(ingredients: Any) -> List[Dict[str, Any]]:
    if not isinstance(ingredients, list):
        return []
    out: List[Dict[str, Any]] = []
    for raw in ingredients:
        if not isinstance(raw, dict):
            continue
        row = canonicalize_ingredient_row(raw)
        if row:
            out.append(row)
    return out


def sanitize_menu_dishes(dishes: Any) -> List[Dict[str, Any]]:
    if not isinstance(dishes, list):
        return []
    result: List[Dict[str, Any]] = []
    for d in dishes:
        if not isinstance(d, dict):
            continue
        name = (d.get("name") or "").strip()
        if not name:
            continue
        ing = sanitize_dish_ingredients(d.get("ingredients"))
        entry = {**d, "ingredients": ing}
        result.append(entry)
    return result


def apply_sanitized_dishes(data: Dict[str, Any]) -> Dict[str, Any]:
    return {**data, "dishes": sanitize_menu_dishes(data.get("dishes"))}
