"""
Distributor discovery via Google's *new* Places API.

Why "new" and not legacy?
=========================

The legacy textsearch endpoint (``maps.googleapis.com/.../textsearch/json``)
is deprecated and Google now rejects new projects with
``REQUEST_DENIED: You're calling a legacy API``. The replacement
(``places.googleapis.com/v1/places:searchText``) ships enabled by default and
takes a JSON body + ``X-Goog-Api-Key`` header + ``X-Goog-FieldMask``.

Strategy
========

1. Geocode the restaurant ZIP -> lat/lng (legacy Geocoding API is fine and
   still supported; user just needs it enabled in their Cloud project).
2. Run a handful of broad text queries with progressively wider radius rings
   (10mi, 20mi, 30mi, 50mi) until we have ``MAX_DISTRIBUTORS`` unique places
   or we exhaust the 50mi cap.
3. Persist them with inferred categories (from name + types).

There is no demo / fallback list any more. If discovery returns 0 results we
emit a Notification telling the user exactly which Google APIs to enable.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from sqlmodel import Session, select

from config import settings
from models import Distributor, RestaurantProfile

log = logging.getLogger(__name__)

PLACES_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
JINA_BASE = "https://r.jina.ai/"

MAX_DISTRIBUTORS = 6
RADIUS_RINGS_M: List[int] = [
    16_093,   # 10 miles
    32_187,   # 20 miles
    48_280,   # 30 miles
    80_467,   # 50 miles (hard cap)
]
MAX_RADIUS_M = RADIUS_RINGS_M[-1]

# Broad set of text queries. Searches are cheap and we dedupe by place_id.
TEXT_QUERIES: List[str] = [
    "wholesale food distributor",
    "restaurant supply",
    "foodservice distributor",
    "produce wholesaler",
    "wholesale meat",
    "wholesale produce",
    "wholesale dairy",
    "restaurant depot",
    "cash and carry restaurant",
]

PLACES_FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.types",
    "places.websiteUri",
])


# ─── Category inference ──────────────────────────────────────────────────────

_CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "produce": ["produce", "vegetable", "fruit", "greens", "herb"],
    "meat": ["meat", "beef", "pork", "poultry", "chicken", "butcher"],
    "seafood": ["seafood", "fish", "shrimp", "lobster", "oyster"],
    "dairy": ["dairy", "cheese", "milk", "butter", "cream", "yogurt"],
    "dry goods": ["dry goods", "flour", "rice", "pasta", "grain", "spice", "pantry"],
    "beverage": ["beverage", "drink", "juice", "soda", "coffee", "tea"],
    "frozen": ["frozen", "ice cream", "freezer"],
    "bakery": ["bakery", "bread", "pastry"],
}


def _categories_from_text(text: str) -> List[str]:
    if not text:
        return []
    blob = text.lower()
    return [cat for cat, kws in _CATEGORY_KEYWORDS.items() if any(k in blob for k in kws)]


def _scrape_categories(website_url: str) -> List[str]:
    if not website_url or not settings.jina_api_key:
        return []
    try:
        headers = {"Authorization": f"Bearer {settings.jina_api_key}"}
        resp = requests.get(f"{JINA_BASE}{website_url}", headers=headers, timeout=15)
        return _categories_from_text(resp.text)
    except Exception:
        return []


def _categories_from_place(place: Dict[str, Any]) -> List[str]:
    """Use the place display name + types as a free signal even without scraping."""
    bits: List[str] = []
    name = (place.get("displayName") or {}).get("text") or ""
    bits.append(name)
    types = place.get("types") or []
    if isinstance(types, list):
        bits.extend(str(t) for t in types)
    bits.append(place.get("formattedAddress") or "")
    return _categories_from_text(" ".join(bits))


# ─── Demo routing address builder ────────────────────────────────────────────

def _slugify_tag(name: str) -> str:
    """Plus-tag-safe slug: [a-z0-9_-], no leading/trailing/double separators."""
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "vendor"


def build_demo_routing_email(profile_email: str, distributor_name: str) -> str:
    """Build a plus-tagged address that always delivers to the operator's inbox.

    Example: profile_email='ani2nem@gmail.com', distributor_name='Riverbend Produce Co.'
             -> 'ani2nem+riverbend_produce_co@gmail.com'
    """
    local, _, domain = (profile_email or "").partition("@")
    if not local or not domain:
        return f"vendor+{_slugify_tag(distributor_name)}@example.com"
    tag = _slugify_tag(distributor_name)
    base_local = local.split("+", 1)[0]
    return f"{base_local}+{tag}@{domain}"


# ─── Geocoding ───────────────────────────────────────────────────────────────

_GEOCODE_CACHE: Dict[str, Tuple[float, float]] = {}


def _geocode_zip(zip_code: str) -> Optional[Tuple[float, float]]:
    if not zip_code or not settings.google_places_api_key:
        return None
    if zip_code in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[zip_code]
    try:
        resp = requests.get(
            GEOCODE_URL,
            params={"address": zip_code, "key": settings.google_places_api_key},
            timeout=10,
        )
        body = resp.json() or {}
    except Exception as exc:
        log.warning("[places] geocode failed for %s: %s", zip_code, exc)
        return None
    status = body.get("status")
    if status != "OK":
        log.warning(
            "[places] geocode %s -> status=%s err=%s",
            zip_code, status, body.get("error_message"),
        )
        return None
    results = body.get("results") or []
    if not results:
        return None
    loc = results[0].get("geometry", {}).get("location") or {}
    if "lat" in loc and "lng" in loc:
        _GEOCODE_CACHE[zip_code] = (loc["lat"], loc["lng"])
        return _GEOCODE_CACHE[zip_code]
    return None


# ─── Place fetching (new Places API) ─────────────────────────────────────────

def _places_text_search(
    query: str,
    location: Tuple[float, float],
    radius_m: int,
) -> List[Dict[str, Any]]:
    """POST against places.googleapis.com/v1/places:searchText."""
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": settings.google_places_api_key,
        "X-Goog-FieldMask": PLACES_FIELD_MASK,
    }
    body = {
        "textQuery": query,
        "locationBias": {
            "circle": {
                "center": {"latitude": location[0], "longitude": location[1]},
                "radius": float(radius_m),
            }
        },
        "maxResultCount": 20,
    }
    try:
        resp = requests.post(PLACES_TEXT_URL, headers=headers, json=body, timeout=12)
    except Exception as exc:
        log.warning("[places] textsearch %r failed (network): %s", query, exc)
        return []
    if resp.status_code != 200:
        # Try to surface Google's structured error so the operator can act.
        try:
            err_body = resp.json()
        except Exception:
            err_body = {"raw": resp.text[:200]}
        log.warning(
            "[places] textsearch %r radius=%dm -> HTTP %d %s",
            query, radius_m, resp.status_code, err_body,
        )
        return []
    data = resp.json() or {}
    places = data.get("places") or []
    log.info("[places] textsearch %r radius=%dm -> %d results", query, radius_m, len(places))
    return places


def _aggregate_places_at_radius(
    location: Tuple[float, float],
    radius_m: int,
    seen_ids: Set[str],
    accumulator: List[Dict[str, Any]],
) -> None:
    for query in TEXT_QUERIES:
        if len(accumulator) >= MAX_DISTRIBUTORS:
            return
        for place in _places_text_search(query, location, radius_m):
            pid = place.get("id")
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            accumulator.append(place)
            if len(accumulator) >= MAX_DISTRIBUTORS:
                return


def _aggregate_places(zip_code: str) -> List[Dict[str, Any]]:
    """Run progressive-radius searches until 6 vendors found or 50mi exhausted."""
    location = _geocode_zip(zip_code)
    if not location:
        return []

    seen: Set[str] = set()
    aggregated: List[Dict[str, Any]] = []
    for radius_m in RADIUS_RINGS_M:
        log.info("[places] expanding to radius=%dm (%.0fmi)", radius_m, radius_m / 1609.34)
        _aggregate_places_at_radius(location, radius_m, seen, aggregated)
        if len(aggregated) >= MAX_DISTRIBUTORS:
            break

    return aggregated


# ─── Notifications for setup problems ────────────────────────────────────────

def _emit_setup_notification(session: Session, reason: str) -> None:
    """Tell the operator exactly what they need to fix in Google Cloud."""
    from models import Notification

    msg = (
        reason + " "
        "Enable both APIs in Google Cloud Console: "
        "Geocoding API and Places API (New). "
        "Then make sure GOOGLE_PLACES_API_KEY in backend/.env has access to both."
    )
    session.add(Notification(title="Distributor Discovery Failed", message=msg))


# ─── Public entry point ──────────────────────────────────────────────────────

def discover_distributors(profile: RestaurantProfile, session: Session) -> List[Distributor]:
    """Discover real local distributors near the profile ZIP.

    No hardcoded fallbacks: returns whatever Google returns (capped at
    ``MAX_DISTRIBUTORS``). If Google returns nothing, we emit a notification
    explaining what to enable, and the procurement flow surfaces a banner.
    """
    if not settings.google_places_api_key:
        log.warning("[places] GOOGLE_PLACES_API_KEY not set; skipping discovery")
        _emit_setup_notification(
            session, "Discovery skipped because GOOGLE_PLACES_API_KEY is empty."
        )
        return []

    location = _geocode_zip(profile.zip_code or "")
    if not location:
        _emit_setup_notification(
            session,
            f"Could not geocode zip {profile.zip_code!r}. "
            "Check the profile zip code or the Geocoding API status.",
        )
        return []

    places = _aggregate_places(profile.zip_code or "")
    log.info(
        "[places] aggregated %d unique places for zip=%s (cap=%d, max_radius=%dmi)",
        len(places), profile.zip_code, MAX_DISTRIBUTORS, MAX_RADIUS_M // 1609,
    )

    if not places:
        _emit_setup_notification(
            session,
            f"Google returned no distributors within 50 miles of zip {profile.zip_code!r}.",
        )
        return []

    created: List[Distributor] = []
    for place in places:
        place_id = place.get("id", "")
        name = (place.get("displayName") or {}).get("text") or "Unknown"
        existing = session.exec(
            select(Distributor)
            .where(Distributor.restaurant_profile_id == profile.id)
            .where(Distributor.google_place_id == place_id)
        ).first()
        if existing:
            continue
        website = place.get("websiteUri") or ""
        categories = _scrape_categories(website) or _categories_from_place(place)
        dist = Distributor(
            restaurant_profile_id=profile.id,
            name=name,
            google_place_id=place_id,
            demo_routing_email=build_demo_routing_email(profile.email, name),
            supplied_categories=categories,
        )
        session.add(dist)
        created.append(dist)
    session.flush()
    return created
