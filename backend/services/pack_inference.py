"""
Pack-size inference for the procurement RFP.

Restaurants need things like "10 fl oz of pizza sauce per week" but vendors
sell things like "1 × #10 can (~104 fl oz)". Without translating between the
two we end up sending RFPs that ask vendors for "0.38 lb of bacon" or
"10 fl oz of marinara" — both nonsensical at the wholesale level.

This module takes a culinary need (qty + unit) and translates it into a
realistic purchase quantity (number of packs to order, total amount in pack
units, and a human-readable pack label). It deliberately uses no external
data — the rules below are conventional foodservice case sizes that the
caller can override later if a vendor publishes their own pack catalog.

API:
    >>> compute_purchase("Pizza Sauce", "Condiments", 10.0, "fl oz")
    {
        "packs_needed": 1,
        "pack_qty": 104,
        "pack_unit": "fl oz",
        "pack_label": "#10 can (~104 fl oz)",
        "total_in_pack_unit": 104,
        "recipe_need_qty": 10.0,
        "recipe_need_unit": "fl oz",
        "leftover_in_pack_unit": 94.0,
    }

If no pack rule matches, returns ``None`` and the caller should fall back to
showing the raw culinary qty/unit in the RFP.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


# ─── Unit conversion ─────────────────────────────────────────────────────────
#
# Three independent dimensions: mass (canonical=lb), volume (canonical=fl oz),
# and count (canonical=each). Cross-dimension conversion (e.g. lb → fl oz)
# requires a density we don't track, so those return None and the caller
# falls back to the recipe unit.

_TO_LB: Dict[str, float] = {
    "lb": 1.0,
    "oz": 1.0 / 16.0,
    "kg": 2.2046226218,
    "g": 2.2046226218 / 1000.0,
}

_TO_FL_OZ: Dict[str, float] = {
    "fl oz": 1.0,
    "cup": 8.0,
    "tbsp": 0.5,
    "tsp": 1.0 / 6.0,
    "l": 33.8140227,
    "ml": 33.8140227 / 1000.0,
    "gal": 128.0,
    "qt": 32.0,
    "pt": 16.0,
}

_TO_EACH: Dict[str, float] = {
    "each": 1.0,
}


def _normalize_unit(u: Optional[str]) -> str:
    return (u or "").strip().lower()


def convert(qty: float, from_unit: str, to_unit: str) -> Optional[float]:
    """Convert ``qty`` from ``from_unit`` to ``to_unit``. Returns None on
    incompatible units (e.g. mass→volume without a density)."""
    f = _normalize_unit(from_unit)
    t = _normalize_unit(to_unit)
    if not f or not t:
        return None
    if f == t:
        return qty
    for table in (_TO_LB, _TO_FL_OZ, _TO_EACH):
        if f in table and t in table:
            return qty * table[f] / table[t]
    return None


# ─── Pack rules ──────────────────────────────────────────────────────────────
#
# Order matters: most specific keyword wins (longest-match resolution).
# Each rule:
#   - keys:  list of substrings to match against the lower-cased ingredient
#   - qty:   how many units of `unit` are in ONE pack
#   - unit:  the pack's own unit (lb / fl oz / each / etc.)
#   - label: human-readable pack description shown in the RFP

_PACK_RULES: List[Dict[str, Any]] = [
    # ── Cheese / Dairy ───────────────────────────────────────────────────────
    {"keys": ["mozzarella"],            "qty": 5,   "unit": "lb",    "label": "5-lb bag (shredded)"},
    {"keys": ["ricotta"],               "qty": 3,   "unit": "lb",    "label": "3-lb tub"},
    {"keys": ["parmesan", "parmigiano"],"qty": 5,   "unit": "lb",    "label": "5-lb bag (grated)"},
    {"keys": ["cream cheese"],          "qty": 3,   "unit": "lb",    "label": "3-lb block"},
    {"keys": ["goat cheese"],           "qty": 2,   "unit": "lb",    "label": "2-lb log"},
    {"keys": ["feta"],                  "qty": 4,   "unit": "lb",    "label": "4-lb pail"},
    {"keys": ["cheddar"],               "qty": 5,   "unit": "lb",    "label": "5-lb block"},
    {"keys": ["cheese"],                "qty": 5,   "unit": "lb",    "label": "5-lb block"},
    {"keys": ["butter"],                "qty": 1,   "unit": "lb",    "label": "1-lb (4-stick) pack"},
    {"keys": ["heavy cream", "cream"],  "qty": 64,  "unit": "fl oz", "label": "1/2-gal carton (64 fl oz)"},
    {"keys": ["milk"],                  "qty": 128, "unit": "fl oz", "label": "1-gal jug (128 fl oz)"},

    # ── Sauces / Condiments ──────────────────────────────────────────────────
    {"keys": ["pizza sauce", "marinara", "tomato sauce"], "qty": 104, "unit": "fl oz", "label": "#10 can (~104 fl oz)"},
    {"keys": ["alfredo sauce"],         "qty": 64,  "unit": "fl oz", "label": "1/2-gal jug (64 fl oz)"},
    {"keys": ["ranch sauce", "ranch dressing"],         "qty": 128, "unit": "fl oz", "label": "1-gal jug (128 fl oz)"},
    {"keys": ["bbq sauce"],             "qty": 128, "unit": "fl oz", "label": "1-gal jug (128 fl oz)"},
    {"keys": ["buffalo sauce"],         "qty": 128, "unit": "fl oz", "label": "1-gal jug (128 fl oz)"},
    {"keys": ["pesto sauce", "pesto"],  "qty": 32,  "unit": "fl oz", "label": "32-fl-oz jar"},
    {"keys": ["salsa sauce", "salsa"],  "qty": 128, "unit": "fl oz", "label": "1-gal jug (128 fl oz)"},
    {"keys": ["caesar dressing", "dressing"], "qty": 128, "unit": "fl oz", "label": "1-gal jug (128 fl oz)"},
    {"keys": ["hot sauce"],             "qty": 64,  "unit": "fl oz", "label": "64-fl-oz bottle"},
    {"keys": ["honey"],                 "qty": 32,  "unit": "fl oz", "label": "32-fl-oz jug"},
    {"keys": ["olive oil", "oil"],      "qty": 128, "unit": "fl oz", "label": "1-gal jug (128 fl oz)"},

    # ── Proteins ─────────────────────────────────────────────────────────────
    {"keys": ["pepperoni"],             "qty": 25,  "unit": "lb",    "label": "25-lb case (sliced)"},
    {"keys": ["bacon"],                 "qty": 15,  "unit": "lb",    "label": "15-lb case"},
    {"keys": ["ham"],                   "qty": 10,  "unit": "lb",    "label": "10-lb whole"},
    {"keys": ["sausage"],               "qty": 10,  "unit": "lb",    "label": "10-lb tube (bulk)"},
    {"keys": ["chicken breast", "chicken thigh", "chicken"], "qty": 40, "unit": "lb", "label": "40-lb case"},
    {"keys": ["ground beef"],           "qty": 10,  "unit": "lb",    "label": "10-lb chub"},
    {"keys": ["brisket"],               "qty": 12,  "unit": "lb",    "label": "~12-lb whole brisket"},
    {"keys": ["beef"],                  "qty": 10,  "unit": "lb",    "label": "10-lb portion"},
    {"keys": ["shrimp"],                "qty": 5,   "unit": "lb",    "label": "5-lb bag (frozen)"},
    {"keys": ["anchovies", "anchovy"],  "qty": 2,   "unit": "lb",    "label": "2-lb tin (oil-packed)"},

    # ── Produce ──────────────────────────────────────────────────────────────
    {"keys": ["mushroom"],              "qty": 10,  "unit": "lb",    "label": "10-lb case"},
    {"keys": ["red onion", "yellow onion", "onion"], "qty": 50, "unit": "lb", "label": "50-lb sack"},
    {"keys": ["bell pepper", "pepper"], "qty": 10,  "unit": "lb",    "label": "10-lb case"},
    {"keys": ["jalapeno"],              "qty": 10,  "unit": "lb",    "label": "10-lb case"},
    {"keys": ["tomato"],                "qty": 25,  "unit": "lb",    "label": "25-lb case"},
    {"keys": ["spinach"],               "qty": 4,   "unit": "lb",    "label": "4-lb bag (washed)"},
    {"keys": ["romaine lettuce", "lettuce"], "qty": 24, "unit": "each", "label": "case of 24 heads"},
    {"keys": ["pineapple"],             "qty": 6,   "unit": "each",  "label": "case of 6"},
    {"keys": ["pickle"],                "qty": 1,   "unit": "gal",   "label": "1-gal jar"},
    {"keys": ["blueberr"],              "qty": 5,   "unit": "lb",    "label": "5-lb flat (fresh)"},
    {"keys": ["strawberr"],             "qty": 5,   "unit": "lb",    "label": "5-lb flat (fresh)"},
    {"keys": ["apple"],                 "qty": 40,  "unit": "lb",    "label": "40-lb case"},
    {"keys": ["zucchini"],              "qty": 20,  "unit": "lb",    "label": "20-lb case"},
    {"keys": ["potato"],                "qty": 50,  "unit": "lb",    "label": "50-lb sack"},
    {"keys": ["basil", "parsley", "cilantro", "herb"], "qty": 1, "unit": "lb", "label": "1-lb fresh herb bunch"},
    {"keys": ["croutons"],              "qty": 5,   "unit": "lb",    "label": "5-lb bag"},

    # ── Bakery / Dough ───────────────────────────────────────────────────────
    {"keys": ["pizza dough"],           "qty": 24,  "unit": "each",  "label": "case of 24 dough balls"},
    {"keys": ["bread", "bun", "roll"],  "qty": 12,  "unit": "each",  "label": "12-pack"},
    {"keys": ["breadcrumb"],            "qty": 25,  "unit": "lb",    "label": "25-lb bag"},

    # ── Dry goods ────────────────────────────────────────────────────────────
    {"keys": ["all purpose flour", "flour"], "qty": 50, "unit": "lb", "label": "50-lb sack"},
    {"keys": ["sugar"],                 "qty": 50,  "unit": "lb",    "label": "50-lb sack"},
    {"keys": ["chocolate chip"],        "qty": 25,  "unit": "lb",    "label": "25-lb case"},
    {"keys": ["cocoa"],                 "qty": 5,   "unit": "lb",    "label": "5-lb bag"},
    {"keys": ["penne", "spaghetti", "pasta"], "qty": 20, "unit": "lb", "label": "20-lb case (dry)"},

    # ── Pantry ───────────────────────────────────────────────────────────────
    {"keys": ["vanilla extract", "vanilla"], "qty": 32, "unit": "fl oz", "label": "32-fl-oz extract bottle"},
    {"keys": ["salt"],                  "qty": 25,  "unit": "lb",    "label": "25-lb bag"},
    {"keys": ["caramel"],               "qty": 64,  "unit": "fl oz", "label": "1/2-gal jug (64 fl oz)"},
    {"keys": ["chocolate"],             "qty": 5,   "unit": "lb",    "label": "5-lb block"},

    # ── Eggs ─────────────────────────────────────────────────────────────────
    {"keys": ["egg"],                   "qty": 360, "unit": "each",  "label": "30-doz case (360 eggs)"},

    # ── Drinks (recipe unit "each" = bottles/cans, "fl oz" = fountain) ───────
    # Two parallel rules per drink size: one in `each` (for menus that parse
    # drinks as bottles) and one in `fl oz` (for menus that parse drinks as
    # fountain volume). `infer_pack` picks the one compatible with the recipe.
    {"keys": ["2-l drink", "2l drink"], "qty": 8,   "unit": "each",  "label": "case of 8 × 2-L bottles"},
    {"keys": ["2-l drink", "2l drink"], "qty": 16,  "unit": "l",     "label": "case of 8 × 2-L bottles (16 L)"},
    {"keys": ["20oz drink"],            "qty": 24,  "unit": "each",  "label": "case of 24 × 20-oz bottles"},
    {"keys": ["20oz drink"],            "qty": 480, "unit": "fl oz", "label": "case of 24 × 20-oz bottles (480 fl oz)"},
    {"keys": ["16oz drink"],            "qty": 24,  "unit": "each",  "label": "case of 24 × 16-oz cans"},
    {"keys": ["16oz drink"],            "qty": 384, "unit": "fl oz", "label": "case of 24 × 16-oz cans (384 fl oz)"},
    {"keys": ["12oz drink"],            "qty": 24,  "unit": "each",  "label": "case of 24 × 12-oz cans"},
    {"keys": ["12oz drink"],            "qty": 288, "unit": "fl oz", "label": "case of 24 × 12-oz cans (288 fl oz)"},
    {"keys": ["water"],                 "qty": 24,  "unit": "each",  "label": "case of 24 × 16.9-oz bottles"},
    {"keys": ["drink", "soda", "juice"],"qty": 24,  "unit": "each",  "label": "case of 24"},
]


def infer_pack(
    name: str,
    category: Optional[str] = None,
    recipe_unit: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return the best matching pack rule, or None if nothing matches.

    Resolution order:
      1. Filter to rules whose keyword appears in the ingredient name.
      2. If ``recipe_unit`` is given, prefer rules whose pack unit is
         convertible from it (so a recipe in ``fl oz`` matches a pack
         priced in ``fl oz`` rather than one priced in ``each``).
      3. Among the remaining candidates, the longest keyword wins
         (``"pizza sauce"`` beats a generic ``"sauce"``).
    """
    if not name:
        return None
    needle = name.lower()
    matches: List[Dict[str, Any]] = []
    for rule in _PACK_RULES:
        for key in rule["keys"]:
            if key in needle:
                matches.append({**rule, "_match_len": len(key)})
                break
    if not matches:
        return None

    if recipe_unit:
        compatible = [
            m for m in matches
            if convert(1.0, recipe_unit, m["unit"]) is not None
        ]
        if compatible:
            matches = compatible

    return max(matches, key=lambda r: r["_match_len"])


# ─── Public API ──────────────────────────────────────────────────────────────

def compute_purchase(
    name: str,
    category: Optional[str],
    culinary_qty: float,
    culinary_unit: str,
    *,
    override_qty: Optional[float] = None,
    override_unit: Optional[str] = None,
    override_label: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Translate a culinary need into a pack-rounded purchase plan.

    Resolution priority:
      1. **Override** — if all three of ``override_qty`` / ``override_unit`` /
         ``override_label`` are provided (typically from
         ``Ingredient.pack_*_override``), use them and skip inference.
      2. **Inferred default** — keyword + unit-compatibility match against
         ``_PACK_RULES``.
      3. **None** — no rule matches, or unit is incompatible. Caller should
         fall back to the raw culinary qty/unit.

    The returned dict carries a ``source`` field (``"override"`` or
    ``"inferred"``) so the caller can persist provenance.
    """
    if culinary_qty is None or culinary_qty <= 0:
        return None

    if override_qty and override_unit and override_label:
        rule = {
            "qty": float(override_qty),
            "unit": override_unit,
            "label": override_label,
        }
        source = "override"
    else:
        inferred = infer_pack(name, category, culinary_unit)
        if not inferred:
            return None
        rule = inferred
        source = "inferred"

    pack_qty = float(rule["qty"])
    pack_unit = rule["unit"]
    pack_label = rule["label"]

    qty_in_pack_unit = convert(culinary_qty, culinary_unit, pack_unit)
    if qty_in_pack_unit is None:
        # Recipe unit can't be reconciled with the pack unit (e.g. recipe in
        # lb but pack measured in fl oz, no density known). Skip the pack
        # translation rather than guess.
        return None

    packs_needed = max(1, math.ceil(qty_in_pack_unit / pack_qty))
    total_in_pack_unit = packs_needed * pack_qty
    leftover = round(total_in_pack_unit - qty_in_pack_unit, 2)

    return {
        "packs_needed": packs_needed,
        "pack_qty": pack_qty,
        "pack_unit": pack_unit,
        "pack_label": pack_label,
        "total_in_pack_unit": total_in_pack_unit,
        "leftover_in_pack_unit": leftover,
        "recipe_need_qty": float(culinary_qty),
        "recipe_need_unit": culinary_unit,
        "source": source,
    }


def format_order_cell(plan: Optional[Dict[str, Any]]) -> str:
    """Render the pack plan as a single 'Order This' cell for the RFP table."""
    if not plan:
        return ""
    n = plan["packs_needed"]
    return f"{n} × {plan['pack_label']}"


def format_recipe_need_cell(plan: Optional[Dict[str, Any]], fallback_qty: float, fallback_unit: str) -> str:
    """Render the 'Recipe Need' cell. Falls back to raw culinary qty/unit
    when no pack plan was inferred so the column is never blank."""
    if plan:
        return f"{plan['recipe_need_qty']:.2f} {plan['recipe_need_unit']}"
    return f"{fallback_qty:.2f} {fallback_unit}"
