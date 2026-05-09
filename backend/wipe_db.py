from database import engine
from sqlalchemy import text
from sqlmodel import Session

with Session(engine) as session:
    session.execute(text("TRUNCATE TABLE dishes, recipes, recipe_ingredients, menus, ingredients RESTART IDENTITY CASCADE;"))
    session.commit()
    print("🚀 Database wiped clean.")
