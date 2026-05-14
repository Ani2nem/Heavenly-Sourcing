"""phases 4-6 profile sms opt-in + procurement contract link

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-13 24:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "restaurant_profiles",
        sa.Column(
            "sms_alerts_opt_in",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    op.add_column(
        "procurement_cycles",
        sa.Column("contract_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_procurement_cycles_contract_id",
        "procurement_cycles",
        "contracts",
        ["contract_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_procurement_cycles_contract_id",
        "procurement_cycles",
        type_="foreignkey",
    )
    op.drop_column("procurement_cycles", "contract_id")
    op.drop_column("restaurant_profiles", "sms_alerts_opt_in")
