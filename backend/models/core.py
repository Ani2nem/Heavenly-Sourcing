import uuid
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import JSON
from sqlmodel import Column, Field, SQLModel


class RestaurantProfile(SQLModel, table=True):
    __tablename__ = "restaurant_profiles"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str

    # Location is now OPTIONAL. The product is contract-led; location is only
    # used as a fallback signal for the legacy Places-based discovery flow and
    # for the (future) "emergency buy at local Walmart" path. Onboarding no
    # longer forces the user to type a zip+city+state before they can upload
    # a contract.
    zip_code: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None

    email: str

    # Optional cellphone for the contract-decision SMS alert path (Phase 6).
    # We render UI affordances around it regardless so the manager can opt in
    # early, but the SMS sender no-ops gracefully when this is NULL.
    phone_number: Optional[str] = None

    # Opt-in for Phase 6 actionable SMS (Twilio). Requires phone_number +
    # env Twilio credentials.
    sms_alerts_opt_in: bool = Field(default=False)

    # State machine for the onboarding wizard so the frontend has a single
    # source of truth for "where should I send this user next?". Values:
    #   NEEDS_PROFILE         — no profile row exists yet
    #   NEEDS_CONTRACTS       — profile exists, no contracts uploaded or skipped
    #   NEEDS_MENU            — contracts step done (uploaded OR explicitly
    #                           skipped), menu not yet parsed
    #   COMPLETED             — menu parsed; full dashboard available
    onboarding_state: str = Field(default="NEEDS_CONTRACTS")

    created_at: datetime = Field(default_factory=datetime.utcnow)


class Menu(SQLModel, table=True):
    __tablename__ = "menus"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    restaurant_profile_id: uuid.UUID = Field(foreign_key="restaurant_profiles.id")
    raw_text: Optional[str] = None
    parsed_at: Optional[datetime] = None


class Dish(SQLModel, table=True):
    __tablename__ = "dishes"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    menu_id: uuid.UUID = Field(foreign_key="menus.id")
    name: str
    base_price: Optional[float] = None
    is_active: bool = Field(default=True)


class Ingredient(SQLModel, table=True):
    __tablename__ = "ingredients"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str
    category: Optional[str] = None
    culinary_unit: Optional[str] = None
    shelf_life_days: Optional[int] = None
    usda_fdc_id: Optional[str] = None

    # Per-restaurant pack-size override. When all three are set, the
    # procurement RFP uses these values instead of the inferred default in
    # `services/pack_inference.py`. Useful when a vendor publishes a custom
    # SKU ("we sell mozzarella in 6-lb bags, not 5") or the kitchen has a
    # standing preference.
    pack_qty_override: Optional[float] = None
    pack_unit_override: Optional[str] = None
    pack_label_override: Optional[str] = None


class Recipe(SQLModel, table=True):
    __tablename__ = "recipes"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    dish_id: uuid.UUID = Field(foreign_key="dishes.id")
    confidence_score: Optional[float] = None


class RecipeIngredient(SQLModel, table=True):
    __tablename__ = "recipe_ingredients"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    recipe_id: uuid.UUID = Field(foreign_key="recipes.id")
    ingredient_id: uuid.UUID = Field(foreign_key="ingredients.id")
    quantity_required: Optional[float] = None


class IngredientPrice(SQLModel, table=True):
    """A single USDA price observation for an ingredient (one row per report date)."""

    __tablename__ = "ingredient_prices"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    ingredient_id: uuid.UUID = Field(foreign_key="ingredients.id", index=True)
    source: str = Field(default="AMS_MARKET_NEWS")  # AMS_MARKET_NEWS | NASS | BENCHMARK
    report_slug: Optional[str] = None
    region: Optional[str] = None
    commodity_label: Optional[str] = None
    unit: str = Field(default="lb")
    price_low: Optional[float] = None
    price_high: Optional[float] = None
    price_mostly: Optional[float] = None
    as_of_date: Optional[date] = Field(default=None, index=True)
    raw_payload: Optional[Any] = Field(default=None, sa_column=Column(JSON))
    fetched_at: datetime = Field(default_factory=datetime.utcnow)
