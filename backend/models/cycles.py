import uuid
from datetime import datetime, date
from typing import Optional
from sqlmodel import SQLModel, Field


class ProcurementCycle(SQLModel, table=True):
    __tablename__ = "procurement_cycles"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    restaurant_profile_id: uuid.UUID = Field(foreign_key="restaurant_profiles.id")
    status: str = Field(default="COLLECTING_QUOTES")
    order_type: str = Field(default="WEEKLY")
    week_start_date: Optional[date] = None
    preferred_delivery_window: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CycleDishForecast(SQLModel, table=True):
    __tablename__ = "cycle_dish_forecast"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    procurement_cycle_id: uuid.UUID = Field(foreign_key="procurement_cycles.id")
    dish_id: uuid.UUID = Field(foreign_key="dishes.id")
    forecasted_quantity: int = Field(default=0)


class CycleIngredientsNeeded(SQLModel, table=True):
    __tablename__ = "cycle_ingredients_needed"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    procurement_cycle_id: uuid.UUID = Field(foreign_key="procurement_cycles.id")
    ingredient_id: uuid.UUID = Field(foreign_key="ingredients.id")
    culinary_qty_needed: float = Field(default=0.0)
    purchasing_qty_needed: float = Field(default=0.0)
