"""contract renewal_cycle_started_at

Adds Phase 3 lifecycle idempotency column on contracts.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-13 23:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "contracts",
        sa.Column("renewal_cycle_started_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("contracts", "renewal_cycle_started_at")
