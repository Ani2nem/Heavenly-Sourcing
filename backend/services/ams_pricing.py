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
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.auth import HTTPBasicAuth

from config import settings

log = logging.getLogger(__name__)

MARS_BASE = "https://marsapi.ams.usda.gov/services/v1.2/reports"


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
    # ── Dairy ─────────────────────────────────────────────────────────────────
    {"match": "mozzarella",   "report_keyword": "cheese",  "row_match": ["mozzarella"],         "unit": "lb"},
    {"match": "ricotta",      "report_keyword": "cheese",  "row_match": ["ricotta"],            "unit": "lb"},
    {"match": "mascarpone",   "report_keyword": "cheese",  "row_match": ["mascarpone", "italian"], "unit": "lb"},
    {"match": "cream cheese", "report_keyword": "cheese",  "row_match": ["cream cheese"],       "unit": "lb"},
    {"match": "goat cheese",  "report_keyword": "cheese",  "row_match": ["goat", "chevre"],     "unit": "lb"},
    {"match": "cheddar",      "report_keyword": "cheese",  "row_match": ["cheddar"],            "unit": "lb"},
    {"match": "swiss",        "report_keyword": "cheese",  "row_match": ["swiss"],              "unit": "lb"},
    {"match": "monterey",     "report_keyword": "cheese",  "row_match": ["monterey"],           "unit": "lb"},
    {"match": "muenster",     "report_keyword": "cheese",  "row_match": ["muenster"],           "unit": "lb"},
    {"match": "parmesan",     "report_keyword": "cheese",  "row_match": ["parmesan", "italian"], "unit": "lb"},
    {"match": "cheese",       "report_keyword": "cheese",  "row_match": ["cheese"],             "unit": "lb"},
    {"match": "butter",       "report_keyword": "butter",  "row_match": ["butter"],             "unit": "lb"},
    {"match": "milk",         "report_keyword": "milk",    "row_match": ["milk"],               "unit": "lb"},

    # ── Produce: most live in "Terminal Market" reports filtered by row group ─
    {"match": "tomato",       "report_keyword": "terminal market", "row_match": ["tomato"],     "unit": "lb"},
    {"match": "onion",        "report_keyword": "onions and potatoes", "row_match": ["onion"],  "unit": "lb"},
    {"match": "potato",       "report_keyword": "onions and potatoes", "row_match": ["potato"], "unit": "lb"},
    {"match": "lettuce",      "report_keyword": "terminal market", "row_match": ["lettuce"],    "unit": "head"},
    {"match": "mushroom",     "report_keyword": "terminal market", "row_match": ["mushroom"],   "unit": "lb"},
    {"match": "bell pepper",  "report_keyword": "terminal market", "row_match": ["bell pepper", "peppers"], "unit": "lb"},
    {"match": "jalapeno",     "report_keyword": "terminal market", "row_match": ["jalapeno", "chile"], "unit": "lb"},
    {"match": "pepper",       "report_keyword": "terminal market", "row_match": ["pepper"],     "unit": "lb"},
    {"match": "spinach",      "report_keyword": "terminal market", "row_match": ["spinach"],    "unit": "lb"},
    {"match": "olive",        "report_keyword": "terminal market", "row_match": ["olive"],      "unit": "lb"},

    # ── Fruit ─────────────────────────────────────────────────────────────────
    {"match": "pineapple",    "report_keyword": "fruit",   "row_match": ["pineapple"],          "unit": "each"},
    {"match": "blueberr",     "report_keyword": "fruit",   "row_match": ["blueberr"],           "unit": "lb"},
    {"match": "apple",        "report_keyword": "fruit",   "row_match": ["apple"],              "unit": "lb"},

    # ── Meat / poultry ────────────────────────────────────────────────────────
    {"match": "ground beef",  "report_keyword": "beef",    "row_match": ["ground beef", "ground chuck"], "unit": "lb"},
    {"match": "brisket",      "report_keyword": "beef",    "row_match": ["brisket"],            "unit": "lb"},
    {"match": "beef",         "report_keyword": "beef",    "row_match": ["beef"],               "unit": "lb"},
    {"match": "chicken",      "report_keyword": "chicken", "row_match": ["chicken", "broiler"], "unit": "lb"},
    {"match": "pepperoni",    "report_keyword": "pork",    "row_match": ["pepperoni"],          "unit": "lb"},
    {"match": "sausage",      "report_keyword": "pork",    "row_match": ["sausage"],            "unit": "lb"},
    {"match": "bacon",        "report_keyword": "pork",    "row_match": ["bacon"],              "unit": "lb"},
    {"match": "ham",          "report_keyword": "pork",    "row_match": ["ham"],                "unit": "lb"},
    {"match": "pork",         "report_keyword": "pork",    "row_match": ["pork"],               "unit": "lb"},
    {"match": "shrimp",       "report_keyword": "shellfish", "row_match": ["shrimp"],           "unit": "lb"},
]


def find_mapping_for(name: str) -> Optional[Dict[str, Any]]:
    if not name:
        return None
    needle = name.lower()
    candidates = [m for m in INGREDIENT_TO_AMS if m["match"] in needle]
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


# ─── Report discovery (cached) ────────────────────────────────────────────────

_REPORTS_CACHE: List[Dict[str, Any]] = []
_REPORTS_CACHE_LOCK = threading.Lock()
_SLUG_FOR_COMMODITY: Dict[str, str] = {}
_NEGATIVE_COMMODITIES: set = set()  # commodities we know have no usable report


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
        if any(bad in blob for bad in _PRICE_TITLE_BAD):
            # likely a volume/storage report, skip
            continue
        score = 0
        if any(h in blob for h in _PRICE_TITLE_HINTS):
            score += 5
        if hint and hint in blob:
            score += 3
        # Prefer national / weekly / daily summaries
        if "national" in blob:
            score += 1
        if "u.s." in blob or "united states" in blob:
            score += 1
        out.append({"_score": score, "report": r})
    out.sort(key=lambda x: x["_score"], reverse=True)
    return [c["report"] for c in out[:8]]  # top 8 candidates


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
    "weighted_avg_price", "average_price", "avg_price", "price_avg",
    "asking_price",
)
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


def _extract_rows(
    body: Dict[str, Any],
    row_needles: List[str],
) -> List[Dict[str, Any]]:
    """Pick rows whose searchable blob matches ANY of the row_needles AND
    that carry an actual price (low/high/mostly/avg).
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
        if needles:
            blob = _row_search_blob(row)
            if not any(n in blob for n in needles):
                continue
        d = _parse_date(_first(row, _DATE_KEYS))
        if d is None:
            continue
        label = _row_label(row)
        out.append({
            "as_of_date": d,
            "price_low": _parse_float(_first(row, _LOW_KEYS)),
            "price_high": _parse_float(_first(row, _HIGH_KEYS)),
            "price_mostly": _parse_float(_first(row, _MOSTLY_KEYS)),
            "commodity_label": label[:255] if label else None,
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
    if cache_key in _NEGATIVE_COMMODITIES:
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
        body = _ams_get(f"/{cached_slug}", params)
        if body:
            rows = _extract_rows(body, row_needles)
            if rows:
                return _annotate(rows, mapping, cached_slug)
        # cached slug stopped returning; clear and re-discover
        _SLUG_FOR_COMMODITY.pop(cache_key, None)

    # 2. Discover candidates from the reports list
    candidates = _candidate_reports_for(report_keyword)
    for report in candidates:
        slug = _slug_id(report)
        if not slug:
            continue
        body = _ams_get(f"/{slug}", params)
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
    _NEGATIVE_COMMODITIES.add(cache_key)
    return []


def _annotate(
    rows: List[Dict[str, Any]],
    mapping: Dict[str, Any],
    slug: str,
) -> List[Dict[str, Any]]:
    for r in rows:
        r["slug"] = slug
        r["unit"] = mapping.get("unit") or "lb"
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
