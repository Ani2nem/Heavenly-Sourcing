"""
Negotiation models — multi-round bargaining state for a contract renewal
or a brand-new contract RFP.

A `Negotiation` is one back-and-forth thread between the restaurant and
ONE vendor for ONE contract decision. A contract renewal opens parallel
negotiations: one with the incumbent, plus up to N with similar vendors.
Each negotiation has its own rounds; the comparison agent reads across
them at decision time.

These models are wired in Phase 3 (lifecycle agent). For Phase 1 we just
create the tables so migrations are stable and the schema doesn't need
another disruptive change later.
"""
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON
from sqlmodel import Column, Field, SQLModel


class Negotiation(SQLModel, table=True):
    """Header for one vendor-thread inside a contract decision."""

    __tablename__ = "negotiations"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # The contract this negotiation is about — could be a renewal of an
    # existing contract, or a brand-new one we're trying to sign.
    contract_id: uuid.UUID = Field(foreign_key="contracts.id", index=True)
    vendor_id: uuid.UUID = Field(foreign_key="vendors.id", index=True)

    # NEW_CONTRACT | RENEWAL | EMERGENCY_BUY
    intent: str = Field(default="NEW_CONTRACT")

    # OPEN | AWAITING_VENDOR | AWAITING_MANAGER_APPROVAL | CLOSED_WON | CLOSED_LOST
    status: str = Field(default="OPEN", index=True)

    # Cap on outbound rounds so the agent never runs forever. Default 3
    # matches the "no infinite ping-pong" rule in scoring_engine.
    max_rounds: int = Field(default=3)
    rounds_used: int = Field(default=0)

    # Final terms the manager approved (after Phase 5 ContractDecisionBoard).
    final_terms_snapshot: Optional[Any] = Field(default=None, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=datetime.utcnow)
    closed_at: Optional[datetime] = None


class NegotiationRound(SQLModel, table=True):
    """One outbound or inbound message in a negotiation thread.

    `direction` is OUTBOUND when we emailed the vendor, INBOUND when we
    parsed their reply. The agent appends a new round each time it has
    something new to say or has parsed a new reply.

    `manager_approved_to_send`: when our prompt produces an outbound
    message that contains a final-shaped offer (single number, no range),
    the agent freezes the round in DRAFT and surfaces it to the manager
    for one-click approval. Ranges and exploratory language ship without
    this gate.
    """

    __tablename__ = "negotiation_rounds"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    negotiation_id: uuid.UUID = Field(foreign_key="negotiations.id", index=True)

    round_index: int = Field(default=0)
    direction: str = Field(default="OUTBOUND")  # OUTBOUND | INBOUND

    # DRAFT | SENT | RECEIVED | NEEDS_APPROVAL
    status: str = Field(default="DRAFT")

    subject: Optional[str] = None
    body: Optional[str] = None

    # Structured snapshot of the offer parsed (inbound) or proposed
    # (outbound). Shape:
    #   {"target_range": {"low": 4.10, "high": 4.30, "unit": "lb"},
    #    "term_months": 12,
    #    "moq": 50,
    #    "flex": ["fuel_surcharge_waived", "monthly_renegotiation_clause"]}
    offer_snapshot: Optional[Any] = Field(default=None, sa_column=Column(JSON))

    # When True, an outbound round will NOT be auto-sent — the manager
    # must approve via the ContractDecisionBoard. We set this whenever the
    # generated message contains a hard final number (vs a range).
    manager_approved_to_send: Optional[bool] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    sent_at: Optional[datetime] = None
    received_at: Optional[datetime] = None
