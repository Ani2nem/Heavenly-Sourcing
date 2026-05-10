import uuid
from datetime import datetime
from typing import Optional, Any
from sqlmodel import SQLModel, Field, Column
from sqlalchemy import JSON


class Distributor(SQLModel, table=True):
    __tablename__ = "distributors"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    restaurant_profile_id: uuid.UUID = Field(foreign_key="restaurant_profiles.id")
    name: str
    google_place_id: Optional[str] = None
    demo_routing_email: Optional[str] = None
    supplied_categories: Optional[Any] = Field(default=None, sa_column=Column(JSON))


class DistributorQuote(SQLModel, table=True):
    __tablename__ = "distributor_quotes"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    procurement_cycle_id: uuid.UUID = Field(foreign_key="procurement_cycles.id")
    distributor_id: uuid.UUID = Field(foreign_key="distributors.id")
    quote_status: str = Field(default="PENDING")
    total_quoted_price: Optional[float] = None
    received_at: Optional[datetime] = None
    score: Optional[float] = None
    recommendation_text: Optional[str] = None


class DistributorQuoteItem(SQLModel, table=True):
    __tablename__ = "distributor_quote_items"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    distributor_quote_id: uuid.UUID = Field(foreign_key="distributor_quotes.id")
    ingredient_id: uuid.UUID = Field(foreign_key="ingredients.id")
    quoted_price_per_unit: Optional[float] = None


class Notification(SQLModel, table=True):
    __tablename__ = "notifications"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    title: str
    message: str
    is_read: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PurchaseReceipt(SQLModel, table=True):
    __tablename__ = "purchase_receipts"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    procurement_cycle_id: uuid.UUID = Field(foreign_key="procurement_cycles.id")
    distributor_quote_id: uuid.UUID = Field(foreign_key="distributor_quotes.id")
    distributor_id: uuid.UUID = Field(foreign_key="distributors.id")
    receipt_number: Optional[str] = None
    total_amount: Optional[float] = None
    line_items: Optional[Any] = Field(default=None, sa_column=Column(JSON))
    raw_email_subject: Optional[str] = None
    raw_email_excerpt: Optional[str] = None
    received_at: datetime = Field(default_factory=datetime.utcnow)
