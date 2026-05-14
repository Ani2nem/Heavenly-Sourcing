"""Phase 4 — vendor public-signal enrichment.

Live **Yelp Fusion** ratings when ``settings.yelp_api_key`` is set.
BBB and D&B remain clearly labeled demonstrative stubs (no silent merging
with live data) unless a future enterprise integration is configured.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

from config import settings

log = logging.getLogger(__name__)


def _deterministic_stub_rng(vendor_id_int: int) -> random.Random:
    seed = int(vendor_id_int % (2 ** 32))
    return random.Random(seed)


def _bbb_dnb_stub(vendor) -> Dict[str, Any]:
    rng = _deterministic_stub_rng(vendor.id.int)
    bbb_grades = ["A+", "A", "A-", "B+", "B", "B-", "C+", "Not Rated"]
    today = datetime.now(timezone.utc).date().isoformat()
    return {
        "bbb": {
            "rating": rng.choice(bbb_grades),
            "lookup_url": f"https://www.bbb.org/search?find_text={vendor.name_slug}",
            "source": "stub_demonstrative",
            "disclaimer": "Illustrative grade for UI demos — verify live data at bbb.org.",
        },
        "dnb": {
            "credit_score": rng.randint(55, 92),
            "source_date": today,
            "source": "stub_demonstrative",
            "disclaimer": "Synthetic score — not Dun & Bradstreet data.",
        },
    }


def _yelp_best_match(
    term: str,
    location: str,
) -> Optional[Dict[str, Any]]:
    key = (settings.yelp_api_key or "").strip()
    if not key:
        return None
    try:
        r = requests.get(
            "https://api.yelp.com/v3/businesses/search",
            headers={"Authorization": f"Bearer {key}"},
            params={"term": term, "location": location, "limit": 5},
            timeout=15,
        )
        r.raise_for_status()
        businesses = (r.json() or {}).get("businesses") or []
        return businesses[0] if businesses else None
    except Exception as exc:
        log.warning("[vendor_signals] Yelp search failed: %s", exc)
        return None


def build_public_signals(
    vendor,
    *,
    restaurant_zip: Optional[str] = None,
    restaurant_city: Optional[str] = None,
    restaurant_state: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a JSON blob suitable for ``Vendor.public_signals``."""

    loc_parts = [
        p for p in (restaurant_city, restaurant_state, restaurant_zip) if p
    ]
    location = ", ".join(loc_parts) if loc_parts else "United States"

    stubs = _bbb_dnb_stub(vendor)

    biz = _yelp_best_match(vendor.name, location)
    if biz:
        yelp_b2b: Dict[str, Any] = {
            "source": "yelp_fusion_api",
            "name": biz.get("name"),
            "stars": biz.get("rating"),
            "n_reviews": biz.get("review_count"),
            "url": biz.get("url"),
            "business_id": biz.get("id"),
        }
        evidence_yelp = "Yelp Fusion API match"
    else:
        rng = _deterministic_stub_rng(vendor.id.int)
        yelp_b2b = {
            "source": "stub_demonstrative",
            "stars": round(rng.uniform(3.2, 4.8), 1),
            "n_reviews": rng.randint(4, 240),
            "disclaimer": "No Yelp API key or no search match — showing illustrative values.",
        }
        evidence_yelp = "Yelp stub (missing API key or no match)"

    return {
        "bbb": stubs["bbb"],
        "dnb": stubs["dnb"],
        "yelp_b2b": yelp_b2b,
        "evidence": (
            f"{evidence_yelp}. BBB/D&B entries are labeled stubs — verify independently."
        ),
    }
