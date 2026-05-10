"""Wipe all transactional data: menus, recipes, ingredients, distributors,
procurement cycles, quotes, receipts, notifications, ingredient prices.
Leaves restaurant_profiles and the schema itself intact."""
from database import engine
from sqlalchemy import text
from sqlmodel import Session

TABLES = [
    "purchase_receipts",
    "distributor_quote_items",
    "distributor_quotes",
    "cycle_ingredients_needed",
    "cycle_dish_forecast",
    "procurement_cycles",
    "distributors",
    "notifications",
    "ingredient_prices",
    "recipe_ingredients",
    "recipes",
    "dishes",
    "menus",
    "ingredients",
]

with Session(engine) as session:
    session.execute(text(f"TRUNCATE TABLE {', '.join(TABLES)} RESTART IDENTITY CASCADE;"))
    session.commit()
    print(f"Wiped {len(TABLES)} tables.")
