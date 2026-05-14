"""
Restaurant profile router.

Post-pivot shape: location fields (zip/city/state) and phone are all
optional. The required minimum to create a profile is name + email,
since contracts are the primary spine and location is only a fallback
signal for Google Places discovery and the future emergency-buy flow.

The profile also exposes `onboarding_state`, which is the single source
of truth the frontend wizard uses to decide where to send the user
next: NEEDS_PROFILE → NEEDS_CONTRACTS → NEEDS_MENU → COMPLETED.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlmodel import Session, select

from database import get_session
from models import RestaurantProfile

router = APIRouter(tags=["profile"])


class ProfileCreate(BaseModel):
    name: str
    email: EmailStr
    zip_code: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    phone_number: Optional[str] = None
    sms_alerts_opt_in: bool = False


class ProfileUpdate(BaseModel):
    """All fields optional — PATCH-style update used by the settings page."""

    name: Optional[str] = None
    email: Optional[EmailStr] = None
    zip_code: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    phone_number: Optional[str] = None
    sms_alerts_opt_in: Optional[bool] = None


def _serialize(profile: RestaurantProfile) -> dict:
    return {
        "id": str(profile.id),
        "name": profile.name,
        "email": profile.email,
        "zip_code": profile.zip_code,
        "city": profile.city,
        "state": profile.state,
        "phone_number": profile.phone_number,
        "sms_alerts_opt_in": profile.sms_alerts_opt_in,
        "onboarding_state": profile.onboarding_state,
        "created_at": profile.created_at.isoformat(),
    }


@router.post("/profile", status_code=201)
def create_profile(payload: ProfileCreate, session: Session = Depends(get_session)):
    existing = session.exec(select(RestaurantProfile)).first()
    if existing:
        raise HTTPException(status_code=409, detail="Profile already exists")

    profile = RestaurantProfile(
        **payload.model_dump(),
        onboarding_state="NEEDS_CONTRACTS",
    )
    session.add(profile)
    session.commit()
    session.refresh(profile)
    return _serialize(profile)


@router.get("/profile")
def get_profile(session: Session = Depends(get_session)):
    profile = session.exec(select(RestaurantProfile)).first()
    if not profile:
        return None
    return _serialize(profile)


@router.patch("/profile")
def patch_profile(
    payload: ProfileUpdate,
    session: Session = Depends(get_session),
):
    """Update the (single) restaurant profile in place.

    Used by the settings page to add a phone number after onboarding
    (so SMS alerts can be opted into later), and to backfill ZIP if the
    manager skipped it during initial setup.
    """
    profile = session.exec(select(RestaurantProfile)).first()
    if not profile:
        raise HTTPException(status_code=404, detail="No profile to update")

    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(profile, key, value)
    session.add(profile)
    session.commit()
    session.refresh(profile)
    return _serialize(profile)
