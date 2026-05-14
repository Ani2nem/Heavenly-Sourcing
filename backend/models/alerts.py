"""
ManagerAlert — a multi-channel notification with severity and an action URL.

This is a strict superset of the existing `Notification` model. The old
notification table is a low-friction "show a toast" stream; ManagerAlert
is for things the manager has to *do* (sign a contract, approve a round,
investigate a short-delivery). Different bells, different inbox UX.

We keep them as separate tables (rather than adding fields to Notification)
because the existing notifications router and the toast frontend already
have a tight contract around the old shape, and we don't want to
risk regressions in the demo flow.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class ManagerAlert(SQLModel, table=True):
    __tablename__ = "manager_alerts"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    restaurant_profile_id: uuid.UUID = Field(
        foreign_key="restaurant_profiles.id", index=True
    )

    # Free-form group key so the dashboard can collapse related alerts.
    # Examples: contract_id, negotiation_id, vendor_id (stringified).
    grouping_key: Optional[str] = Field(default=None, index=True)

    # INFO | ACTION_REQUIRED | URGENT
    severity: str = Field(default="INFO")

    title: str
    body: str

    # Where the manager should be sent when they click the alert.
    action_url: Optional[str] = None
    action_label: Optional[str] = None

    # Delivery tracking. Each delivered_at is set when the corresponding
    # transport actually shipped (or is None when the channel was not
    # configured / opted out). The dashboard is always-on; email and SMS
    # are explicit opt-in fields that depend on profile + env config.
    delivered_dashboard: bool = Field(default=True)
    delivered_email_at: Optional[datetime] = None
    delivered_sms_at: Optional[datetime] = None

    is_read: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)
