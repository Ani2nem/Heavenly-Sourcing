"""
Contract management API.

Endpoints
~~~~~~~~~

- POST   /api/contracts/upload    Upload a PDF / pasted text; runs the
                                  contract_parser extractor and returns
                                  a normalised extraction the wizard
                                  immediately renders for verification.
- POST   /api/contracts            Persist a contract from a (possibly
                                  manager-edited) extraction payload.
- GET    /api/contracts            List all contracts for the (single)
                                  active restaurant profile.
- GET    /api/contracts/{id}       Get one contract including line items
                                  and the verification status flags.
- POST   /api/contracts/{id}/verify  Mark the contract as verified.
- POST   /api/contracts/seed-demo  Create a believable Sysco-shaped
                                  contract for the demo. Idempotent on
                                  the seed nickname so repeated clicks
                                  don't multiply rows.
- POST   /api/contracts/skip        Record that the manager chose the
                                  "no existing contracts" path; flips
                                  onboarding_state to NEEDS_MENU so the
                                  wizard proceeds to the menu step.

Design notes
~~~~~~~~~~~~

The verifier flow is intentionally simple: the wizard receives the
extracted dict, the manager edits any fields they want to correct, then
the wizard sends the full dict back to POST /api/contracts. We do not
maintain a separate "extraction draft" table — the contract row itself
acts as the draft (status=DRAFT) until the manager hits "Looks Right"
which calls POST .../verify and flips status to ACTIVE.

This means a manager can come back later and re-edit the contract by
re-uploading or PUT-replacing fields, but for Phase 2 we only implement
the create-and-verify flow. Edit/replace can be a Phase 3 add-on without
schema changes.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from agents.contract_parser import (
    ALLOWED_CATEGORIES,
    ALLOWED_PRICING_STRUCTURES,
    CONTRACT_TERM_SECTIONS,
    KNOWN_TERM_KEYS,
    PRICING_STRUCTURE_DESCRIPTIONS,
    TERM_KEY_LABELS,
    build_demo_contract,
    extract_from_text,
    extract_text_from_upload,
)
from agents.contract_lifecycle import start_renewal_cycle
from database import get_session
from models import (
    Contract,
    ContractDocument,
    ContractLineItem,
    ManagerAlert,
    Negotiation,
    NegotiationRound,
    Notification,
    RestaurantProfile,
    Vendor,
    VendorRestaurantLink,
    VendorTrustScore,
)

router = APIRouter(tags=["contracts"])
log = logging.getLogger(__name__)


# ─── Request / response models ───────────────────────────────────────────────


class ContractUploadRequest(BaseModel):
    base64_content: str
    mime_type: str
    filename: Optional[str] = None


class ContractTextExtractRequest(BaseModel):
    """Convenience path for the demo: paste raw text without base64 dancing."""

    text: str
    filename: Optional[str] = None


class VendorPayload(BaseModel):
    name: str
    primary_domain: Optional[str] = None
    headquarters_city: Optional[str] = None
    headquarters_state: Optional[str] = None
    service_region: Optional[str] = None


class LineItemPayload(BaseModel):
    sku_name: str
    pack_description: Optional[str] = None
    unit_of_measure: Optional[str] = None
    fixed_price: Optional[float] = None
    price_formula: Optional[str] = None
    min_volume: Optional[float] = None
    min_volume_period: Optional[str] = None
    notes: Optional[str] = None


class ContractPayload(BaseModel):
    nickname: str
    vendor: VendorPayload
    primary_category: Optional[str] = None
    category_coverage: List[str] = []
    start_date: Optional[str] = None  # YYYY-MM-DD
    end_date: Optional[str] = None
    pricing_structure: str = "FIXED"
    line_items: List[LineItemPayload] = []
    extracted_terms: Dict[str, Any] = {}
    raw_text: Optional[str] = None
    raw_filename: Optional[str] = None
    source: str = "MANUAL_ENTRY"


class AwardNegotiationPayload(BaseModel):
    negotiation_id: str


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _require_profile(session: Session) -> RestaurantProfile:
    profile = session.exec(select(RestaurantProfile)).first()
    if not profile:
        raise HTTPException(
            status_code=400,
            detail="Create a restaurant profile first.",
        )
    return profile


def _normalize_slug(name: str) -> str:
    """Lowercase + alnum-only — used for Vendor.name_slug dedupe."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower()) or "vendor"


def _upsert_vendor(
    session: Session,
    profile: RestaurantProfile,
    payload: VendorPayload,
    source: str = "INCUMBENT_FROM_CONTRACT",
) -> Vendor:
    """Find or create a canonical Vendor + ensure a per-restaurant link.

    Dedupe order: primary_domain (when set) > name_slug. We deliberately
    avoid matching by exact `name` because contracts often spell the same
    counterparty inconsistently ("Sysco, Inc." vs "Sysco Corporation").
    """
    vendor: Optional[Vendor] = None
    if payload.primary_domain:
        vendor = session.exec(
            select(Vendor).where(Vendor.primary_domain == payload.primary_domain)
        ).first()
    if vendor is None:
        vendor = session.exec(
            select(Vendor).where(Vendor.name_slug == _normalize_slug(payload.name))
        ).first()

    if vendor is None:
        vendor = Vendor(
            name=payload.name,
            name_slug=_normalize_slug(payload.name),
            primary_domain=payload.primary_domain,
            headquarters_city=payload.headquarters_city,
            headquarters_state=payload.headquarters_state,
            service_region=payload.service_region,
            source=source,
        )
        session.add(vendor)
        session.flush()
    else:
        # Backfill enrichment fields when the contract has them and we
        # don't yet. Don't overwrite existing values — the manager may
        # have corrected them via the vendor edit UI.
        dirty = False
        for attr in ("primary_domain", "headquarters_city",
                      "headquarters_state", "service_region"):
            if getattr(vendor, attr) is None and getattr(payload, attr) is not None:
                setattr(vendor, attr, getattr(payload, attr))
                dirty = True
        if dirty:
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
            internal_alias=payload.name,
            verification_status="AUTO_TRUSTED" if source != "MANUAL_ENTRY"
                                 else "PENDING_DOMAIN_CHECK",
            is_active_incumbent=True,
        )
        session.add(link)
        session.flush()
    elif not link.is_active_incumbent:
        link.is_active_incumbent = True
        session.add(link)

    return vendor


def _parse_iso_date(s: Optional[str]):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except (TypeError, ValueError):
        return None


def _serialize_contract(
    session: Session,
    contract: Contract,
) -> Dict[str, Any]:
    line_items = session.exec(
        select(ContractLineItem).where(ContractLineItem.contract_id == contract.id)
    ).all()
    vendor: Optional[Vendor] = (
        session.get(Vendor, contract.vendor_id) if contract.vendor_id else None
    )
    return {
        "id": str(contract.id),
        "nickname": contract.nickname,
        "primary_category": contract.primary_category,
        "category_coverage": contract.category_coverage or [],
        "start_date": contract.start_date.isoformat() if contract.start_date else None,
        "end_date": contract.end_date.isoformat() if contract.end_date else None,
        "renewal_notice_days": contract.renewal_notice_days,
        "renewal_cycle_started_at": (
            contract.renewal_cycle_started_at.isoformat()
            if contract.renewal_cycle_started_at
            else None
        ),
        "pricing_structure": contract.pricing_structure,
        "status": contract.status,
        "source": contract.source,
        "manager_verified": contract.manager_verified,
        "verified_at": (
            contract.verified_at.isoformat() if contract.verified_at else None
        ),
        "vendor": (
            {
                "id": str(vendor.id),
                "name": vendor.name,
                "primary_domain": vendor.primary_domain,
                "headquarters_city": vendor.headquarters_city,
                "headquarters_state": vendor.headquarters_state,
                "service_region": vendor.service_region,
            }
            if vendor
            else None
        ),
        "line_items": [
            {
                "id": str(li.id),
                "sku_name": li.sku_name,
                "pack_description": li.pack_description,
                "unit_of_measure": li.unit_of_measure,
                "fixed_price": li.fixed_price,
                "price_formula": li.price_formula,
                "min_volume": li.min_volume,
                "min_volume_period": li.min_volume_period,
                "notes": li.notes,
            }
            for li in line_items
        ],
        "extracted_terms": contract.extracted_terms or {},
        "created_at": contract.created_at.isoformat(),
    }


def _latest_inbound_midpoint(session: Session, negotiation_id: uuid.UUID) -> Optional[float]:
    r = session.exec(
        select(NegotiationRound)
        .where(NegotiationRound.negotiation_id == negotiation_id)
        .where(NegotiationRound.direction == "INBOUND")
        .where(NegotiationRound.status == "RECEIVED")
        .order_by(NegotiationRound.round_index.desc())
    ).first()
    if not r or not r.offer_snapshot:
        return None
    m = r.offer_snapshot.get("avg_quote_midpoint")
    if isinstance(m, (int, float)) and m > 0:
        return float(m)
    return None


def _final_terms_from_latest_inbound(
    session: Session, negotiation_id: uuid.UUID
) -> Dict[str, Any]:
    r = session.exec(
        select(NegotiationRound)
        .where(NegotiationRound.negotiation_id == negotiation_id)
        .where(NegotiationRound.direction == "INBOUND")
        .where(NegotiationRound.status == "RECEIVED")
        .order_by(NegotiationRound.round_index.desc())
    ).first()
    if not r or not r.offer_snapshot:
        return {}
    return dict(r.offer_snapshot)


def _bump_onboarding_state(profile: RestaurantProfile, session: Session) -> None:
    """Advance profile.onboarding_state when we cross the contracts gate.

    Idempotent: re-running on an already-completed profile is a no-op.
    """
    if profile.onboarding_state in ("NEEDS_PROFILE", "NEEDS_CONTRACTS"):
        profile.onboarding_state = "NEEDS_MENU"
        session.add(profile)


# ─── Endpoints ───────────────────────────────────────────────────────────────


@router.get("/contracts/schema")
def contracts_schema():
    """Return the canonical enums the frontend dropdowns key against.

    Kept as an endpoint (vs hardcoded in the frontend) so adding a new
    pricing structure or category doesn't require a frontend redeploy.
    """
    return {
        "allowed_categories": ALLOWED_CATEGORIES,
        "allowed_pricing_structures": ALLOWED_PRICING_STRUCTURES,
        "known_term_keys": KNOWN_TERM_KEYS,
        "term_sections": CONTRACT_TERM_SECTIONS,
        "term_key_labels": TERM_KEY_LABELS,
        "pricing_structure_descriptions": PRICING_STRUCTURE_DESCRIPTIONS,
    }


@router.post("/contracts/upload")
def upload_contract(
    payload: ContractUploadRequest,
    session: Session = Depends(get_session),
):
    """Receive an uploaded PDF / text and return a normalised extraction.

    This endpoint does NOT persist anything — the wizard renders the
    extraction in the verifier UI, lets the manager edit fields, then
    POSTs the final dict back to /api/contracts to persist.
    """
    _require_profile(session)
    try:
        extracted = extract_text_from_upload(payload.base64_content, payload.mime_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    text = extracted.get("text") or ""
    if not text.strip():
        raise HTTPException(
            status_code=422,
            detail="Could not extract any text from the upload. If this is a "
                   "scanned PDF, paste the contract text directly.",
        )

    normalized = extract_from_text(text)
    normalized["raw_text"] = text
    normalized["raw_filename"] = payload.filename
    normalized["page_count"] = extracted.get("page_count", 0)
    normalized["source"] = "UPLOAD_PDF"
    return normalized


@router.post("/contracts/extract-text")
def extract_text_contract(
    payload: ContractTextExtractRequest,
    session: Session = Depends(get_session),
):
    """Same as /upload but accepts plain text without base64 dancing.

    Used by the wizard's "paste your contract instead" tab and by the
    demo seed path when an operator wants to tweak the seed text before
    persisting.
    """
    _require_profile(session)
    normalized = extract_from_text(payload.text)
    normalized["raw_text"] = payload.text
    normalized["raw_filename"] = payload.filename
    normalized["source"] = "MANUAL_ENTRY"
    return normalized


@router.post("/contracts", status_code=201)
def create_contract(
    payload: ContractPayload,
    session: Session = Depends(get_session),
):
    """Persist a contract from a (possibly manager-edited) extraction.

    Status starts at DRAFT; the manager flips it to ACTIVE via the
    /verify endpoint after they've eyeballed the extracted fields.
    """
    profile = _require_profile(session)
    vendor = _upsert_vendor(session, profile, payload.vendor, source=payload.source)

    contract = Contract(
        restaurant_profile_id=profile.id,
        vendor_id=vendor.id,
        nickname=payload.nickname[:255],
        primary_category=payload.primary_category,
        category_coverage=payload.category_coverage,
        start_date=_parse_iso_date(payload.start_date),
        end_date=_parse_iso_date(payload.end_date),
        pricing_structure=payload.pricing_structure,
        status="DRAFT",
        source=payload.source,
        raw_text=payload.raw_text,
        raw_filename=payload.raw_filename,
        extracted_terms=payload.extracted_terms or {},
    )
    session.add(contract)
    session.flush()

    for li in payload.line_items:
        session.add(ContractLineItem(
            contract_id=contract.id,
            sku_name=li.sku_name[:255],
            pack_description=li.pack_description,
            unit_of_measure=li.unit_of_measure,
            fixed_price=li.fixed_price,
            price_formula=li.price_formula,
            min_volume=li.min_volume,
            min_volume_period=li.min_volume_period,
            notes=li.notes,
        ))

    if payload.raw_text:
        session.add(ContractDocument(
            contract_id=contract.id,
            filename=payload.raw_filename or "contract.txt",
            mime_type="application/pdf" if payload.source == "UPLOAD_PDF" else "text/plain",
            extracted_text=payload.raw_text,
        ))

    _bump_onboarding_state(profile, session)
    session.commit()
    session.refresh(contract)
    return _serialize_contract(session, contract)


@router.get("/contracts")
def list_contracts(session: Session = Depends(get_session)):
    profile = session.exec(select(RestaurantProfile)).first()
    if not profile:
        return []
    contracts = session.exec(
        select(Contract)
        .where(Contract.restaurant_profile_id == profile.id)
        .order_by(Contract.created_at.desc())
    ).all()
    return [_serialize_contract(session, c) for c in contracts]


@router.get("/contracts/{contract_id}")
def get_contract(contract_id: str, session: Session = Depends(get_session)):
    try:
        cid = uuid.UUID(contract_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid contract_id")
    contract = session.get(Contract, cid)
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")
    return _serialize_contract(session, contract)


@router.post("/contracts/{contract_id}/verify")
def verify_contract(contract_id: str, session: Session = Depends(get_session)):
    """Manager has eyeballed the extracted fields and clicks 'Looks Right'.

    Flips status DRAFT → ACTIVE and stamps verified_at. The lifecycle
    agent (Phase 3) will refuse to operate on un-verified contracts.
    """
    try:
        cid = uuid.UUID(contract_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid contract_id")
    contract = session.get(Contract, cid)
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")
    contract.manager_verified = True
    contract.verified_at = datetime.utcnow()
    if contract.status == "DRAFT":
        contract.status = "ACTIVE"
    session.add(contract)
    session.commit()
    session.refresh(contract)
    return _serialize_contract(session, contract)


@router.post("/contracts/{contract_id}/start-renewal")
def start_contract_renewal(
    contract_id: str,
    force: bool = False,
    skip_email: bool = False,
    session: Session = Depends(get_session),
):
    """Manually kick the Phase 3 renewal / competitor RFP cycle for one contract.

    Normal eligibility mirrors :func:`agents.contract_lifecycle.start_renewal_cycle`:
    ACTIVE + verified + incumbent vendor + end_date inside ``renewal_notice_days``.
    ``force=True`` skips only the calendar / duplicate-cycle guards (still requires
    verified ACTIVE contract with vendor and future-ish term).
    """
    profile = _require_profile(session)
    try:
        cid = uuid.UUID(contract_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid contract_id")
    contract = session.get(Contract, cid)
    if not contract or contract.restaurant_profile_id != profile.id:
        raise HTTPException(status_code=404, detail="Contract not found")

    stats = start_renewal_cycle(session, cid, force=force, skip_email=skip_email)
    if not stats.get("started"):
        raise HTTPException(
            status_code=400,
            detail={
                "error": stats.get("reason"),
                "stats": stats,
            },
        )
    return stats


@router.get("/contracts/{contract_id}/negotiations")
def list_contract_negotiations(contract_id: str, session: Session = Depends(get_session)):
    """Return negotiations + rounds for a contract (Phase 3 UI / debugging)."""
    profile = _require_profile(session)
    try:
        cid = uuid.UUID(contract_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid contract_id")
    contract = session.get(Contract, cid)
    if not contract or contract.restaurant_profile_id != profile.id:
        raise HTTPException(status_code=404, detail="Contract not found")

    negotiations = session.exec(
        select(Negotiation).where(Negotiation.contract_id == cid)
    ).all()

    out: List[Dict[str, Any]] = []
    for neg in negotiations:
        vendor = session.get(Vendor, neg.vendor_id)
        rounds = session.exec(
            select(NegotiationRound)
            .where(NegotiationRound.negotiation_id == neg.id)
            .order_by(NegotiationRound.round_index)
        ).all()
        out.append(
            {
                "id": str(neg.id),
                "vendor_id": str(neg.vendor_id),
                "vendor_name": vendor.name if vendor else None,
                "intent": neg.intent,
                "status": neg.status,
                "max_rounds": neg.max_rounds,
                "rounds_used": neg.rounds_used,
                "rounds": [
                    {
                        "id": str(r.id),
                        "round_index": r.round_index,
                        "direction": r.direction,
                        "status": r.status,
                        "subject": r.subject,
                        "offer_snapshot": r.offer_snapshot,
                        "sent_at": r.sent_at.isoformat() if r.sent_at else None,
                        "received_at": (
                            r.received_at.isoformat() if r.received_at else None
                        ),
                        "created_at": r.created_at.isoformat(),
                    }
                    for r in rounds
                ],
            }
        )

    return {"contract_id": str(cid), "negotiations": out}


@router.get("/contracts/{contract_id}/decision-board")
def contract_decision_board(contract_id: str, session: Session = Depends(get_session)):
    """Phase 5 — aggregated view for picking a winning negotiation thread."""
    profile = _require_profile(session)
    try:
        cid = uuid.UUID(contract_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid contract_id")
    contract = session.get(Contract, cid)
    if not contract or contract.restaurant_profile_id != profile.id:
        raise HTTPException(status_code=404, detail="Contract not found")

    negs = session.exec(
        select(Negotiation).where(Negotiation.contract_id == cid)
    ).all()
    options: List[Dict[str, Any]] = []
    for neg in negs:
        vendor = session.get(Vendor, neg.vendor_id)
        trust = session.exec(
            select(VendorTrustScore)
            .where(VendorTrustScore.vendor_id == neg.vendor_id)
            .where(VendorTrustScore.restaurant_profile_id == profile.id)
        ).first()
        mid = _latest_inbound_midpoint(session, neg.id)
        bbb = None
        if vendor and vendor.public_signals and isinstance(vendor.public_signals, dict):
            bbb = (vendor.public_signals.get("bbb") or {}).get("rating")
        options.append(
            {
                "negotiation_id": str(neg.id),
                "vendor_id": str(neg.vendor_id),
                "vendor_name": vendor.name if vendor else None,
                "intent": neg.intent,
                "status": neg.status,
                "latest_quote_midpoint": mid,
                "trust_score": trust.trust_score if trust else None,
                "bbb_rating_stub": bbb,
                "public_yelp_source": (
                    (vendor.public_signals.get("yelp_b2b") or {}).get("source")
                    if vendor and isinstance(vendor.public_signals, dict)
                    else None
                ),
            }
        )

    return {
        "contract": {
            "id": str(contract.id),
            "nickname": contract.nickname,
            "status": contract.status,
            "vendor_id": str(contract.vendor_id) if contract.vendor_id else None,
        },
        "negotiations": options,
    }


@router.post("/contracts/{contract_id}/award")
def award_contract_negotiation(
    contract_id: str,
    payload: AwardNegotiationPayload,
    session: Session = Depends(get_session),
):
    """Phase 5 — record the winning vendor / negotiation for this contract."""
    profile = _require_profile(session)
    try:
        cid = uuid.UUID(contract_id)
        nid = uuid.UUID(payload.negotiation_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid id")

    contract = session.get(Contract, cid)
    if not contract or contract.restaurant_profile_id != profile.id:
        raise HTTPException(status_code=404, detail="Contract not found")

    winner = session.get(Negotiation, nid)
    if not winner or winner.contract_id != cid:
        raise HTTPException(status_code=404, detail="Negotiation not found for contract")

    snapshot = _final_terms_from_latest_inbound(session, winner.id)
    now = datetime.utcnow()

    all_negs = session.exec(
        select(Negotiation).where(Negotiation.contract_id == cid)
    ).all()
    for n in all_negs:
        if n.id == winner.id:
            n.status = "CLOSED_WON"
            n.final_terms_snapshot = snapshot or {}
            n.closed_at = now
        elif n.status != "CLOSED_LOST":
            n.status = "CLOSED_LOST"
            n.closed_at = now
        session.add(n)

    win_vendor = session.get(Vendor, winner.vendor_id)
    contract.vendor_id = winner.vendor_id
    contract.status = "ACTIVE"
    contract.updated_at = now
    session.add(contract)

    session.add(
        Notification(
            title="Contract award recorded",
            message=(
                f"{contract.nickname}: selected {win_vendor.name if win_vendor else 'vendor'} "
                "as the recorded counterparty for this agreement cycle."
            ),
        )
    )
    session.add(
        ManagerAlert(
            restaurant_profile_id=profile.id,
            grouping_key=str(contract.id),
            severity="INFO",
            title=f"Award saved — {contract.nickname}",
            body=(
                f"You selected {win_vendor.name if win_vendor else 'a vendor'} "
                "in the decision board. Generate paperwork offline if required."
            ),
            action_url="/contracts",
            action_label="Open Contracts",
        )
    )
    session.commit()
    return {"ok": True, "contract_id": str(cid), "winning_negotiation_id": str(nid)}


@router.post("/contracts/seed-demo", status_code=201)
def seed_demo_contract(session: Session = Depends(get_session)):
    """Create a believable Sysco-shaped contract so the demo can show off
    the verifier UI + lifecycle agent (when wired in Phase 3) without
    needing a real PDF.

    Idempotent on the seed nickname — running it twice doesn't duplicate.
    """
    profile = _require_profile(session)
    seed = build_demo_contract()

    existing = session.exec(
        select(Contract)
        .where(Contract.restaurant_profile_id == profile.id)
        .where(Contract.nickname == seed["nickname"])
    ).first()
    if existing:
        return _serialize_contract(session, existing)

    vendor_payload = VendorPayload(**seed["vendor"])
    vendor = _upsert_vendor(session, profile, vendor_payload, source="DEMO_SEED")

    contract = Contract(
        restaurant_profile_id=profile.id,
        vendor_id=vendor.id,
        nickname=seed["nickname"][:255],
        primary_category=seed["primary_category"],
        category_coverage=seed["category_coverage"],
        start_date=seed.get("start_date"),
        end_date=seed.get("end_date"),
        pricing_structure=seed["pricing_structure"],
        status="DRAFT",
        source="DEMO_SEED",
        raw_text=None,
        extracted_terms=seed["extracted_terms"],
    )
    session.add(contract)
    session.flush()

    for li in seed["line_items"]:
        session.add(ContractLineItem(
            contract_id=contract.id,
            sku_name=li["sku_name"],
            pack_description=li.get("pack_description"),
            unit_of_measure=li.get("unit_of_measure"),
            fixed_price=li.get("fixed_price"),
            price_formula=li.get("price_formula"),
            min_volume=li.get("min_volume"),
            min_volume_period=li.get("min_volume_period"),
            notes=li.get("notes"),
        ))

    _bump_onboarding_state(profile, session)
    session.commit()
    session.refresh(contract)
    return _serialize_contract(session, contract)


@router.post("/contracts/skip")
def skip_contracts(session: Session = Depends(get_session)):
    """Manager picked the 'no existing contracts' onboarding option.

    We don't create a placeholder contract — the manager has nothing yet.
    We just advance onboarding_state so the wizard moves to the menu step
    where the system will derive a starting category list for them.
    """
    profile = _require_profile(session)
    _bump_onboarding_state(profile, session)
    session.commit()
    return {"onboarding_state": profile.onboarding_state}
