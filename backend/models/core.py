import uuid
from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field


class RestaurantProfile(SQLModel, table=True):
    __tablename__ = "restaurant_profiles"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str
    zip_code: str
    city: str
    state: str
    email: str
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
