"""add pack-size override + persisted pack plan columns

Adds:
  - ingredients.pack_qty_override / pack_unit_override / pack_label_override
    (per-restaurant override of the inferred pack size)
  - cycle_ingredients_needed.pack_count / pack_unit / pack_label /
    pack_total_qty / pack_source
    (pack-rounded purchase plan persisted at cycle creation time)

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-09 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Per-restaurant pack-size override on the ingredient ──────────────────
    op.add_column(
        "ingredients",
        sa.Column("pack_qty_override", sa.Float(), nullable=True),
    )
    op.add_column(
        "ingredients",
        sa.Column("pack_unit_override", sa.String(), nullable=True),
    )
    op.add_column(
        "ingredients",
        sa.Column("pack_label_override", sa.String(), nullable=True),
    )

    # ── Persisted pack-rounded purchase plan on cycle_ingredients_needed ─────
    op.add_column(
        "cycle_ingredients_needed",
        sa.Column("pack_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "cycle_ingredients_needed",
        sa.Column("pack_unit", sa.String(), nullable=True),
    )
    op.add_column(
        "cycle_ingredients_needed",
        sa.Column("pack_label", sa.String(), nullable=True),
    )
    op.add_column(
        "cycle_ingredients_needed",
        sa.Column("pack_total_qty", sa.Float(), nullable=True),
    )
    op.add_column(
        "cycle_ingredients_needed",
        sa.Column("pack_source", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cycle_ingredients_needed", "pack_source")
    op.drop_column("cycle_ingredients_needed", "pack_total_qty")
    op.drop_column("cycle_ingredients_needed", "pack_label")
    op.drop_column("cycle_ingredients_needed", "pack_unit")
    op.drop_column("cycle_ingredients_needed", "pack_count")

    op.drop_column("ingredients", "pack_label_override")
    op.drop_column("ingredients", "pack_unit_override")
    op.drop_column("ingredients", "pack_qty_override")
