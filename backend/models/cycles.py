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
    # Optional FK — weekly spot-buy cycles executed under a long-term contract.
    contract_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="contracts.id", index=True
    )
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

    # Recipe need (e.g. 10.0 fl oz of pizza sauce per cycle).
    culinary_qty_needed: float = Field(default=0.0)

    # Pack-rounded purchase plan written by `_background_procurement` after
    # consulting `services/pack_inference.compute_purchase`. When the plan
    # is None (no pack rule matched + no override) these stay 0/None and
    # `purchasing_qty_needed` falls back to the culinary need.
    purchasing_qty_needed: float = Field(default=0.0)   # legacy; mirrors culinary need
    pack_count: Optional[int] = None                    # how many packs to order
    pack_unit: Optional[str] = None                     # the pack's unit (e.g. "fl oz" for #10 can)
    pack_label: Optional[str] = None                    # human description ("#10 can (~104 fl oz)")
    pack_total_qty: Optional[float] = None              # pack_count × pack_qty in pack_unit (e.g. 104)
    pack_source: Optional[str] = None                   # "override" | "inferred" | None
