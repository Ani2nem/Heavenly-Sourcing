"""Discover competitor vendors for contract renewal RFPs (Phase 3).

Reuses Google Places via ``places_discovery.fetch_places_near_zip`` but
persists canonical ``Vendor`` + ``VendorRestaurantLink`` rows instead of
``Distributor``, and caps results at ``MAX_CONTRACT_COMPETITORS`` (4).

Skips the incumbent by ``google_place_id`` match and by normalised name
slug overlap so we don't RFP the same counterparty twice.
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional, Set

from sqlmodel import Session, select

from models import RestaurantProfile, Vendor, VendorRestaurantLink
from services.places_discovery import (
    MAX_CONTRACT_COMPETITORS,
    build_demo_routing_email,
    fetch_places_near_zip,
    _categories_from_place,  # noqa: SLF001
    _scrape_categories,     # noqa: SLF001
)

from config import settings

log = logging.getLogger(__name__)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower()) or "vendor"


def discover_competitor_vendors(
    profile: RestaurantProfile,
    session: Session,
    incumbent: Vendor,
    *,
    max_count: int = MAX_CONTRACT_COMPETITORS,
) -> List[Vendor]:
    """Return up to ``max_count`` ``Vendor`` rows suitable for competitor RFPs.

    Idempotent: existing vendors matched by ``google_place_id`` are reused;
    missing ``VendorRestaurantLink`` rows are created with a demo routing
    email plus ``AUTO_TRUSTED`` verification (Places-derived identities).
    """
    zip_code = (profile.zip_code or "").strip()
    if not zip_code:
        log.warning("[contract-competitors] no profile zip — skipping Places discovery")
        return []

    places = fetch_places_near_zip(zip_code, max_places=max_count + 4)
    if not places:
        log.info("[contract-competitors] Places returned 0 candidates for zip=%s", zip_code)
        return []

    inc_place = (incumbent.google_place_id or "").strip()
    inc_slug = _slug(incumbent.name)
    seen_place_ids: Set[str] = set()
    out: List[Vendor] = []

    for place in places:
        if len(out) >= max_count:
            break

        place_id = (place.get("id") or "").strip()
        name = (place.get("displayName") or {}).get("text") or "Unknown"
        slug = _slug(name)

        if place_id and place_id in seen_place_ids:
            continue
        if inc_place and place_id == inc_place:
            continue
        if slug == inc_slug:
            continue
        # Loose duplicate: same slug prefix as incumbent (short names collide less).
        if inc_slug and slug.startswith(inc_slug[: min(8, len(inc_slug))]):
            continue

        if place_id:
            seen_place_ids.add(place_id)

        vendor = session.exec(
            select(Vendor).where(Vendor.google_place_id == place_id)
        ).first() if place_id else None

        if vendor is None:
            vendor = session.exec(
                select(Vendor).where(Vendor.name_slug == slug)
            ).first()

        website = place.get("websiteUri") or ""
        categories = _scrape_categories(website) if settings.jina_api_key else []
        if not categories:
            categories = _categories_from_place(place)

        if vendor is None:
            vendor = Vendor(
                name=name,
                name_slug=slug,
                google_place_id=place_id or None,
                supplied_categories=categories or None,
                service_region=None,
                source="DISCOVERED_PLACES",
            )
            session.add(vendor)
            session.flush()
        else:
            # Enrich missing scrape data opportunistically.
            if not vendor.supplied_categories and categories:
                vendor.supplied_categories = categories
                session.add(vendor)
                session.flush()

        link = session.exec(
            select(VendorRestaurantLink)
            .where(VendorRestaurantLink.vendor_id == vendor.id)
            .where(VendorRestaurantLink.restaurant_profile_id == profile.id)
        ).first()

        contact_email = build_demo_routing_email(profile.email, name)
        if link is None:
            session.add(VendorRestaurantLink(
                vendor_id=vendor.id,
                restaurant_profile_id=profile.id,
                contact_email=contact_email,
                internal_alias=name,
                verification_status="AUTO_TRUSTED",
                is_active_incumbent=False,
            ))
            session.flush()
        elif not link.contact_email:
            link.contact_email = contact_email
            session.add(link)
            session.flush()

        out.append(vendor)

    session.flush()
    log.info(
        "[contract-competitors] selected %d competitor vendor(s) for zip=%s",
        len(out), zip_code,
    )
    return out
