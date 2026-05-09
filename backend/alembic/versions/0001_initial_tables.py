"""initial_tables

Revision ID: 0001
Revises:
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union
import uuid
import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "restaurant_profiles",
        sa.Column("id", sa.UUID(), primary_key=True, default=uuid.uuid4),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("zip_code", sa.String(), nullable=False),
        sa.Column("city", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "menus",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("restaurant_profile_id", sa.UUID(), sa.ForeignKey("restaurant_profiles.id"), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("parsed_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "dishes",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("menu_id", sa.UUID(), sa.ForeignKey("menus.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("base_price", sa.Float(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, default=True),
    )

    op.create_table(
        "ingredients",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("culinary_unit", sa.String(), nullable=True),
        sa.Column("shelf_life_days", sa.Integer(), nullable=True),
        sa.Column("usda_fdc_id", sa.String(), nullable=True),
    )

    op.create_table(
        "recipes",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("dish_id", sa.UUID(), sa.ForeignKey("dishes.id"), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=True),
    )

    op.create_table(
        "recipe_ingredients",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("recipe_id", sa.UUID(), sa.ForeignKey("recipes.id"), nullable=False),
        sa.Column("ingredient_id", sa.UUID(), sa.ForeignKey("ingredients.id"), nullable=False),
        sa.Column("quantity_required", sa.Float(), nullable=True),
    )

    op.create_table(
        "distributors",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("restaurant_profile_id", sa.UUID(), sa.ForeignKey("restaurant_profiles.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("google_place_id", sa.String(), nullable=True),
        sa.Column("demo_routing_email", sa.String(), nullable=True),
        sa.Column("supplied_categories", sa.JSON(), nullable=True),
    )

    op.create_table(
        "procurement_cycles",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("restaurant_profile_id", sa.UUID(), sa.ForeignKey("restaurant_profiles.id"), nullable=False),
        sa.Column("status", sa.String(), nullable=False, default="COLLECTING_QUOTES"),
        sa.Column("order_type", sa.String(), nullable=False, default="WEEKLY"),
        sa.Column("week_start_date", sa.Date(), nullable=True),
        sa.Column("preferred_delivery_window", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "cycle_dish_forecast",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("procurement_cycle_id", sa.UUID(), sa.ForeignKey("procurement_cycles.id"), nullable=False),
        sa.Column("dish_id", sa.UUID(), sa.ForeignKey("dishes.id"), nullable=False),
        sa.Column("forecasted_quantity", sa.Integer(), nullable=False, default=0),
    )

    op.create_table(
        "cycle_ingredients_needed",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("procurement_cycle_id", sa.UUID(), sa.ForeignKey("procurement_cycles.id"), nullable=False),
        sa.Column("ingredient_id", sa.UUID(), sa.ForeignKey("ingredients.id"), nullable=False),
        sa.Column("culinary_qty_needed", sa.Float(), nullable=False, default=0.0),
        sa.Column("purchasing_qty_needed", sa.Float(), nullable=False, default=0.0),
    )

    op.create_table(
        "distributor_quotes",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("procurement_cycle_id", sa.UUID(), sa.ForeignKey("procurement_cycles.id"), nullable=False),
        sa.Column("distributor_id", sa.UUID(), sa.ForeignKey("distributors.id"), nullable=False),
        sa.Column("quote_status", sa.String(), nullable=False, default="PENDING"),
        sa.Column("total_quoted_price", sa.Float(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("recommendation_text", sa.Text(), nullable=True),
    )

    op.create_table(
        "distributor_quote_items",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("distributor_quote_id", sa.UUID(), sa.ForeignKey("distributor_quotes.id"), nullable=False),
        sa.Column("ingredient_id", sa.UUID(), sa.ForeignKey("ingredients.id"), nullable=False),
        sa.Column("quoted_price_per_unit", sa.Float(), nullable=True),
    )

    op.create_table(
        "notifications",
        sa.Column("id", sa.UUID(), primary_key=True, default=uuid.uuid4),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("is_read", sa.Boolean(), nullable=False, default=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_table("distributor_quote_items")
    op.drop_table("distributor_quotes")
    op.drop_table("cycle_ingredients_needed")
    op.drop_table("cycle_dish_forecast")
    op.drop_table("procurement_cycles")
    op.drop_table("distributors")
    op.drop_table("recipe_ingredients")
    op.drop_table("recipes")
    op.drop_table("ingredients")
    op.drop_table("dishes")
    op.drop_table("menus")
    op.drop_table("restaurant_profiles")
