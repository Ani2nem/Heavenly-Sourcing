"""Phase 5–6 — actionable manager alerts (dashboard + SMS dispatch)."""
from __future__ import annotations

import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from database import get_session
from models import ManagerAlert, RestaurantProfile

router = APIRouter(tags=["alerts"])


def _require_profile(session: Session) -> RestaurantProfile:
    profile = session.exec(select(RestaurantProfile)).first()
    if not profile:
        raise HTTPException(status_code=400, detail="Create a restaurant profile first.")
    return profile


def _serialize_alert(row: ManagerAlert) -> Dict[str, Any]:
    return {
        "id": str(row.id),
        "severity": row.severity,
        "title": row.title,
        "body": row.body,
        "action_url": row.action_url,
        "action_label": row.action_label,
        "grouping_key": row.grouping_key,
        "is_read": row.is_read,
        "delivered_sms_at": (
            row.delivered_sms_at.isoformat() if row.delivered_sms_at else None
        ),
        "created_at": row.created_at.isoformat(),
    }


@router.get("/alerts/manager")
def list_manager_alerts(session: Session = Depends(get_session)):
    profile = _require_profile(session)
    rows = session.exec(
        select(ManagerAlert)
        .where(ManagerAlert.restaurant_profile_id == profile.id)
        .order_by(ManagerAlert.created_at.desc())
    ).all()
    return [_serialize_alert(r) for r in rows]


@router.patch("/alerts/manager/{alert_id}/read")
def mark_manager_alert_read(alert_id: str, session: Session = Depends(get_session)):
    profile = _require_profile(session)
    try:
        aid = uuid.UUID(alert_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid alert_id")
    row = session.get(ManagerAlert, aid)
    if not row or row.restaurant_profile_id != profile.id:
        raise HTTPException(status_code=404, detail="Alert not found")
    row.is_read = True
    session.add(row)
    session.commit()
    session.refresh(row)
    return _serialize_alert(row)
