from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from database import create_db_and_tables
from api import admin, profile, menu, procurement, notifications, ingredients


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    # Start IMAP daemon scheduler
    from services.email_daemon import start_imap_scheduler, stop_imap_scheduler
    start_imap_scheduler()
    yield
    stop_imap_scheduler()


app = FastAPI(title="HeavenlySourcing API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(profile.router, prefix="/api")
app.include_router(menu.router, prefix="/api")
app.include_router(procurement.router, prefix="/api")
app.include_router(notifications.router, prefix="/api")
app.include_router(ingredients.router, prefix="/api")
app.include_router(admin.router, prefix="/api")


@app.get("/health")
def health():
    return {"status": "ok"}
