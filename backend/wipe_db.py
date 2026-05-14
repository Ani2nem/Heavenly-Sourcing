"""Wipe all transactional data: menus, recipes, ingredients, distributors,
procurement cycles, quotes, receipts, notifications, ingredient prices.
Leaves restaurant_profiles and the schema itself intact."""
from database import engine
from sqlalchemy import text
from sqlmodel import Session

TABLES = [
    # ── Phase-2 contract pivot tables (drop children first to avoid FK pain) ──
    "manager_alerts",
    "negotiation_rounds",
    "negotiations",
    "contract_documents",
    "contract_line_items",
    "contracts",
    "vendor_trust_scores",
    "vendor_restaurant_links",
    "vendors",
    # ── Legacy weekly-RFP tables ─────────────────────────────────────────────
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
