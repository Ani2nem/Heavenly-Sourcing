"""contract pivot

Re-roots the product around long-term contracts instead of weekly spot
quotes. Adds:

  * contracts, contract_line_items, contract_documents
  * vendors (canonical, not restaurant-scoped),
    vendor_restaurant_links, vendor_trust_scores
  * negotiations, negotiation_rounds (used in Phase 3)
  * manager_alerts (multi-channel notifications)

Profile changes:
  * restaurant_profiles.zip_code / city / state become NULLABLE (location
    is no longer required — contracts replace it as the primary spine)
  * restaurant_profiles.phone_number (Optional, for SMS alerts in Phase 6)
  * restaurant_profiles.onboarding_state (state machine for the wizard:
    NEEDS_PROFILE → NEEDS_CONTRACTS → NEEDS_MENU → COMPLETED)

Procurement changes:
  * purchase_receipts.acquisition_channel — keeps emergency-buy spend
    from polluting contract analytics.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-13 22:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Profile changes ────────────────────────────────────────────────
    op.alter_column(
        "restaurant_profiles", "zip_code", existing_type=sa.String(), nullable=True
    )
    op.alter_column(
        "restaurant_profiles", "city", existing_type=sa.String(), nullable=True
    )
    op.alter_column(
        "restaurant_profiles", "state", existing_type=sa.String(), nullable=True
    )
    op.add_column(
        "restaurant_profiles",
        sa.Column("phone_number", sa.String(), nullable=True),
    )
    op.add_column(
        "restaurant_profiles",
        sa.Column(
            "onboarding_state",
            sa.String(),
            nullable=False,
            server_default="NEEDS_CONTRACTS",
        ),
    )

    # ── PurchaseReceipt analytics tag ──────────────────────────────────
    op.add_column(
        "purchase_receipts",
        sa.Column(
            "acquisition_channel",
            sa.String(),
            nullable=False,
            server_default="WEEKLY_RFP",
        ),
    )

    # ── Canonical vendors table ───────────────────────────────────────
    # NOTE: columns with index=True automatically create an index named
    # `ix_<table>_<col>` — never also call op.create_index for the same
    # column or migration fails with a duplicate-relation error.
    op.create_table(
        "vendors",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("name_slug", sa.String(), nullable=False, index=True),
        sa.Column("primary_domain", sa.String(), nullable=True),
        sa.Column("google_place_id", sa.String(), nullable=True),
        sa.Column("ein", sa.String(), nullable=True),
        sa.Column("supplied_categories", sa.JSON(), nullable=True),
        sa.Column("service_region", sa.String(), nullable=True),
        sa.Column("headquarters_city", sa.String(), nullable=True),
        sa.Column("headquarters_state", sa.String(), nullable=True),
        sa.Column("public_signals", sa.JSON(), nullable=True),
        sa.Column("public_signals_fetched_at", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="MANUAL_ENTRY"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "vendor_restaurant_links",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("vendor_id", sa.UUID(), sa.ForeignKey("vendors.id"), nullable=False),
        sa.Column(
            "restaurant_profile_id", sa.UUID(),
            sa.ForeignKey("restaurant_profiles.id"), nullable=False,
        ),
        sa.Column("contact_email", sa.String(), nullable=True),
        sa.Column("contact_name", sa.String(), nullable=True),
        sa.Column("contact_phone", sa.String(), nullable=True),
        sa.Column("internal_alias", sa.String(), nullable=True),
        sa.Column("internal_notes", sa.Text(), nullable=True),
        sa.Column(
            "verification_status", sa.String(), nullable=False,
            server_default="AUTO_TRUSTED",
        ),
        sa.Column(
            "is_active_incumbent", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_vendor_restaurant_links_vendor_id",
        "vendor_restaurant_links", ["vendor_id"],
    )
    op.create_index(
        "ix_vendor_restaurant_links_restaurant_profile_id",
        "vendor_restaurant_links", ["restaurant_profile_id"],
    )

    op.create_table(
        "vendor_trust_scores",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("vendor_id", sa.UUID(), sa.ForeignKey("vendors.id"), nullable=False),
        sa.Column(
            "restaurant_profile_id", sa.UUID(),
            sa.ForeignKey("restaurant_profiles.id"), nullable=False,
        ),
        sa.Column("deliveries_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deliveries_on_time", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deliveries_short", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "deliveries_over_charged", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("on_time_rate", sa.Float(), nullable=True),
        sa.Column("fulfillment_rate", sa.Float(), nullable=True),
        sa.Column("price_accuracy_rate", sa.Float(), nullable=True),
        sa.Column("trust_score", sa.Float(), nullable=True),
        sa.Column("last_updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_vendor_trust_scores_vendor_id",
        "vendor_trust_scores", ["vendor_id"],
    )

    # ── Contracts ─────────────────────────────────────────────────────
    op.create_table(
        "contracts",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "restaurant_profile_id", sa.UUID(),
            sa.ForeignKey("restaurant_profiles.id"), nullable=False,
        ),
        sa.Column(
            "vendor_id", sa.UUID(), sa.ForeignKey("vendors.id"), nullable=True
        ),
        sa.Column("nickname", sa.String(), nullable=False),
        sa.Column("primary_category", sa.String(), nullable=True),
        sa.Column("category_coverage", sa.JSON(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("renewal_notice_days", sa.Integer(), nullable=False, server_default="60"),
        sa.Column(
            "pricing_structure", sa.String(), nullable=False, server_default="FIXED"
        ),
        sa.Column("status", sa.String(), nullable=False, server_default="DRAFT"),
        sa.Column("source", sa.String(), nullable=False, server_default="MANUAL_ENTRY"),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("raw_filename", sa.String(), nullable=True),
        sa.Column("extracted_terms", sa.JSON(), nullable=True),
        sa.Column(
            "manager_verified", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_contracts_restaurant_profile_id",
        "contracts", ["restaurant_profile_id"],
    )
    op.create_index("ix_contracts_vendor_id", "contracts", ["vendor_id"])
    op.create_index("ix_contracts_status", "contracts", ["status"])

    op.create_table(
        "contract_line_items",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "contract_id", sa.UUID(), sa.ForeignKey("contracts.id"), nullable=False
        ),
        sa.Column("sku_name", sa.String(), nullable=False),
        sa.Column(
            "ingredient_id", sa.UUID(),
            sa.ForeignKey("ingredients.id"), nullable=True,
        ),
        sa.Column("pack_description", sa.String(), nullable=True),
        sa.Column("unit_of_measure", sa.String(), nullable=True),
        sa.Column("fixed_price", sa.Float(), nullable=True),
        sa.Column("price_formula", sa.String(), nullable=True),
        sa.Column("min_volume", sa.Float(), nullable=True),
        sa.Column("min_volume_period", sa.String(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_contract_line_items_contract_id",
        "contract_line_items", ["contract_id"],
    )
    op.create_index(
        "ix_contract_line_items_ingredient_id",
        "contract_line_items", ["ingredient_id"],
    )

    op.create_table(
        "contract_documents",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "contract_id", sa.UUID(), sa.ForeignKey("contracts.id"), nullable=False
        ),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_contract_documents_contract_id",
        "contract_documents", ["contract_id"],
    )

    # ── Negotiations (Phase 3 wires these in) ─────────────────────────
    op.create_table(
        "negotiations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "contract_id", sa.UUID(), sa.ForeignKey("contracts.id"), nullable=False
        ),
        sa.Column(
            "vendor_id", sa.UUID(), sa.ForeignKey("vendors.id"), nullable=False
        ),
        sa.Column("intent", sa.String(), nullable=False, server_default="NEW_CONTRACT"),
        sa.Column("status", sa.String(), nullable=False, server_default="OPEN"),
        sa.Column("max_rounds", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("rounds_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("final_terms_snapshot", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_negotiations_contract_id", "negotiations", ["contract_id"])
    op.create_index("ix_negotiations_vendor_id", "negotiations", ["vendor_id"])
    op.create_index("ix_negotiations_status", "negotiations", ["status"])

    op.create_table(
        "negotiation_rounds",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "negotiation_id", sa.UUID(),
            sa.ForeignKey("negotiations.id"), nullable=False,
        ),
        sa.Column("round_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("direction", sa.String(), nullable=False, server_default="OUTBOUND"),
        sa.Column("status", sa.String(), nullable=False, server_default="DRAFT"),
        sa.Column("subject", sa.String(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("offer_snapshot", sa.JSON(), nullable=True),
        sa.Column("manager_approved_to_send", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_negotiation_rounds_negotiation_id",
        "negotiation_rounds", ["negotiation_id"],
    )

    # ── ManagerAlert (multi-channel actionable notifications) ─────────
    op.create_table(
        "manager_alerts",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "restaurant_profile_id", sa.UUID(),
            sa.ForeignKey("restaurant_profiles.id"), nullable=False,
        ),
        # index=True auto-creates ix_manager_alerts_grouping_key
        sa.Column("grouping_key", sa.String(), nullable=True, index=True),
        sa.Column("severity", sa.String(), nullable=False, server_default="INFO"),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("action_url", sa.String(), nullable=True),
        sa.Column("action_label", sa.String(), nullable=True),
        sa.Column(
            "delivered_dashboard", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("delivered_email_at", sa.DateTime(), nullable=True),
        sa.Column("delivered_sms_at", sa.DateTime(), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_manager_alerts_restaurant_profile_id",
        "manager_alerts", ["restaurant_profile_id"],
    )
    # ix_manager_alerts_grouping_key already created by the index=True flag
    # on the grouping_key column above.


def downgrade() -> None:
    # ix_manager_alerts_grouping_key is owned by the column-level index=True
    # flag, so it's dropped automatically when we drop the table.
    op.drop_index(
        "ix_manager_alerts_restaurant_profile_id", table_name="manager_alerts"
    )
    op.drop_table("manager_alerts")

    op.drop_index(
        "ix_negotiation_rounds_negotiation_id", table_name="negotiation_rounds"
    )
    op.drop_table("negotiation_rounds")

    op.drop_index("ix_negotiations_status", table_name="negotiations")
    op.drop_index("ix_negotiations_vendor_id", table_name="negotiations")
    op.drop_index("ix_negotiations_contract_id", table_name="negotiations")
    op.drop_table("negotiations")

    op.drop_index(
        "ix_contract_documents_contract_id", table_name="contract_documents"
    )
    op.drop_table("contract_documents")

    op.drop_index(
        "ix_contract_line_items_ingredient_id", table_name="contract_line_items"
    )
    op.drop_index(
        "ix_contract_line_items_contract_id", table_name="contract_line_items"
    )
    op.drop_table("contract_line_items")

    op.drop_index("ix_contracts_status", table_name="contracts")
    op.drop_index("ix_contracts_vendor_id", table_name="contracts")
    op.drop_index(
        "ix_contracts_restaurant_profile_id", table_name="contracts"
    )
    op.drop_table("contracts")

    op.drop_index(
        "ix_vendor_trust_scores_vendor_id", table_name="vendor_trust_scores"
    )
    op.drop_table("vendor_trust_scores")

    op.drop_index(
        "ix_vendor_restaurant_links_restaurant_profile_id",
        table_name="vendor_restaurant_links",
    )
    op.drop_index(
        "ix_vendor_restaurant_links_vendor_id",
        table_name="vendor_restaurant_links",
    )
    op.drop_table("vendor_restaurant_links")

    # ix_vendors_name_slug is owned by the column-level index=True flag,
    # so it's dropped automatically when we drop the table.
    op.drop_table("vendors")

    op.drop_column("purchase_receipts", "acquisition_channel")

    op.drop_column("restaurant_profiles", "onboarding_state")
    op.drop_column("restaurant_profiles", "phone_number")
    op.alter_column(
        "restaurant_profiles", "state", existing_type=sa.String(), nullable=False
    )
    op.alter_column(
        "restaurant_profiles", "city", existing_type=sa.String(), nullable=False
    )
    op.alter_column(
        "restaurant_profiles", "zip_code", existing_type=sa.String(), nullable=False
    )
