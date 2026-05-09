from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from pydantic import BaseModel, EmailStr

from database import get_session
from models import RestaurantProfile

router = APIRouter(tags=["profile"])


class ProfileCreate(BaseModel):
    name: str
    zip_code: str
    city: str
    state: str
    email: EmailStr


@router.post("/profile", status_code=201)
def create_profile(payload: ProfileCreate, session: Session = Depends(get_session)):
    existing = session.exec(select(RestaurantProfile)).first()
    if existing:
        raise HTTPException(status_code=409, detail="Profile already exists")

    profile = RestaurantProfile(**payload.model_dump())
    session.add(profile)
    session.commit()
    session.refresh(profile)
    return {"id": str(profile.id), "created_at": profile.created_at.isoformat()}


@router.get("/profile")
def get_profile(session: Session = Depends(get_session)):
    profile = session.exec(select(RestaurantProfile)).first()
    if not profile:
        return None
    return {
        "id": str(profile.id),
        "name": profile.name,
        "zip_code": profile.zip_code,
        "city": profile.city,
        "state": profile.state,
        "email": profile.email,
        "created_at": profile.created_at.isoformat(),
    }
