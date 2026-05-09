import requests
from typing import List, Dict, Any
from sqlmodel import Session, select

from config import settings
from models import RestaurantProfile, Distributor

PLACES_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
JINA_BASE = "https://r.jina.ai/"


def _scrape_categories(website_url: str) -> List[str]:
    """Use Jina Reader to extract plain text from a website, then infer supply categories."""
    if not website_url or not settings.jina_api_key:
        return []
    try:
        headers = {"Authorization": f"Bearer {settings.jina_api_key}"}
        resp = requests.get(f"{JINA_BASE}{website_url}", headers=headers, timeout=15)
        text = resp.text.lower()
        categories = []
        keyword_map = {
            "produce": ["produce", "vegetable", "fruit", "greens"],
            "meat": ["meat", "beef", "pork", "poultry", "chicken"],
            "seafood": ["seafood", "fish", "shrimp", "lobster"],
            "dairy": ["dairy", "cheese", "milk", "butter", "cream"],
            "dry goods": ["dry goods", "flour", "rice", "pasta", "grain"],
            "beverage": ["beverage", "drink", "juice", "soda"],
            "frozen": ["frozen", "ice cream", "freezer"],
        }
        for cat, keywords in keyword_map.items():
            if any(kw in text for kw in keywords):
                categories.append(cat)
        return categories
    except Exception:
        return []


def discover_distributors(profile: RestaurantProfile, session: Session) -> List[Distributor]:
    """Query Google Places for wholesale food distributors near the profile ZIP code."""
    if not settings.google_places_api_key:
        print("[places] GOOGLE_PLACES_API_KEY not set; skipping discovery")
        return []

    params = {
        "query": f"wholesale food distributor near {profile.zip_code}",
        "key": settings.google_places_api_key,
        "type": "food",
    }
    try:
        resp = requests.get(PLACES_URL, params=params, timeout=10)
        data = resp.json()
    except Exception as e:
        print(f"[places] API call failed: {e}")
        return []

    results = data.get("results", [])[:8]
    created = []

    for place in results:
        place_id = place.get("place_id", "")
        name = place.get("name", "Unknown")
        website = place.get("website", "")

        # Avoid duplicates
        existing = session.exec(
            select(Distributor)
            .where(Distributor.restaurant_profile_id == profile.id)
            .where(Distributor.google_place_id == place_id)
        ).first()
        if existing:
            continue

        categories = _scrape_categories(website)
        email_domain = profile.email.split("@")[1]
        demo_email = f"{name.lower().replace(' ', '_')}+demo@{email_domain}"

        dist = Distributor(
            restaurant_profile_id=profile.id,
            name=name,
            google_place_id=place_id,
            demo_routing_email=demo_email,
            supplied_categories=categories,
        )
        session.add(dist)
        created.append(dist)

    session.flush()
    return created
