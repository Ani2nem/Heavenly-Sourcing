import base64
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from database import get_session
from models import Dish, Ingredient, Menu, Recipe, RecipeIngredient, RestaurantProfile

router = APIRouter(tags=["menu"])

# ─── In-memory job store (demo-sufficient for single-process deployment) ──────
# Structure: job_id -> {status, progress, total_pages, result, error}
_jobs: Dict[str, Dict[str, Any]] = {}

PDF_BATCH_SIZE = 2   # pages per GPT-4o vision call (image-only PDF fallback)
MAX_PDF_PAGES = 50   # hard cap


# ─── Pydantic models ──────────────────────────────────────────────────────────

class MenuUploadRequest(BaseModel):
    base64_content: str
    mime_type: str = "image/jpeg"


# ─── PDF helpers ──────────────────────────────────────────────────────────────

def _try_extract_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    """
    Try to pull selectable text from the PDF using PyMuPDF.
    Returns the full text (with page headers) if the PDF has meaningful text content,
    or None if it appears to be an image-only / scanned document.
    """
    import fitz  # PyMuPDF

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages: list[str] = []
    total_chars = 0
    for i in range(len(doc)):
        text = doc[i].get_text().strip()
        total_chars += len(text)
        pages.append(f"=== PAGE {i + 1} ===\n{text}")
    doc.close()

    # Fewer than 100 chars across the whole document → image-only, fall back to vision
    if total_chars < 100:
        return None
    return "\n\n".join(pages)


def _pdf_to_page_images(pdf_bytes: bytes) -> list[bytes]:
    """Render each page to PNG (~108 DPI) — used only for image-only PDFs."""
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page_count = min(len(doc), MAX_PDF_PAGES)
    mat = fitz.Matrix(1.5, 1.5)   # 72 DPI × 1.5 ≈ 108 DPI; ~900×1200 px for letter size
    images = []
    for i in range(page_count):
        pix = doc[i].get_pixmap(matrix=mat)
        images.append(pix.tobytes("png"))
    doc.close()
    return images


# ─── DB persistence ───────────────────────────────────────────────────────────

def _save_dishes_to_db(dishes: list, confidence_score: float, profile_id: str) -> dict:
    """
    Atomically persist all parsed dishes and their ingredients.
    Maps the compact prompt keys back to DB column names:
      "q"   → quantity_required (canonical: lb, fl oz, or each)
      "cat" → category
      "sl"  → shelf_life_days
    """
    from database import engine
    from sqlmodel import Session as _Session
    from agents.ingredient_units import canonicalize_ingredient_row

    with _Session(engine) as session:
        try:
            pid = uuid.UUID(profile_id)
            menu = Menu(
                restaurant_profile_id=pid,
                raw_text=str({"dishes": dishes, "confidence_score": confidence_score}),
                parsed_at=datetime.utcnow(),
            )
            session.add(menu)
            session.flush()

            result_dishes = []
            for dish_data in dishes:
                dish_name = (dish_data.get("name") or "").strip()
                if not dish_name:
                    continue

                dish = Dish(
                    menu_id=menu.id,
                    name=dish_name,
                    base_price=dish_data.get("base_price"),
                )
                session.add(dish)
                session.flush()

                recipe = Recipe(dish_id=dish.id, confidence_score=confidence_score)
                session.add(recipe)
                session.flush()

                dish_ingredients = []
                for ing_data in dish_data.get("ingredients", []):
                    norm = canonicalize_ingredient_row(ing_data)
                    if not norm:
                        continue
                    ing_name = (norm.get("name") or "").strip()
                    if not ing_name:
                        continue
                    cul_unit = norm.get("unit")

                    # Reuse existing ingredient row (deduplicates across dishes)
                    existing = session.exec(
                        select(Ingredient).where(Ingredient.name == ing_name)
                    ).first()
                    if not existing:
                        existing = Ingredient(
                            name=ing_name,
                            culinary_unit=cul_unit,
                            category=norm.get("cat"),
                            shelf_life_days=norm.get("sl"),
                        )
                        session.add(existing)
                        session.flush()
                    else:
                        if existing.culinary_unit in (None, "", "portion"):
                            existing.culinary_unit = cul_unit
                        # Keep category / shelf life if missing on canonical row
                        if existing.category is None and norm.get("cat"):
                            existing.category = norm.get("cat")
                        if existing.shelf_life_days is None and norm.get("sl") is not None:
                            existing.shelf_life_days = norm.get("sl")

                    session.add(RecipeIngredient(
                        recipe_id=recipe.id,
                        ingredient_id=existing.id,
                        quantity_required=norm.get("q"),
                    ))
                    dish_ingredients.append({
                        "name": ing_name,
                        "quantity": norm.get("q"),
                        "unit": cul_unit,
                    })

                result_dishes.append({
                    "dish_id": str(dish.id),
                    "name": dish_name,
                    "base_price": dish_data.get("base_price"),
                    "ingredients": dish_ingredients,
                })

            session.commit()
            return {
                "menu_id": str(menu.id),
                "recipes": result_dishes,
                "confidence_score": confidence_score,
            }
        except Exception:
            session.rollback()
            raise


# ─── Background job ───────────────────────────────────────────────────────────

def _process_pdf_job(job_id: str, pdf_bytes: bytes, profile_id: str) -> None:
    """
    Background task lifecycle:
      1. Try text extraction (fast, accurate for text PDFs like this menu).
         → Single GPT-4o call with full menu text; no vision tokens.
      2. If image-only PDF, fall back to page rendering + GPT-4o vision batches.
      3. Deduplicate dishes by normalised name.
      4. Save to DB, update job store.
    """
    from agents.menu_parser import parse_menu_text, parse_menu_pages

    try:
        _jobs[job_id]["status"] = "processing"
        _jobs[job_id]["progress"] = "Extracting menu content…"

        # ── Try text extraction first ──────────────────────────────────────────
        pdf_text = _try_extract_pdf_text(pdf_bytes)

        if pdf_text:
            # Count pages just for the status report
            import fitz
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            total_pages = len(doc)
            doc.close()
            _jobs[job_id]["total_pages"] = total_pages
            _jobs[job_id]["progress"] = (
                f"Text extracted from {total_pages} pages — parsing with GPT-4o…"
            )

            result = parse_menu_text(pdf_text)
            all_dishes = result.get("dishes", [])
            confidence_score = result.get("confidence_score", 90)

        else:
            # ── Image-only PDF fallback: render + vision ───────────────────────
            _jobs[job_id]["progress"] = "Converting PDF pages to images…"
            page_images = _pdf_to_page_images(pdf_bytes)
            total_pages = len(page_images)
            _jobs[job_id]["total_pages"] = total_pages

            all_dishes: list = []
            confidence_sum = 0.0
            batch_count = 0

            for i in range(0, total_pages, PDF_BATCH_SIZE):
                batch = page_images[i : i + PDF_BATCH_SIZE]
                end_page = min(i + PDF_BATCH_SIZE, total_pages)
                _jobs[job_id]["progress"] = (
                    f"Parsing pages {i + 1}–{end_page} of {total_pages} (vision)…"
                )
                batch_result = parse_menu_pages(batch, page_offset=i)
                all_dishes.extend(batch_result.get("dishes", []))
                confidence_sum += batch_result.get("confidence_score", 0)
                batch_count += 1

            confidence_score = round(confidence_sum / batch_count, 1) if batch_count else 0

        # ── Deduplicate by lowercase name ──────────────────────────────────────
        seen: set = set()
        deduped: list = []
        for d in all_dishes:
            key = (d.get("name") or "").strip().lower()
            if key and key not in seen:
                seen.add(key)
                deduped.append(d)

        _jobs[job_id]["progress"] = f"Saving {len(deduped)} dishes to database…"
        db_result = _save_dishes_to_db(deduped, confidence_score, profile_id)

        _jobs[job_id].update({
            "status": "completed",
            "progress": (
                f"Done — {len(deduped)} unique dishes extracted from {total_pages} pages."
            ),
            "result": db_result,
        })

    except Exception as exc:
        import traceback
        traceback.print_exc()
        _jobs[job_id].update({
            "status": "failed",
            "error": str(exc),
            "progress": f"Failed: {exc}",
        })


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.post("/menu/upload")
def upload_menu(
    payload: MenuUploadRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    profile = session.exec(select(RestaurantProfile)).first()
    if not profile:
        raise HTTPException(status_code=400, detail="Create a profile first")

    if payload.mime_type == "application/pdf":
        # Async path: returns immediately; client polls /api/menu/upload/status/{job_id}
        try:
            pdf_bytes = base64.b64decode(payload.base64_content)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid base64 content")

        job_id = str(uuid.uuid4())
        _jobs[job_id] = {
            "status": "queued",
            "progress": "Queued — starting extraction…",
            "total_pages": None,
            "result": None,
            "error": None,
        }
        background_tasks.add_task(_process_pdf_job, job_id, pdf_bytes, str(profile.id))
        return {
            "job_id": job_id,
            "status": "processing",
            "message": "PDF is processing in the background. Poll /api/menu/upload/status/{job_id}.",
        }

    # ── Sync path for single images (fast) ────────────────────────────────────
    from agents.menu_parser import parse_menu
    from agents.ingredient_units import canonicalize_ingredient_row

    try:
        parsed = parse_menu(payload.base64_content, payload.mime_type)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GPT parsing failed: {exc}")

    try:
        menu = Menu(
            restaurant_profile_id=profile.id,
            raw_text=str(parsed),
            parsed_at=datetime.utcnow(),
        )
        session.add(menu)
        session.flush()

        result_dishes = []
        for dish_data in parsed.get("dishes", []):
            dish_name = (dish_data.get("name") or "").strip()
            if not dish_name:
                continue

            dish = Dish(
                menu_id=menu.id,
                name=dish_name,
                base_price=dish_data.get("base_price"),
            )
            session.add(dish)
            session.flush()

            recipe = Recipe(dish_id=dish.id, confidence_score=parsed.get("confidence_score"))
            session.add(recipe)
            session.flush()

            dish_ingredients = []
            for ing_data in dish_data.get("ingredients", []):
                norm = canonicalize_ingredient_row(ing_data)
                if not norm:
                    continue
                ing_name = (norm.get("name") or "").strip()
                if not ing_name:
                    continue
                cul_unit = norm.get("unit")
                existing = session.exec(
                    select(Ingredient).where(Ingredient.name == ing_name)
                ).first()
                if not existing:
                    existing = Ingredient(
                        name=ing_name,
                        culinary_unit=cul_unit,
                        category=norm.get("cat"),
                        shelf_life_days=norm.get("sl"),
                    )
                    session.add(existing)
                    session.flush()
                else:
                    if existing.culinary_unit in (None, "", "portion"):
                        existing.culinary_unit = cul_unit
                    if existing.category is None and norm.get("cat"):
                        existing.category = norm.get("cat")
                    if existing.shelf_life_days is None and norm.get("sl") is not None:
                        existing.shelf_life_days = norm.get("sl")

                session.add(RecipeIngredient(
                    recipe_id=recipe.id,
                    ingredient_id=existing.id,
                    quantity_required=norm.get("q"),
                ))
                dish_ingredients.append({
                    "name": ing_name,
                    "quantity": norm.get("q"),
                    "unit": cul_unit,
                })

            result_dishes.append({
                "dish_id": str(dish.id),
                "name": dish_name,
                "base_price": dish_data.get("base_price"),
                "ingredients": dish_ingredients,
            })

        session.commit()
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to save parsed menu: {exc}")

    return {
        "menu_id": str(menu.id),
        "recipes": result_dishes,
        "confidence_score": parsed.get("confidence_score"),
    }


@router.get("/menu/upload/status/{job_id}")
def get_upload_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job_id,
        "status": job["status"],           # queued | processing | completed | failed
        "progress": job.get("progress", ""),
        "total_pages": job.get("total_pages"),
        "result": job.get("result"),
        "error": job.get("error"),
    }


@router.get("/menu/recipes")
def get_recipes(session: Session = Depends(get_session)):
    profile = session.exec(select(RestaurantProfile)).first()
    if not profile:
        return []

    menus = session.exec(
        select(Menu).where(Menu.restaurant_profile_id == profile.id)
    ).all()
    if not menus:
        return []

    latest_menu = sorted(
        menus, key=lambda m: m.parsed_at or datetime.min, reverse=True
    )[0]
    dishes = session.exec(
        select(Dish).where(Dish.menu_id == latest_menu.id, Dish.is_active == True)
    ).all()

    result = []
    for dish in dishes:
        recipe = session.exec(select(Recipe).where(Recipe.dish_id == dish.id)).first()
        ingredients = []
        if recipe:
            for ri in session.exec(
                select(RecipeIngredient).where(RecipeIngredient.recipe_id == recipe.id)
            ).all():
                ing = session.get(Ingredient, ri.ingredient_id)
                if ing:
                    ingredients.append({
                        "name": ing.name,
                        "quantity": ri.quantity_required,
                        "unit": ing.culinary_unit,
                        "category": ing.category,
                        "shelf_life_days": ing.shelf_life_days,
                    })
        result.append({
            "dish_id": str(dish.id),
            "name": dish.name,
            "base_price": dish.base_price,
            "ingredients": ingredients,
        })

    return result
