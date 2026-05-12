"""
USDA AMS Market News (MARS) pricing client.

Sign up for an API key: https://mymarketnews.ams.usda.gov/mymarketnews-api/api-key
Set AMS_API_KEY in backend/.env.

Strategy
========

AMS publishes ~thousands of reports identified by numeric `slug_id` whose row
shapes vary (some have only volumes / holdings, others have prices). Hard-coding
slugs is brittle, so we:

  1. List all reports once at process start (cached) via /services/v1.2/reports.
  2. For each ingredient, look up candidate reports whose title or commodity
     metadata matches the ingredient keyword AND whose title hints at prices.
  3. Fetch a small recent slice of each candidate; keep rows that include
     recognisable price columns (price_low, price_high, weighted_avg_price,
     average_price, etc.). Skip rows that only contain volumes / holdings.
  4. Cache the discovered (commodity → slug_id) mapping so future fetches are
     a single HTTP call.

The endpoint shape is:
    GET https://marsapi.ams.usda.gov/services/v1.2/reports[/{slug_id}]
    Auth: HTTP Basic, username = AMS_API_KEY, password = ""

Configuration
=============

`INGREDIENT_TO_AMS` is a small list of {match, commodity_keyword, unit, region}
entries. The match string is a substring searched in the ingredient name; the
commodity keyword drives report discovery and per-row commodity filtering.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.auth import HTTPBasicAuth
from urllib.parse import quote as _urlquote

from config import settings

log = logging.getLogger(__name__)

MARS_BASE = "https://marsapi.ams.usda.gov/services/v1.2/reports"

# How long a commodity that returned 0 usable rows stays cached as "no data"
# before we'll try discovering it again. Previously this was a permanent
# in-memory set, which meant one transient failure poisoned the family until
# the API process restarted. A TTL means we self-heal.
_NEGATIVE_TTL_SECONDS = 60 * 60  # 1 hour

# Cap on candidate reports examined per discovery pass. AMS publishes a few
# thousand reports; for keywords like "fruit" or "terminal market" the top
# 8 results sometimes don't include the right slug, so we widen the funnel.
_CANDIDATE_REPORT_CAP = 20


# ─── Ingredient → AMS report mapping ─────────────────────────────────────────
#
# Field meanings (this took a few iterations against live AMS data to get right):
#
#   match            – substring match against the user's ingredient name
#                      ("mozzarella" matches "Mozzarella Cheese", "Whole Milk
#                      Mozzarella", etc.). Longest match wins.
#
#   report_keyword   – substring AMS uses in REPORT TITLES / market_types to
#                      find the right report. e.g. mozzarella lives inside
#                      "Cheese - Central U.S." (not in any report titled
#                      "Mozzarella"), so the report keyword is "cheese".
#
#   row_match        – list of substrings searched against a row's commodity
#                      / group / category / description columns. AMS dairy
#                      reports have commodity="Cheese" with the actual cheese
#                      type in the `group` column ("Mozzarella", "Cheddar"…),
#                      so we have to widen the per-row filter.
#
#   unit             – display unit for the price column ($X/lb, $X/gal…).
#
#   region_hint      – optional preference for "national"/region in candidate
#                      ranking (small score boost).
#
INGREDIENT_TO_AMS: List[Dict[str, Any]] = [
    # ── Cheese (national AMS dairy reports have rich coverage) ────────────────
    {"match": "mozzarella",    "report_keyword": "cheese",      "row_match": ["mozzarella"],                "unit": "lb"},
    {"match": "ricotta",       "report_keyword": "cheese",      "row_match": ["ricotta"],                   "unit": "lb"},
    {"match": "mascarpone",    "report_keyword": "cheese",      "row_match": ["mascarpone", "italian"],     "unit": "lb"},
    {"match": "cream cheese",  "report_keyword": "cheese",      "row_match": ["cream cheese"],              "unit": "lb"},
    {"match": "cottage cheese","report_keyword": "cheese",      "row_match": ["cottage"],                   "unit": "lb"},
    {"match": "goat cheese",   "report_keyword": "cheese",      "row_match": ["goat", "chevre"],            "unit": "lb"},
    {"match": "cheddar",       "report_keyword": "cheese",      "row_match": ["cheddar"],                   "unit": "lb"},
    {"match": "provolone",     "report_keyword": "cheese",      "row_match": ["provolone", "italian"],      "unit": "lb"},
    {"match": "gouda",         "report_keyword": "cheese",      "row_match": ["gouda"],                     "unit": "lb"},
    {"match": "havarti",       "report_keyword": "cheese",      "row_match": ["havarti"],                   "unit": "lb"},
    {"match": "feta",          "report_keyword": "cheese",      "row_match": ["feta"],                      "unit": "lb"},
    {"match": "brie",          "report_keyword": "cheese",      "row_match": ["brie"],                      "unit": "lb"},
    {"match": "camembert",     "report_keyword": "cheese",      "row_match": ["camembert"],                 "unit": "lb"},
    {"match": "blue cheese",   "report_keyword": "cheese",      "row_match": ["blue"],                      "unit": "lb"},
    {"match": "gorgonzola",    "report_keyword": "cheese",      "row_match": ["gorgonzola", "blue"],        "unit": "lb"},
    {"match": "gruyere",       "report_keyword": "cheese",      "row_match": ["gruyere", "swiss"],          "unit": "lb"},
    {"match": "fontina",       "report_keyword": "cheese",      "row_match": ["fontina", "italian"],        "unit": "lb"},
    {"match": "asiago",        "report_keyword": "cheese",      "row_match": ["asiago", "italian"],         "unit": "lb"},
    {"match": "romano",        "report_keyword": "cheese",      "row_match": ["romano", "italian"],         "unit": "lb"},
    {"match": "halloumi",      "report_keyword": "cheese",      "row_match": ["halloumi"],                  "unit": "lb"},
    {"match": "swiss",         "report_keyword": "cheese",      "row_match": ["swiss"],                     "unit": "lb"},
    {"match": "monterey",      "report_keyword": "cheese",      "row_match": ["monterey"],                  "unit": "lb"},
    {"match": "muenster",      "report_keyword": "cheese",      "row_match": ["muenster"],                  "unit": "lb"},
    {"match": "parmesan",      "report_keyword": "cheese",      "row_match": ["parmesan", "italian"],       "unit": "lb"},
    {"match": "parmigiano",    "report_keyword": "cheese",      "row_match": ["parmigiano", "parmesan", "italian"], "unit": "lb"},
    {"match": "pecorino",      "report_keyword": "cheese",      "row_match": ["pecorino", "italian"],       "unit": "lb"},
    {"match": "cheese",        "report_keyword": "cheese",      "row_match": ["cheese"],                    "unit": "lb"},

    # ── Other dairy ───────────────────────────────────────────────────────────
    # "buttermilk" MUST be its own entry so word-boundary matching beats "milk"
    # for things like "Buttermilk Powder".
    {"match": "buttermilk",    "report_keyword": "buttermilk",  "row_match": ["buttermilk"],                "unit": "lb"},
    {"match": "butter",        "report_keyword": "butter",      "row_match": ["butter"],                    "unit": "lb"},
    {"match": "yogurt",        "report_keyword": "yogurt",      "row_match": ["yogurt", "yoghurt"],         "unit": "lb"},
    {"match": "yoghurt",       "report_keyword": "yogurt",      "row_match": ["yogurt", "yoghurt"],         "unit": "lb"},
    {"match": "sour cream",    "report_keyword": "cream",       "row_match": ["sour cream"],                "unit": "lb"},
    {"match": "heavy cream",   "report_keyword": "cream",       "row_match": ["cream"],                     "unit": "lb"},
    {"match": "half and half", "report_keyword": "cream",       "row_match": ["half"],                      "unit": "lb"},
    {"match": "cream",         "report_keyword": "cream",       "row_match": ["cream"],                     "unit": "lb"},
    {"match": "milk",          "report_keyword": "milk",        "row_match": ["milk"],                      "unit": "lb"},

    # ── Eggs ──────────────────────────────────────────────────────────────────
    {"match": "egg",           "report_keyword": "egg",         "row_match": ["egg"],                       "unit": "dozen"},

    # ── Produce: terminal markets carry most of the row-level commodities ─────
    {"match": "tomato",        "report_keyword": "terminal market", "row_match": ["tomato"],                "unit": "lb"},
    {"match": "onion",         "report_keyword": "onions and potatoes", "row_match": ["onion"],             "unit": "lb"},
    {"match": "potato",        "report_keyword": "onions and potatoes", "row_match": ["potato"],            "unit": "lb"},
    {"match": "garlic",        "report_keyword": "terminal market", "row_match": ["garlic"],                "unit": "lb"},
    {"match": "ginger",        "report_keyword": "terminal market", "row_match": ["ginger"],                "unit": "lb"},
    {"match": "shallot",       "report_keyword": "terminal market", "row_match": ["shallot"],               "unit": "lb"},
    {"match": "leek",          "report_keyword": "terminal market", "row_match": ["leek"],                  "unit": "lb"},
    {"match": "lettuce",       "report_keyword": "terminal market", "row_match": ["lettuce"],               "unit": "head"},
    {"match": "romaine",       "report_keyword": "terminal market", "row_match": ["romaine", "lettuce"],    "unit": "head"},
    {"match": "spinach",       "report_keyword": "terminal market", "row_match": ["spinach"],               "unit": "lb"},
    {"match": "arugula",       "report_keyword": "terminal market", "row_match": ["arugula"],               "unit": "lb"},
    {"match": "kale",          "report_keyword": "terminal market", "row_match": ["kale"],                  "unit": "lb"},
    {"match": "cabbage",       "report_keyword": "terminal market", "row_match": ["cabbage"],               "unit": "lb"},
    {"match": "broccoli",      "report_keyword": "terminal market", "row_match": ["broccoli"],              "unit": "lb"},
    {"match": "cauliflower",   "report_keyword": "terminal market", "row_match": ["cauliflower"],           "unit": "lb"},
    {"match": "carrot",        "report_keyword": "terminal market", "row_match": ["carrot"],                "unit": "lb"},
    {"match": "celery",        "report_keyword": "terminal market", "row_match": ["celery"],                "unit": "lb"},
    {"match": "cucumber",      "report_keyword": "terminal market", "row_match": ["cucumber"],              "unit": "lb"},
    {"match": "zucchini",      "report_keyword": "terminal market", "row_match": ["zucchini", "squash"],    "unit": "lb"},
    {"match": "squash",        "report_keyword": "terminal market", "row_match": ["squash"],                "unit": "lb"},
    {"match": "eggplant",      "report_keyword": "terminal market", "row_match": ["eggplant"],              "unit": "lb"},
    {"match": "asparagus",     "report_keyword": "terminal market", "row_match": ["asparagus"],             "unit": "lb"},
    {"match": "green bean",    "report_keyword": "terminal market", "row_match": ["green bean", "snap bean"], "unit": "lb"},
    {"match": "mushroom",      "report_keyword": "terminal market", "row_match": ["mushroom"],              "unit": "lb"},
    {"match": "bell pepper",   "report_keyword": "terminal market", "row_match": ["bell pepper", "peppers"], "unit": "lb"},
    {"match": "jalapeno",      "report_keyword": "terminal market", "row_match": ["jalapeno", "chile"],     "unit": "lb"},
    {"match": "pepper",        "report_keyword": "terminal market", "row_match": ["pepper"],                "unit": "lb"},
    {"match": "corn",          "report_keyword": "terminal market", "row_match": ["corn"],                  "unit": "lb"},
    {"match": "olive",         "report_keyword": "terminal market", "row_match": ["olive"],                 "unit": "lb"},

    # ── Herbs (terminal market reports group these under "herbs") ─────────────
    {"match": "basil",         "report_keyword": "terminal market", "row_match": ["basil", "herb"],         "unit": "lb"},
    {"match": "cilantro",      "report_keyword": "terminal market", "row_match": ["cilantro", "herb"],      "unit": "lb"},
    {"match": "parsley",       "report_keyword": "terminal market", "row_match": ["parsley", "herb"],       "unit": "lb"},
    {"match": "mint",          "report_keyword": "terminal market", "row_match": ["mint", "herb"],          "unit": "lb"},
    {"match": "dill",          "report_keyword": "terminal market", "row_match": ["dill", "herb"],          "unit": "lb"},
    {"match": "rosemary",      "report_keyword": "terminal market", "row_match": ["rosemary", "herb"],      "unit": "lb"},
    {"match": "thyme",         "report_keyword": "terminal market", "row_match": ["thyme", "herb"],         "unit": "lb"},
    {"match": "oregano",       "report_keyword": "terminal market", "row_match": ["oregano", "herb"],       "unit": "lb"},
    {"match": "sage",          "report_keyword": "terminal market", "row_match": ["sage", "herb"],          "unit": "lb"},
    {"match": "chive",         "report_keyword": "terminal market", "row_match": ["chive", "herb"],         "unit": "lb"},

    # ── Fruit ─────────────────────────────────────────────────────────────────
    {"match": "pineapple",     "report_keyword": "fruit",       "row_match": ["pineapple"],                 "unit": "each"},
    # Berries use full singular form as `match` because plural inputs are
    # normalised before matching ("blueberries" → "blueberry"). The looser
    # "blueberr" prefix stays in `row_match` to catch both spellings on the
    # AMS row side.
    {"match": "blueberry",     "report_keyword": "fruit",       "row_match": ["blueberr"],                  "unit": "lb"},
    {"match": "strawberry",    "report_keyword": "fruit",       "row_match": ["strawberr"],                 "unit": "lb"},
    {"match": "raspberry",     "report_keyword": "fruit",       "row_match": ["raspberr"],                  "unit": "lb"},
    {"match": "blackberry",    "report_keyword": "fruit",       "row_match": ["blackberr"],                 "unit": "lb"},
    {"match": "cranberry",     "report_keyword": "fruit",       "row_match": ["cranberr"],                  "unit": "lb"},
    {"match": "cherry",        "report_keyword": "fruit",       "row_match": ["cherry", "cherries"],        "unit": "lb"},
    {"match": "grape",         "report_keyword": "fruit",       "row_match": ["grape"],                     "unit": "lb"},
    {"match": "orange",        "report_keyword": "fruit",       "row_match": ["orange"],                    "unit": "lb"},
    {"match": "lemon",         "report_keyword": "fruit",       "row_match": ["lemon"],                     "unit": "lb"},
    {"match": "lime",          "report_keyword": "fruit",       "row_match": ["lime"],                      "unit": "lb"},
    {"match": "grapefruit",    "report_keyword": "fruit",       "row_match": ["grapefruit"],                "unit": "lb"},
    {"match": "banana",        "report_keyword": "fruit",       "row_match": ["banana"],                    "unit": "lb"},
    {"match": "watermelon",    "report_keyword": "fruit",       "row_match": ["watermelon", "melon"],       "unit": "lb"},
    {"match": "cantaloupe",    "report_keyword": "fruit",       "row_match": ["cantaloupe", "melon"],       "unit": "lb"},
    {"match": "honeydew",      "report_keyword": "fruit",       "row_match": ["honeydew", "melon"],         "unit": "lb"},
    {"match": "peach",         "report_keyword": "fruit",       "row_match": ["peach"],                     "unit": "lb"},
    {"match": "nectarine",     "report_keyword": "fruit",       "row_match": ["nectarine"],                 "unit": "lb"},
    {"match": "plum",          "report_keyword": "fruit",       "row_match": ["plum"],                      "unit": "lb"},
    {"match": "pear",          "report_keyword": "fruit",       "row_match": ["pear"],                      "unit": "lb"},
    {"match": "apple",         "report_keyword": "fruit",       "row_match": ["apple"],                     "unit": "lb"},
    {"match": "mango",         "report_keyword": "fruit",       "row_match": ["mango"],                     "unit": "lb"},
    {"match": "papaya",        "report_keyword": "fruit",       "row_match": ["papaya"],                    "unit": "lb"},
    {"match": "kiwi",          "report_keyword": "fruit",       "row_match": ["kiwi"],                      "unit": "lb"},
    {"match": "avocado",       "report_keyword": "fruit",       "row_match": ["avocado"],                   "unit": "each"},
    {"match": "coconut",       "report_keyword": "fruit",       "row_match": ["coconut"],                   "unit": "each"},
    {"match": "fig",           "report_keyword": "fruit",       "row_match": ["fig"],                       "unit": "lb"},
    {"match": "date",          "report_keyword": "fruit",       "row_match": ["date"],                      "unit": "lb"},
    {"match": "pomegranate",   "report_keyword": "fruit",       "row_match": ["pomegranate"],               "unit": "each"},

    # ── Beef ──────────────────────────────────────────────────────────────────
    {"match": "ground beef",   "report_keyword": "beef",        "row_match": ["ground beef", "ground chuck"], "unit": "lb"},
    {"match": "brisket",       "report_keyword": "beef",        "row_match": ["brisket"],                   "unit": "lb"},
    {"match": "ribeye",        "report_keyword": "beef",        "row_match": ["ribeye", "rib eye"],         "unit": "lb"},
    {"match": "rib eye",       "report_keyword": "beef",        "row_match": ["ribeye", "rib eye"],         "unit": "lb"},
    {"match": "tenderloin",    "report_keyword": "beef",        "row_match": ["tenderloin"],                "unit": "lb"},
    {"match": "sirloin",       "report_keyword": "beef",        "row_match": ["sirloin"],                   "unit": "lb"},
    {"match": "short rib",     "report_keyword": "beef",        "row_match": ["short rib"],                 "unit": "lb"},
    {"match": "skirt steak",   "report_keyword": "beef",        "row_match": ["skirt"],                     "unit": "lb"},
    {"match": "flank steak",   "report_keyword": "beef",        "row_match": ["flank"],                     "unit": "lb"},
    {"match": "chuck",         "report_keyword": "beef",        "row_match": ["chuck"],                     "unit": "lb"},
    {"match": "steak",         "report_keyword": "beef",        "row_match": ["steak"],                     "unit": "lb"},
    {"match": "veal",          "report_keyword": "veal",        "row_match": ["veal"],                      "unit": "lb"},
    {"match": "beef",          "report_keyword": "beef",        "row_match": ["beef"],                      "unit": "lb"},

    # ── Lamb ──────────────────────────────────────────────────────────────────
    {"match": "lamb",          "report_keyword": "lamb",        "row_match": ["lamb"],                      "unit": "lb"},

    # ── Pork ──────────────────────────────────────────────────────────────────
    {"match": "pork belly",    "report_keyword": "pork",        "row_match": ["belly"],                     "unit": "lb"},
    {"match": "pork loin",     "report_keyword": "pork",        "row_match": ["loin"],                      "unit": "lb"},
    {"match": "pork chop",     "report_keyword": "pork",        "row_match": ["chop", "loin"],              "unit": "lb"},
    {"match": "pork shoulder", "report_keyword": "pork",        "row_match": ["shoulder", "butt"],          "unit": "lb"},
    {"match": "pepperoni",     "report_keyword": "pork",        "row_match": ["pepperoni"],                 "unit": "lb"},
    {"match": "prosciutto",    "report_keyword": "pork",        "row_match": ["prosciutto", "ham"],         "unit": "lb"},
    {"match": "salami",        "report_keyword": "pork",        "row_match": ["salami"],                    "unit": "lb"},
    {"match": "chorizo",       "report_keyword": "pork",        "row_match": ["chorizo", "sausage"],        "unit": "lb"},
    {"match": "sausage",       "report_keyword": "pork",        "row_match": ["sausage"],                   "unit": "lb"},
    {"match": "bacon",         "report_keyword": "pork",        "row_match": ["bacon"],                     "unit": "lb"},
    {"match": "ham",           "report_keyword": "pork",        "row_match": ["ham"],                       "unit": "lb"},
    {"match": "pork",          "report_keyword": "pork",        "row_match": ["pork"],                      "unit": "lb"},

    # ── Poultry ───────────────────────────────────────────────────────────────
    {"match": "chicken breast","report_keyword": "chicken",     "row_match": ["breast"],                    "unit": "lb"},
    {"match": "chicken thigh", "report_keyword": "chicken",     "row_match": ["thigh"],                     "unit": "lb"},
    {"match": "chicken wing",  "report_keyword": "chicken",     "row_match": ["wing"],                      "unit": "lb"},
    {"match": "chicken",       "report_keyword": "chicken",     "row_match": ["chicken", "broiler"],        "unit": "lb"},
    {"match": "turkey",        "report_keyword": "turkey",      "row_match": ["turkey"],                    "unit": "lb"},
    {"match": "duck",          "report_keyword": "duck",        "row_match": ["duck"],                      "unit": "lb"},

    # ── Seafood ───────────────────────────────────────────────────────────────
    {"match": "shrimp",        "report_keyword": "shellfish",   "row_match": ["shrimp"],                    "unit": "lb"},
    {"match": "salmon",        "report_keyword": "salmon",      "row_match": ["salmon"],                    "unit": "lb"},
    {"match": "tuna",          "report_keyword": "tuna",        "row_match": ["tuna"],                      "unit": "lb"},
    {"match": "cod",           "report_keyword": "fish",        "row_match": ["cod"],                       "unit": "lb"},
    {"match": "tilapia",       "report_keyword": "fish",        "row_match": ["tilapia"],                   "unit": "lb"},
    {"match": "halibut",       "report_keyword": "fish",        "row_match": ["halibut"],                   "unit": "lb"},
    {"match": "catfish",       "report_keyword": "catfish",     "row_match": ["catfish"],                   "unit": "lb"},
    {"match": "trout",         "report_keyword": "fish",        "row_match": ["trout"],                     "unit": "lb"},
    {"match": "scallop",       "report_keyword": "shellfish",   "row_match": ["scallop"],                   "unit": "lb"},
    {"match": "lobster",       "report_keyword": "shellfish",   "row_match": ["lobster"],                   "unit": "lb"},
    {"match": "crab",          "report_keyword": "shellfish",   "row_match": ["crab"],                      "unit": "lb"},
    {"match": "mussel",        "report_keyword": "shellfish",   "row_match": ["mussel"],                    "unit": "lb"},
    {"match": "clam",          "report_keyword": "shellfish",   "row_match": ["clam"],                      "unit": "lb"},
    {"match": "oyster",        "report_keyword": "shellfish",   "row_match": ["oyster"],                    "unit": "lb"},

    # ── Nuts ──────────────────────────────────────────────────────────────────
    {"match": "almond",        "report_keyword": "nut",         "row_match": ["almond"],                    "unit": "lb"},
    {"match": "pecan",         "report_keyword": "nut",         "row_match": ["pecan"],                     "unit": "lb"},
    {"match": "walnut",        "report_keyword": "nut",         "row_match": ["walnut"],                    "unit": "lb"},
    {"match": "hazelnut",      "report_keyword": "nut",         "row_match": ["hazelnut", "filbert"],       "unit": "lb"},
    {"match": "pistachio",     "report_keyword": "nut",         "row_match": ["pistachio"],                 "unit": "lb"},
    {"match": "peanut",        "report_keyword": "peanut",      "row_match": ["peanut"],                    "unit": "lb"},
    {"match": "cashew",        "report_keyword": "nut",         "row_match": ["cashew"],                    "unit": "lb"},
]


# ─── Name normalisation for matching ─────────────────────────────────────────
#
# Menu text is messy ("Diced Tomatoes (canned)", "Whole Milk Mozzarella"). We
# strip parentheticals, punctuation, and common prep adjectives, then expand
# each remaining token into a few plausible singular forms before doing the
# AMS lookup. This is a strict superset of the old behaviour: anything that
# matched before still matches now, plus a lot more.

_PREP_NOISE_WORDS = {
    "fresh", "frozen", "dried", "canned", "jarred",
    "smoked", "roasted", "grilled", "baked", "cooked", "raw",
    "diced", "chopped", "minced", "shredded", "grated", "sliced",
    "crushed", "pureed",
    "organic", "natural", "premium",
}


def _singular_forms(tok: str) -> List[str]:
    """Return a tok plus naïve singular variants ("tomatoes" → "tomato",
    "cherries" → "cherry", "olives" → "olive"). English plurals are
    irregular enough that we generate multiple candidates and let the matcher
    pick the one that hits an INGREDIENT_TO_AMS entry; extra noise that
    happens to match nothing is harmless.
    """
    forms: List[str] = [tok]
    if len(tok) <= 3:
        return forms
    if tok.endswith("ies"):
        forms.append(tok[:-3] + "y")
    elif tok.endswith("oes"):
        forms.append(tok[:-2])
    elif tok.endswith("es") and len(tok) > 4:
        # "boxes" / "dishes" → drop "es"; "olives" → drop only "s"
        forms.append(tok[:-2])
        forms.append(tok[:-1])
    elif tok.endswith("s") and not tok.endswith("ss"):
        forms.append(tok[:-1])
    return forms


def _normalize_for_match(name: str) -> str:
    """Lowercase, strip punctuation/parentheticals, drop prep adjectives, and
    expand simple plurals. The output is a space-separated string used only
    for matching against `INGREDIENT_TO_AMS` entries.
    """
    if not name:
        return ""
    s = name.lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    expanded: List[str] = []
    for tok in s.split():
        if tok in _PREP_NOISE_WORDS or len(tok) < 2:
            continue
        expanded.extend(_singular_forms(tok))
    return " ".join(expanded)


def find_mapping_for(name: str) -> Optional[Dict[str, Any]]:
    """Pick the best AMS mapping for a free-text ingredient name.

    Matches the normalised form of the name with word-boundary regex against
    each entry's `match` string. Longest match wins so specific entries
    ("cream cheese", "ground beef") beat generic ones ("cheese", "beef"),
    and "milk" no longer matches "buttermilk" (previously a bug that routed
    "Buttermilk Powder" to the fluid-milk report).

    Match strings ending in non-word characters (e.g. "blueberr") are matched
    as a prefix only, to keep their existing "blueberry"/"blueberries" intent.
    """
    norm = _normalize_for_match(name)
    if not norm:
        return None
    candidates: List[Dict[str, Any]] = []
    for m in INGREDIENT_TO_AMS:
        pat = m.get("match") or ""
        if not pat:
            continue
        ends_word = pat[-1].isalnum()
        regex = r"\b" + re.escape(pat) + (r"\b" if ends_word else r"")
        if re.search(regex, norm):
            candidates.append(m)
    if not candidates:
        return None
    return max(candidates, key=lambda m: len(m["match"]))


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def _auth() -> HTTPBasicAuth:
    return HTTPBasicAuth(settings.ams_api_key, "")


def _ams_get(path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if not settings.ams_api_key:
        return None
    url = path if path.startswith("http") else f"{MARS_BASE}{path}"
    try:
        resp = requests.get(url, params=params, auth=_auth(), timeout=15)
        if resp.status_code == 401:
            log.warning("[ams] 401 unauthorized — check AMS_API_KEY")
            return None
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json() or {}
    except Exception as exc:
        log.warning("[ams] GET %s failed: %s", url, exc)
        return None


# Section names AMS uses for actual price data on multi-section reports.
# "Report Header" is metadata only (city, dates, narrative). Anything else
# typically carries the commodity/price columns we care about.
_HEADER_SECTION_NAMES = {"report header", "header"}

# How many days back to ask for on section endpoints. Terminal Market section
# endpoints ignore `limit` entirely and return up to 1.8M rows (>120MB) per
# call without a date filter — `lastDays` is the only way to keep the payload
# manageable. 30 days gives us 4 weekly observations or 20+ daily ones, which
# is plenty for the sparkline.
_SECTION_LAST_DAYS = 30


def _section_rank(name: str) -> int:
    """Rank section names by likelihood of containing prices.

    AMS multi-section reports use a zoo of section names depending on the
    commodity family. Examples we've actually seen in the wild:

      Terminal Markets : ["Report Header", "Report Details"]
      Weekly Chicken   : ["Report Header", "Report Detail"]
      Egg              : ["Report Header", "Report Volume Weighted",
                          "Report Volume Simple", "Report Detail Weighted",
                          "Report Detail Simple"]
      Pork Variety     : ["Report Header", "Report Metrics", "Report Details"]

    Detail/Prices sections carry prices. Volume-only sections carry shipment
    quantities (no $). Metrics may carry either. Sort so we try
    Detail → Price → Metrics → Volume → others.
    """
    low = name.lower()
    if "detail" in low:
        return 0
    if "price" in low and "volume" not in low:
        return 1
    if "metric" in low:
        return 2
    if "summary" in low:
        return 3
    if "volume" in low:
        return 5  # try last; usually just shipment counts
    return 4


def _section_has_prices(body: Dict[str, Any]) -> bool:
    """True if at least one of the first few rows has a price column.

    We don't want to settle for a section that only carries shipment volumes
    (e.g. egg "Report Volume Simple" returns rows but no price fields).
    """
    rows = body.get("results") or body.get("report") or []
    if not isinstance(rows, list) or not rows:
        return False
    for row in rows[:10]:
        if isinstance(row, dict) and _row_has_price(row):
            return True
    return False


def _fetch_report_body(
    slug: str, params: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Fetch a report and follow the `reportSections` redirect when needed.

    AMS reports come in two shapes:

    1. **Single-section** (e.g. the dairy slugs 1082–1085, 1092). Hitting
       `/reports/{slug}` directly returns rows with `commodity`, `group`,
       `price_min`, `price_max` inline. `reportSections` is `None`.

    2. **Multi-section** (Terminal Markets, Pork, Chicken, Egg, etc.).
       Hitting `/reports/{slug}` returns ONLY the "Report Header" metadata
       rows. The price rows live in a sibling section that has to be
       requested explicitly via `/reports/{slug}/{section name}`. Section
       names vary by report family — "Report Details", "Report Detail",
       "Report Detail Simple", etc. We rank-sort them and try in order.

    Section endpoints also IGNORE the `limit` parameter; Terminal Market
    section calls can return up to 1.8M rows / 120MB without a date filter.
    We attach `lastDays=30` to keep the payload small (single-section dairy
    reports stay un-filtered because they sometimes carry stale-but-current
    weekly data that's older than 30 days).
    """
    body = _ams_get(f"/{slug}", params)
    if not body:
        return body

    sections = body.get("reportSections")
    if not isinstance(sections, list):
        return body

    non_header = [
        s for s in sections
        if isinstance(s, str) and s.strip()
        and s.strip().lower() not in _HEADER_SECTION_NAMES
    ]
    if not non_header:
        return body

    section_params = dict(params or {})
    section_params.setdefault("lastDays", _SECTION_LAST_DAYS)
    # `limit`/`offset`/`sort` are ignored on section endpoints; strip so the
    # querystring stays clean for logging / debugging.
    for noisy in ("limit", "offset", "sort"):
        section_params.pop(noisy, None)

    fallback: Optional[Dict[str, Any]] = None
    for sect in sorted(non_header, key=_section_rank):
        deep = _ams_get(f"/{slug}/{_urlquote(sect)}", section_params)
        if not deep:
            continue
        rows = deep.get("results") or deep.get("report") or []
        if not isinstance(rows, list) or not rows:
            continue
        if _section_has_prices(deep):
            return deep
        # Remember the first non-empty section in case nothing has prices —
        # better to surface some data than fall back to the header-only body.
        if fallback is None:
            fallback = deep

    return fallback if fallback is not None else body


# ─── Report discovery (cached) ────────────────────────────────────────────────

_REPORTS_CACHE: List[Dict[str, Any]] = []
_REPORTS_CACHE_LOCK = threading.Lock()
_SLUG_FOR_COMMODITY: Dict[str, str] = {}

# Commodities that recently returned 0 usable rows. Keyed by mapping cache key,
# value is the UTC datetime at which the negative cache entry expires. Using a
# TTL (vs the old permanent set) means a transient AMS hiccup doesn't
# permanently block re-discovery for an entire ingredient family.
_NEGATIVE_COMMODITIES_TTL: Dict[str, datetime] = {}

# Back-compat alias for any external code that imported the old name.
_NEGATIVE_COMMODITIES = _NEGATIVE_COMMODITIES_TTL


def _is_negative_cached(key: str) -> bool:
    if not key:
        return False
    expiry = _NEGATIVE_COMMODITIES_TTL.get(key)
    if not expiry:
        return False
    if datetime.utcnow() >= expiry:
        _NEGATIVE_COMMODITIES_TTL.pop(key, None)
        return False
    return True


def _mark_negative(key: str) -> None:
    if not key:
        return
    _NEGATIVE_COMMODITIES_TTL[key] = datetime.utcnow() + timedelta(
        seconds=_NEGATIVE_TTL_SECONDS
    )


def reset_caches() -> Dict[str, int]:
    """Clear in-memory AMS caches (report list, slug map, negative cache).

    Exposed via ``POST /api/admin/usda/reset-caches`` so a poisoned cache
    (e.g. discovery failed once, all subsequent fetches short-circuited) can
    be cleared without restarting the API process.
    """
    global _REPORTS_CACHE
    cleared = {
        "reports_cache": len(_REPORTS_CACHE),
        "slug_for_commodity": len(_SLUG_FOR_COMMODITY),
        "negative_commodities": len(_NEGATIVE_COMMODITIES_TTL),
    }
    _REPORTS_CACHE = []
    _SLUG_FOR_COMMODITY.clear()
    _NEGATIVE_COMMODITIES_TTL.clear()
    return cleared


def _list_all_reports() -> List[Dict[str, Any]]:
    global _REPORTS_CACHE
    if _REPORTS_CACHE:
        return _REPORTS_CACHE
    with _REPORTS_CACHE_LOCK:
        if _REPORTS_CACHE:
            return _REPORTS_CACHE
        body = _ams_get("")
        if not body:
            return []
        # The /reports endpoint returns either a list directly or {"results":[...]}
        if isinstance(body, list):
            reports = body
        else:
            reports = body.get("results") or body.get("reports") or []
        if isinstance(reports, list):
            _REPORTS_CACHE = reports
        return _REPORTS_CACHE


_PRICE_TITLE_HINTS = ("price", "wholesale", "market", "weekly", "daily", "average")
_PRICE_TITLE_BAD = ("storage", "holdings", "shipment", "stocks", "inventory", "imports")
# AMS leaves dead reports in the listing forever with "(Discontinued)" tacked
# onto the title. They still serve a Report Header section (so reportSections
# looks normal) but every detail/price section is empty. They ranked HIGHER
# than active reports for "terminal market" produce keywords because their
# titles include "Wholesale Market…" (+1 from the wholesale bonus), so the
# 20-candidate cap was getting filled entirely with dead reports.
_PRICE_TITLE_DEAD = ("discontinued",)


def _report_text_blob(r: Dict[str, Any]) -> str:
    parts: List[str] = []
    for k in ("report_title", "title", "name", "report_name", "slug_name"):
        v = r.get(k)
        if v:
            parts.append(str(v))
    for k in ("commodities", "commodity", "markets", "market_types", "categories"):
        v = r.get(k)
        if isinstance(v, list):
            parts.extend(str(x) for x in v)
        elif v:
            parts.append(str(v))
    return " ".join(parts).lower()


def _candidate_reports_for(report_keyword: str, region_hint: str = "") -> List[Dict[str, Any]]:
    """Find AMS reports whose title/markets contain the keyword.

    Uses a *report-level* keyword (e.g. "cheese", "pork") rather than the
    specific commodity, because USDA report titles are organised by category
    — mozzarella / cheddar / muenster all live inside "Cheese - East U.S."
    style reports, never in a report titled "Mozzarella".

    Reports with a "storage/holdings/shipments" hint were previously dropped
    outright. We now only drop them when they have NO price-style hint,
    because many real price reports use mixed titling
    (e.g. "Fruit and Vegetable Wholesale Markets and Shipments").
    """
    needle = (report_keyword or "").lower().strip()
    hint = (region_hint or "").lower()
    if not needle:
        return []
    out: List[Dict[str, Any]] = []
    for r in _list_all_reports():
        blob = _report_text_blob(r)
        if needle not in blob:
            continue
        if any(dead in blob for dead in _PRICE_TITLE_DEAD):
            continue
        has_bad = any(bad in blob for bad in _PRICE_TITLE_BAD)
        has_good = any(h in blob for h in _PRICE_TITLE_HINTS)
        if has_bad and not has_good:
            continue
        score = 0
        if has_good:
            score += 5
        if has_bad:
            score -= 2  # mixed-content report, deprioritise
        if hint and hint in blob:
            score += 3
        if "national" in blob:
            score += 1
        if "u.s." in blob or "united states" in blob:
            score += 1
        if "wholesale" in blob:
            score += 1
        out.append({"_score": score, "report": r})
    out.sort(key=lambda x: x["_score"], reverse=True)
    return [c["report"] for c in out[:_CANDIDATE_REPORT_CAP]]


def _slug_id(report: Dict[str, Any]) -> Optional[str]:
    for k in ("slug_id", "slugId", "id", "report_id"):
        if report.get(k) is not None:
            return str(report[k])
    return None


# ─── Row parsing ─────────────────────────────────────────────────────────────

_DATE_KEYS = (
    "report_date", "published_date", "report_begin_date", "publish_date",
    "begin_date", "report_end_date", "reportDate",
)
_LOW_KEYS = (
    "price_min", "low_price", "low", "price_low", "f_o_b_price_min",
    "price_min_dollars", "low_dollars",
)
_HIGH_KEYS = (
    "price_max", "high_price", "high", "price_high", "f_o_b_price_max",
    "price_max_dollars", "high_dollars",
)
_MOSTLY_KEYS = (
    "price_mostly", "mostly_low_price", "mostly", "price_mostly_low",
    # Weighted/simple averages used in poultry, egg, and pork reports.
    "weighted_avg_price", "wtd_avg_price", "wtd_average_price",
    "weighted_average_price",
    "average_price", "avg_price", "price_avg", "asking_price",
)
# AMS reports use varying capitalization for the unit field: "price_unit",
# "price_Unit", "Price_Unit". We match all of them.
_PRICE_UNIT_KEYS = ("price_unit", "price_Unit", "Price_Unit")
_COMMODITY_KEYS = (
    "commodity", "commodity_name", "item", "product", "description",
    # AMS dairy/meat reports often put the actual sub-type in `group` or
    # `category` rather than `commodity` (commodity == "Cheese", group ==
    # "Mozzarella"). We have to scan all of these for row-level matching.
    "group", "category", "type", "subgroup", "variety",
)


def _first(d: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[Any]:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _parse_date(raw: Any) -> Optional[date]:
    if raw is None:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    s = str(raw).strip().split(" ")[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_float(raw: Any) -> Optional[float]:
    if raw in (None, ""):
        return None
    try:
        return float(str(raw).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _row_has_price(row: Dict[str, Any]) -> bool:
    return (
        _first(row, _LOW_KEYS) is not None
        or _first(row, _HIGH_KEYS) is not None
        or _first(row, _MOSTLY_KEYS) is not None
    )


def _row_search_blob(row: Dict[str, Any]) -> str:
    """Concatenate every plausibly-useful column for substring matching.

    AMS rows put the descriptive sub-type in different columns depending on
    report family (dairy uses `group`, terminal markets use `commodity`,
    chicken reports use `description`). We scan all of them.
    """
    parts: List[str] = []
    for k in _COMMODITY_KEYS:
        v = row.get(k)
        if v:
            parts.append(str(v))
    return " ".join(parts).lower()


def _row_label(row: Dict[str, Any]) -> str:
    """Best display label for a row — prefer specific (`group`) over generic
    (`commodity`) when both are present, so we get "Mozzarella" instead of
    just "Cheese".
    """
    for k in ("group", "subgroup", "variety", "description", "item",
              "product", "commodity_name", "commodity"):
        v = row.get(k)
        if v:
            return str(v)
    return ""


def _price_scale_for_unit(unit_str: Optional[str]) -> float:
    """Return a multiplier to convert AMS-reported prices to dollars.

    Several AMS reports quote in cents (poultry: "Cents Per Lb",
    eggs: "Cents Per Dozen"). We store dollars in the DB so the UI
    consistently renders `$X.XX/unit`. Returns 0.01 for cents, 1.0 otherwise.
    """
    if not unit_str:
        return 1.0
    return 0.01 if "cent" in str(unit_str).lower() else 1.0


# Terminal Market rows DON'T carry a `price_unit` field — prices are per the
# package described in `package` (e.g. "40 lb cartons", "5 kg/11 lb flats",
# "25 lb sacks"). Without a divisor, $22 for an 11 lb flat of tomatoes gets
# stored as "$22/lb" which is ~10× the true wholesale price.
_PACKAGE_LB_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:-)?\s*(?:lb|lbs|pound|pounds)\b",
    re.IGNORECASE,
)
# Common AMS bushel approximations (rough trade-weight averages). Bushel
# weights are commodity-specific; these cover the vast majority of produce
# rows. For commodities we don't have a number for, we fall back to dropping
# the row rather than storing a wildly wrong $/lb.
_BUSHEL_WEIGHT_LB: Dict[str, float] = {
    "apple": 42.0, "pear": 50.0, "peach": 50.0, "tomato": 53.0,
    "cucumber": 48.0, "pepper": 25.0, "bean": 30.0, "corn": 56.0,
    "carrot": 50.0, "potato": 60.0,
}


def _extract_package_lb(package: Optional[str], commodity_blob: str = "") -> Optional[float]:
    """Best-effort: how many pounds is one AMS `package`?

    Handles common AMS phrasings:
      "40 lb cartons"            → 40
      "25 lb sacks loose"        → 25
      "5 kg/11 lb flats"         → 11   (US weight wins over metric)
      "1 lb film bags"           → 1
      "1 1/9 bushel cartons"     → commodity-specific bushel weight, if known

    Returns None when no number is recoverable; callers should drop the row
    rather than store a per-package price as $/lb.
    """
    if not package:
        return None
    text = str(package)
    m = _PACKAGE_LB_RE.search(text)
    if m:
        try:
            v = float(m.group(1))
            if v > 0:
                return v
        except ValueError:
            pass
    if "bushel" in text.lower():
        blob = (commodity_blob or "").lower()
        for needle, weight in _BUSHEL_WEIGHT_LB.items():
            if needle in blob:
                return weight
    return None


def _extract_rows(
    body: Dict[str, Any],
    row_needles: List[str],
) -> List[Dict[str, Any]]:
    """Pick rows whose searchable blob matches ANY of the row_needles AND
    that carry an actual price (low/high/mostly/avg).

    Normalizes the stored price to dollars per the mapping's unit. Two
    transforms can apply (independently):
      • cent → dollar  : when `price_unit` says "Cents Per X" (poultry, eggs)
      • per-package → per-lb : when no `price_unit` is given and the row is
        priced per package (Terminal Market rows: 11 lb flats, 25 lb sacks…).
        Without this, $22 per 11-lb flat of tomatoes gets stored as "$22/lb".
        Rows whose package weight is unrecoverable are dropped rather than
        stored with a wrong-by-10× value.
    """
    rows = body.get("results") or body.get("report") or []
    if not isinstance(rows, list):
        return []
    needles = [n.lower() for n in row_needles if n]
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not _row_has_price(row):
            continue
        blob = _row_search_blob(row) if needles else ""
        if needles and not any(n in blob for n in needles):
            continue
        d = _parse_date(_first(row, _DATE_KEYS))
        if d is None:
            continue

        unit_str = _first(row, _PRICE_UNIT_KEYS)
        scale = _price_scale_for_unit(unit_str)
        forced_unit: Optional[str] = None
        if not unit_str:
            # No explicit price unit → price is per-package. Try to extract
            # the package weight in lb so we can normalise to $/lb.
            lb = _extract_package_lb(row.get("package"), commodity_blob=blob)
            if lb and lb > 0:
                scale = scale / lb
                # Force the storage unit to "lb" regardless of the mapping —
                # the divisor is in pounds, so "lb" is the only honest unit
                # to attach. This overrides e.g. mapping.unit="each" for
                # pineapples (whose wholesale prices are per 30-lb carton,
                # not per pineapple).
                forced_unit = "lb"
            else:
                # Unknown package size: dropping the row beats storing
                # tomato at $22/lb.
                continue

        def _scaled(keys: Tuple[str, ...]) -> Optional[float]:
            v = _parse_float(_first(row, keys))
            return None if v is None else v * scale

        label = _row_label(row)
        out.append({
            "as_of_date": d,
            "price_low": _scaled(_LOW_KEYS),
            "price_high": _scaled(_HIGH_KEYS),
            "price_mostly": _scaled(_MOSTLY_KEYS),
            "commodity_label": label[:255] if label else None,
            "unit_override": forced_unit,
            "raw": row,
        })
    return out


# ─── Public fetch API ────────────────────────────────────────────────────────

def _cache_key(mapping: Dict[str, Any]) -> str:
    """Stable cache key per mapping entry (ingredient family)."""
    return mapping.get("match") or mapping.get("report_keyword") or ""


def fetch_recent_prices(
    name: str,
    *,
    weeks: int = 8,           # noqa: ARG001 — kept for API compat; AMS row dates are filtered post-fetch
    page_size: int = 200,
) -> List[Dict[str, Any]]:
    """Discover (or use cached) AMS slug for the ingredient and return parsed
    price rows.

    Strategy
    ────────
      1. Use cached (slug → mapping) discovery if we've already found one.
      2. Otherwise, list all reports and pick candidates whose title matches
         the mapping's `report_keyword` (cheese / pork / fruit / …).
      3. Fetch each candidate (sorted newest-first) and try to extract rows
         that match the mapping's `row_match` keywords.
      4. Cache the first slug that yields rows so subsequent fetches are a
         single HTTP call.

    Note: we used to send `q=published_date>=…` here. AMS rejects that with
    HTTP 400 for many slugs, so we just rely on `sort=-published_date` and
    take the most recent rows.
    """
    if not settings.ams_api_key:
        return []
    mapping = find_mapping_for(name)
    if not mapping:
        return []
    cache_key = _cache_key(mapping)
    if _is_negative_cached(cache_key):
        return []

    row_needles: List[str] = mapping.get("row_match") or [mapping.get("match", "")]
    report_keyword: str = mapping.get("report_keyword") or mapping.get("match", "")

    params = {
        "limit": page_size,
        "offset": 0,
        "sort": "-published_date",
    }

    # 1. Try cached slug, if any
    cached_slug = _SLUG_FOR_COMMODITY.get(cache_key)
    if cached_slug:
        body = _fetch_report_body(cached_slug, params)
        if body:
            rows = _extract_rows(body, row_needles)
            if rows:
                return _annotate(rows, mapping, cached_slug)
        # cached slug stopped returning; clear and re-discover
        _SLUG_FOR_COMMODITY.pop(cache_key, None)

    # 2. Discover candidates from the reports list
    candidates = _candidate_reports_for(report_keyword)

    # 2b. Fallback discovery: if the report-level keyword found nothing
    # (e.g. our mapping says "fish" but AMS titles the report after the
    # specific species), retry with the row-level keywords.
    if not candidates:
        seen_slugs: set = set()
        extra: List[Dict[str, Any]] = []
        for needle in row_needles:
            if not needle or needle == report_keyword:
                continue
            for r in _candidate_reports_for(needle):
                sid = _slug_id(r)
                if sid and sid not in seen_slugs:
                    seen_slugs.add(sid)
                    extra.append(r)
        candidates = extra

    for report in candidates:
        slug = _slug_id(report)
        if not slug:
            continue
        body = _fetch_report_body(slug, params)
        if not body:
            continue
        rows = _extract_rows(body, row_needles)
        if rows:
            _SLUG_FOR_COMMODITY[cache_key] = slug
            log.info(
                "[ams] using slug %s (%s) for %r — %d rows",
                slug, report.get("report_title"), cache_key, len(rows),
            )
            return _annotate(rows, mapping, slug)

    log.info(
        "[ams] no usable price report found for %r "
        "(keyword=%r, candidates_tried=%d)",
        cache_key, report_keyword, len(candidates),
    )
    _mark_negative(cache_key)
    return []


def _annotate(
    rows: List[Dict[str, Any]],
    mapping: Dict[str, Any],
    slug: str,
) -> List[Dict[str, Any]]:
    """Stamp slug / unit / region onto extracted rows.

    Honours ``unit_override`` if set by ``_extract_rows`` (e.g. when a per-
    package row was normalised to per-lb). Otherwise falls back to the
    mapping's preferred display unit.
    """
    for r in rows:
        r["slug"] = slug
        r["unit"] = r.pop("unit_override", None) or mapping.get("unit") or "lb"
        r["region"] = mapping.get("region") or "national"
    return rows


# ─── DB helpers ──────────────────────────────────────────────────────────────

def store_price_points(session, ingredient_id, points: List[Dict[str, Any]]) -> int:
    if not points:
        return 0
    from models import IngredientPrice
    from sqlmodel import select

    inserted = 0
    for p in points:
        if not p.get("as_of_date"):
            continue
        existing = session.exec(
            select(IngredientPrice)
            .where(IngredientPrice.ingredient_id == ingredient_id)
            .where(IngredientPrice.as_of_date == p["as_of_date"])
            .where(IngredientPrice.report_slug == p.get("slug"))
        ).first()
        if existing:
            continue
        session.add(IngredientPrice(
            ingredient_id=ingredient_id,
            source="AMS_MARKET_NEWS",
            report_slug=p.get("slug"),
            region=p.get("region"),
            commodity_label=p.get("commodity_label"),
            unit=p.get("unit") or "lb",
            price_low=p.get("price_low"),
            price_high=p.get("price_high"),
            price_mostly=p.get("price_mostly"),
            as_of_date=p["as_of_date"],
            raw_payload=p.get("raw"),
        ))
        inserted += 1
    if inserted:
        session.commit()
    return inserted


def fetch_and_store_prices_for_ingredient(session, ingredient) -> int:
    points = fetch_recent_prices(ingredient.name, weeks=8)
    if not points:
        return 0
    return store_price_points(session, ingredient.id, points)


def summarize_ingredient_prices(session, ingredient_id, max_points: int = 12) -> Dict[str, Any]:
    from models import IngredientPrice
    from sqlmodel import select

    rows = session.exec(
        select(IngredientPrice)
        .where(IngredientPrice.ingredient_id == ingredient_id)
        .order_by(IngredientPrice.as_of_date.desc())
        .limit(max_points)
    ).all()
    if not rows:
        return {"has_data": False}

    def midpoint(p) -> Optional[float]:
        if p.price_mostly is not None:
            return p.price_mostly
        if p.price_low is not None and p.price_high is not None:
            return (p.price_low + p.price_high) / 2.0
        return p.price_low or p.price_high

    series, midpoints = [], []
    for r in rows:
        m = midpoint(r)
        series.append({
            "as_of_date": r.as_of_date.isoformat() if r.as_of_date else None,
            "price_low": r.price_low,
            "price_high": r.price_high,
            "price_mostly": r.price_mostly,
            "midpoint": m,
            "unit": r.unit,
        })
        if m is not None:
            midpoints.append(m)

    summary: Dict[str, Any] = {
        "has_data": True,
        "source": "AMS_MARKET_NEWS",
        "unit": rows[0].unit,
        "region": rows[0].region,
        "commodity_label": rows[0].commodity_label,
        "report_slug": rows[0].report_slug,
        "latest": series[0] if series else None,
        "n_points": len(series),
        "series": list(reversed(series)),
    }
    if midpoints:
        summary.update({
            "min": min(midpoints),
            "max": max(midpoints),
            "avg": sum(midpoints) / len(midpoints),
        })
    return summary
