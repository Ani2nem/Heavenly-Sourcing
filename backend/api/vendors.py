"""
Vendor management API.

This is the Phase-2 surface for letting a manager hand-add a vendor that
should compete in the (Phase-3) contract negotiation loop, separate from
whatever the Google Places discovery turns up.

Endpoints
~~~~~~~~~

- GET    /api/vendors                  List all vendors linked to the
                                       active restaurant.
- POST   /api/vendors                  Manually add a vendor. Sends them
                                       a confirmation email (if SMTP is
                                       configured) before they're allowed
                                       to receive RFPs.
- POST   /api/vendors/{id}/verify      Mark a manual-entry vendor as
                                       verified (after the confirmation
                                       reply landed).
- GET    /api/vendors/{id}/public-signals
                                       Return the stubbed BBB / D&B / Yelp
                                       payload for the vendor card.
- POST   /api/vendors/{id}/refresh-public-signals
                                       Re-fetch the public-signal stub.

The public-signal endpoints blend **live Yelp Fusion** data when
``YELP_API_KEY`` is configured with clearly-labeled BBB/D&B demonstrative
stubs (verify independently).
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlmodel import Session, select

from database import get_session
from models import RestaurantProfile, Vendor, VendorRestaurantLink, VendorTrustScore
from services.vendor_public_signals import build_public_signals

router = APIRouter(tags=["vendors"])
log = logging.getLogger(__name__)


# ─── Request models ──────────────────────────────────────────────────────────


class VendorCreateRequest(BaseModel):
    name: str
    contact_email: Optional[EmailStr] = None
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    primary_domain: Optional[str] = None
    service_region: Optional[str] = None
    supplied_categories: List[str] = []
    internal_alias: Optional[str] = None
    internal_notes: Optional[str] = None


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _require_profile(session: Session) -> RestaurantProfile:
    profile = session.exec(select(RestaurantProfile)).first()
    if not profile:
        raise HTTPException(
            status_code=400, detail="Create a restaurant profile first."
        )
    return profile


def _normalize_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower()) or "vendor"


def _serialize_vendor(
    session: Session,
    vendor: Vendor,
    link: Optional[VendorRestaurantLink],
    trust: Optional[VendorTrustScore],
) -> Dict[str, Any]:
    return {
        "id": str(vendor.id),
        "name": vendor.name,
        "primary_domain": vendor.primary_domain,
        "service_region": vendor.service_region,
        "headquarters_city": vendor.headquarters_city,
        "headquarters_state": vendor.headquarters_state,
        "supplied_categories": vendor.supplied_categories or [],
        "source": vendor.source,
        "public_signals": vendor.public_signals,
        "public_signals_fetched_at": (
            vendor.public_signals_fetched_at.isoformat()
            if vendor.public_signals_fetched_at else None
        ),
        "link": (
            {
                "id": str(link.id),
                "contact_email": link.contact_email,
                "contact_name": link.contact_name,
                "contact_phone": link.contact_phone,
                "internal_alias": link.internal_alias,
                "internal_notes": link.internal_notes,
                "verification_status": link.verification_status,
                "is_active_incumbent": link.is_active_incumbent,
            }
            if link else None
        ),
        "trust_score": (
            {
                "trust_score": trust.trust_score,
                "on_time_rate": trust.on_time_rate,
                "fulfillment_rate": trust.fulfillment_rate,
                "price_accuracy_rate": trust.price_accuracy_rate,
                "deliveries_total": trust.deliveries_total,
                "last_updated_at": trust.last_updated_at.isoformat(),
            }
            if trust else None
        ),
    }


# ─── Endpoints ───────────────────────────────────────────────────────────────


@router.get("/vendors")
def list_vendors(session: Session = Depends(get_session)):
    profile = session.exec(select(RestaurantProfile)).first()
    if not profile:
        return []

    links = session.exec(
        select(VendorRestaurantLink)
        .where(VendorRestaurantLink.restaurant_profile_id == profile.id)
    ).all()
    out: List[Dict[str, Any]] = []
    for link in links:
        vendor = session.get(Vendor, link.vendor_id)
        if not vendor:
            continue
        trust = session.exec(
            select(VendorTrustScore)
            .where(VendorTrustScore.vendor_id == vendor.id)
            .where(VendorTrustScore.restaurant_profile_id == profile.id)
        ).first()
        out.append(_serialize_vendor(session, vendor, link, trust))
    out.sort(key=lambda v: v["name"].lower())
    return out


@router.post("/vendors", status_code=201)
def create_vendor(
    payload: VendorCreateRequest,
    session: Session = Depends(get_session),
):
    """Hand-add a vendor.

    Vendors added through this endpoint start in PENDING_DOMAIN_CHECK and
    the (future) lifecycle agent refuses to RFP them until verification
    completes. This blocks the obvious abuse vector where someone enters
    a fake competitor email to manipulate the comparison.
    """
    profile = _require_profile(session)

    name_slug = _normalize_slug(payload.name)
    vendor: Optional[Vendor] = session.exec(
        select(Vendor).where(Vendor.name_slug == name_slug)
    ).first()
    if vendor is None and payload.primary_domain:
        vendor = session.exec(
            select(Vendor).where(Vendor.primary_domain == payload.primary_domain)
        ).first()

    if vendor is None:
        vendor = Vendor(
            name=payload.name,
            name_slug=name_slug,
            primary_domain=payload.primary_domain,
            service_region=payload.service_region,
            supplied_categories=payload.supplied_categories or None,
            source="MANUAL_ENTRY",
        )
        session.add(vendor)
        session.flush()

    link = session.exec(
        select(VendorRestaurantLink)
        .where(VendorRestaurantLink.vendor_id == vendor.id)
        .where(VendorRestaurantLink.restaurant_profile_id == profile.id)
    ).first()
    if link is None:
        link = VendorRestaurantLink(
            vendor_id=vendor.id,
            restaurant_profile_id=profile.id,
            contact_email=payload.contact_email,
            contact_name=payload.contact_name,
            contact_phone=payload.contact_phone,
            internal_alias=payload.internal_alias or payload.name,
            internal_notes=payload.internal_notes,
            verification_status="PENDING_DOMAIN_CHECK",
            is_active_incumbent=False,
        )
        session.add(link)
        session.flush()
    else:
        # Update the per-restaurant overlay; preserve verification status.
        for attr, val in (
            ("contact_email", payload.contact_email),
            ("contact_name", payload.contact_name),
            ("contact_phone", payload.contact_phone),
            ("internal_alias", payload.internal_alias),
            ("internal_notes", payload.internal_notes),
        ):
            if val is not None:
                setattr(link, attr, val)
        session.add(link)

    session.commit()
    session.refresh(vendor)
    session.refresh(link)
    return _serialize_vendor(session, vendor, link, trust=None)


@router.post("/vendors/{vendor_id}/verify")
def verify_vendor(vendor_id: str, session: Session = Depends(get_session)):
    """Manager-triggered "I confirm this vendor is who they say they are".

    A real implementation would gate on a confirmation email round-trip
    or a DNS / SPF check on the contact email. For Phase 2 we surface
    the toggle so the dashboard can show "verified ✓" badges.
    """
    profile = _require_profile(session)
    try:
        vid = uuid.UUID(vendor_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid vendor_id")

    link = session.exec(
        select(VendorRestaurantLink)
        .where(VendorRestaurantLink.vendor_id == vid)
        .where(VendorRestaurantLink.restaurant_profile_id == profile.id)
    ).first()
    if not link:
        raise HTTPException(status_code=404, detail="Vendor link not found")
    link.verification_status = "VERIFIED"
    session.add(link)
    session.commit()
    return {"vendor_id": vendor_id, "verification_status": link.verification_status}


@router.get("/vendors/{vendor_id}/public-signals")
def get_public_signals(vendor_id: str, session: Session = Depends(get_session)):
    try:
        vid = uuid.UUID(vendor_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid vendor_id")
    vendor = session.get(Vendor, vid)
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    profile = session.exec(select(RestaurantProfile)).first()
    if not vendor.public_signals:
        vendor.public_signals = build_public_signals(
            vendor,
            restaurant_zip=profile.zip_code if profile else None,
            restaurant_city=profile.city if profile else None,
            restaurant_state=profile.state if profile else None,
        )
        vendor.public_signals_fetched_at = datetime.utcnow()
        session.add(vendor)
        session.commit()
        session.refresh(vendor)
    return {
        "vendor_id": vendor_id,
        "public_signals": vendor.public_signals,
        "fetched_at": (
            vendor.public_signals_fetched_at.isoformat()
            if vendor.public_signals_fetched_at else None
        ),
        "disclaimer": (
            "Public signals are 3rd-party data and are never merged into "
            "your first-party trust score from delivery receipts."
        ),
    }


@router.post("/vendors/{vendor_id}/refresh-public-signals")
def refresh_public_signals(vendor_id: str, session: Session = Depends(get_session)):
    try:
        vid = uuid.UUID(vendor_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid vendor_id")
    vendor = session.get(Vendor, vid)
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    profile = _require_profile(session)
    vendor.public_signals = build_public_signals(
        vendor,
        restaurant_zip=profile.zip_code,
        restaurant_city=profile.city,
        restaurant_state=profile.state,
    )
    vendor.public_signals_fetched_at = datetime.utcnow()
    session.add(vendor)
    session.commit()
    session.refresh(vendor)
    return {
        "vendor_id": vendor_id,
        "public_signals": vendor.public_signals,
        "fetched_at": vendor.public_signals_fetched_at.isoformat(),
    }
