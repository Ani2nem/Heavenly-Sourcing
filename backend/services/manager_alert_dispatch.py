"""Multi-channel delivery hooks for :class:`models.alerts.ManagerAlert`."""
from __future__ import annotations

from datetime import datetime

from models import ManagerAlert, RestaurantProfile
from sqlmodel import Session

from services.sms_daemon import send_sms


def deliver_manager_alert_sms(
    session: Session,
    alert: ManagerAlert,
    profile: RestaurantProfile,
) -> None:
    """Send SMS for actionable severities when the profile opted in."""

    if alert.severity not in ("ACTION_REQUIRED", "URGENT"):
        return
    if not profile.sms_alerts_opt_in:
        return
    phone = (profile.phone_number or "").strip()
    if not phone:
        return
    if alert.delivered_sms_at is not None:
        return

    text = f"HeavenlySourcing — {alert.title}\n{alert.body}"
    if send_sms(phone, text):
        alert.delivered_sms_at = datetime.utcnow()
        session.add(alert)
