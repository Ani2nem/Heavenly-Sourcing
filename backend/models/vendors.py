"""
Vendor models — the canonical, non-restaurant-scoped identity of a supplier.

Why a new `Vendor` table alongside the existing `Distributor`?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The legacy `Distributor` table is per-restaurant (FK to restaurant_profiles)
because it was populated by Places discovery for one restaurant at a time.
That worked for weekly spot quotes but is wrong for contracts:

  * The same Sysco depot serves dozens of restaurants and we want to share
    enrichment data + trust signals across them, not re-discover per
    customer.
  * "Manually add a vendor" needs a global identity row that can be linked
    to multiple restaurants over time.
  * Public-signal enrichment (BBB rating, D&B credit, Yelp B2B) is keyed
    by vendor identity, not by which restaurant happened to discover them.

So `Vendor` is the canonical row and `VendorRestaurantLink` carries the
per-restaurant overlay (preferred contact email, internal notes, trust
score, etc.). The legacy `Distributor` table stays for the existing weekly
flow until that's migrated under contracts in Phase 3+.
"""
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON
from sqlmodel import Column, Field, SQLModel


class Vendor(SQLModel, table=True):
    """Canonical vendor identity. Not scoped to a restaurant."""

    __tablename__ = "vendors"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Display name and a normalized slug for dedupe (lowercased, alnum-only).
    name: str
    name_slug: str = Field(index=True)

    # Identity hints used for dedupe across restaurants. We don't strictly
    # require any of them (a manually-added small wholesaler may have none),
    # but if two vendors share a domain or a Place ID we collapse them.
    primary_domain: Optional[str] = None
    google_place_id: Optional[str] = None
    ein: Optional[str] = None

    # Free-form list of categories this vendor supplies. Same vocabulary as
    # Distributor.supplied_categories so the existing category-matching
    # helper in api/procurement.py keeps working.
    supplied_categories: Optional[Any] = Field(default=None, sa_column=Column(JSON))

    # Coarse regional footprint hints (so we don't send a 12-month broadline
    # RFP to a vendor that only delivers in the next state over).
    service_region: Optional[str] = None   # free-form: "Bay Area", "NJ + NY", "national"
    headquarters_city: Optional[str] = None
    headquarters_state: Optional[str] = None

    # Public-signal enrichment. ALWAYS shown with a "3rd-party only —
    # verify" badge in the UI; never blended silently into the first-party
    # trust score derived from receipts.
    #
    # Shape:
    #   {
    #     "bbb": {"rating": "A+", "url": "..."},
    #     "dnb": {"credit_score": 78, "source_date": "2025-11-12"},
    #     "yelp_b2b": {"stars": 4.2, "n_reviews": 38},
    #     "evidence": "Pulled 2026-05-13 from public APIs"
    #   }
    public_signals: Optional[Any] = Field(default=None, sa_column=Column(JSON))
    public_signals_fetched_at: Optional[datetime] = None

    # Where did this vendor get into our system?
    # DISCOVERED_PLACES | MANUAL_ENTRY | INCUMBENT_FROM_CONTRACT | AGENT_DERIVED
    source: str = Field(default="MANUAL_ENTRY")

    created_at: datetime = Field(default_factory=datetime.utcnow)


class VendorRestaurantLink(SQLModel, table=True):
    """Per-restaurant overlay on top of a canonical Vendor.

    Carries the operationally-meaningful fields that vary per customer:
    which email do we contact them on, what's our internal nickname for
    them, are they currently the incumbent for any category, etc.
    """

    __tablename__ = "vendor_restaurant_links"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    vendor_id: uuid.UUID = Field(foreign_key="vendors.id", index=True)
    restaurant_profile_id: uuid.UUID = Field(
        foreign_key="restaurant_profiles.id", index=True
    )

    # The address we contact this vendor on for THIS restaurant. For demo
    # use it's a plus-tagged routing address built by places_discovery,
    # but real deployments will store the rep's actual mailbox here.
    contact_email: Optional[str] = None
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None

    # Manager's nickname / internal note ("Our Sysco rep, slow to reply").
    internal_alias: Optional[str] = None
    internal_notes: Optional[str] = None

    # Manual entries need a verification step before the lifecycle agent
    # will RFP them — otherwise a competitor could be added with a fake
    # email and used to manipulate the comparison. Verification options:
    #   PENDING_DOMAIN_CHECK (we sent a confirmation email; not yet acked)
    #   VERIFIED             (domain or operator-confirmed)
    #   AUTO_TRUSTED         (came from Places discovery — has place_id)
    verification_status: str = Field(default="AUTO_TRUSTED")

    is_active_incumbent: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class VendorTrustScore(SQLModel, table=True):
    """First-party trust score derived from this restaurant's history with
    a vendor. Rolling counters updated whenever a PurchaseReceipt is
    ingested.

    Separate from `public_signals` on Vendor because:
      * public_signals are global and 3rd-party (badge "verify")
      * trust score is per-restaurant + first-party (badge "your data")

    The comparison dashboard renders them side-by-side, never merged.
    """

    __tablename__ = "vendor_trust_scores"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    vendor_id: uuid.UUID = Field(foreign_key="vendors.id", index=True)
    restaurant_profile_id: uuid.UUID = Field(
        foreign_key="restaurant_profiles.id", index=True
    )

    # Rolling counts, updated on each receipt.
    deliveries_total: int = Field(default=0)
    deliveries_on_time: int = Field(default=0)
    deliveries_short: int = Field(default=0)      # shipped less than PO'd
    deliveries_over_charged: int = Field(default=0)  # invoice > PO total > 2%

    # Cached convenience numbers (0..1). Recomputed when counters change.
    on_time_rate: Optional[float] = None
    fulfillment_rate: Optional[float] = None
    price_accuracy_rate: Optional[float] = None

    # Composite 0..100. Phase 4 wires this into the negotiation prompt as
    # a positive lever ("you've delivered on time 47/48 weeks, that's why
    # we'd love to keep this with you").
    trust_score: Optional[float] = None

    last_updated_at: datetime = Field(default_factory=datetime.utcnow)
