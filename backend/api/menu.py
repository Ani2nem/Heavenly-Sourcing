import base64
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
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
MAX_PARALLEL_VISION_BATCHES = 6   # cap concurrent vision calls so we don't trip rate limits


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


# ─── USDA pricing backfill ────────────────────────────────────────────────────

def _backfill_usda_prices_for_menu(menu_id: Optional[str]) -> Dict[str, int]:
    """Fetch BOTH USDA FDC ids AND AMS Market News prices for every ingredient
    referenced by a menu.

    FDC backfill is fast (one HTTP call per ingredient, returns instantly).
    AMS backfill can take 5-30s per ingredient on first discovery (it has to
    list ~1k AMS reports), so we run it serially after the quick FDC pass.

    Returns: {"fdc_ids": int, "price_rows": int}
    """
    result = {"fdc_ids": 0, "price_rows": 0}
    if not menu_id:
        return result
    from database import engine
    from sqlmodel import Session as _Session
    from services.ams_pricing import (
        fetch_and_store_prices_for_ingredient,
        find_mapping_for,
    )
    from services.usda_client import search_fdc_id

    try:
        mid = uuid.UUID(menu_id)
    except (ValueError, TypeError):
        return result

    with _Session(engine) as session:
        ingredient_ids = {
            ri.ingredient_id
            for ri in session.exec(
                select(RecipeIngredient).join(
                    Recipe, Recipe.id == RecipeIngredient.recipe_id
                ).join(
                    Dish, Dish.id == Recipe.dish_id
                ).where(Dish.menu_id == mid)
            ).all()
        }
        if not ingredient_ids:
            return result
        ings = session.exec(
            select(Ingredient).where(Ingredient.id.in_(ingredient_ids))
        ).all()

        # Pass 1: FDC ids (fast). Don't block AMS pass if any single one fails.
        fdc_dirty = False
        for ing in ings:
            if ing.usda_fdc_id:
                continue
            try:
                fdc_id = search_fdc_id(ing.name)
            except Exception as exc:
                print(f"[menu] FDC search failed for {ing.name}: {exc}")
                continue
            if fdc_id:
                ing.usda_fdc_id = fdc_id
                session.add(ing)
                result["fdc_ids"] += 1
                fdc_dirty = True
        if fdc_dirty:
            session.commit()

        # Pass 2: AMS price points (slow on first hit). Skip ingredients we
        # don't have a mapping for to avoid hammering AMS for things like
        # "Vanilla" or "BBQ Sauce" that AMS doesn't cover.
        for ing in ings:
            if not find_mapping_for(ing.name):
                continue
            try:
                result["price_rows"] += fetch_and_store_prices_for_ingredient(session, ing)
            except Exception as exc:
                print(f"[menu] AMS fetch failed for {ing.name}: {exc}")
    return result


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
            # ── Image-only PDF fallback: render + vision (parallel) ────────────
            _jobs[job_id]["progress"] = "Converting PDF pages to images…"
            page_images = _pdf_to_page_images(pdf_bytes)
            total_pages = len(page_images)
            _jobs[job_id]["total_pages"] = total_pages

            # Build all batches up front, then dispatch in parallel
            batch_specs: list = []
            for i in range(0, total_pages, PDF_BATCH_SIZE):
                batch_specs.append((i, page_images[i : i + PDF_BATCH_SIZE]))
            batch_count = len(batch_specs)

            _jobs[job_id]["progress"] = (
                f"Parsing {total_pages} pages across {batch_count} parallel vision calls…"
            )

            results: list = [None] * batch_count
            with ThreadPoolExecutor(
                max_workers=min(MAX_PARALLEL_VISION_BATCHES, batch_count)
            ) as pool:
                futures = {
                    pool.submit(parse_menu_pages, batch, i): idx
                    for idx, (i, batch) in enumerate(batch_specs)
                }
                for fut in futures:
                    idx = futures[fut]
                    try:
                        results[idx] = fut.result()
                    except Exception as exc:
                        print(f"[menu] vision batch {idx + 1}/{batch_count} crashed: {exc}")
                        results[idx] = {"dishes": [], "confidence_score": 0}

            all_dishes: list = []
            confidence_sum = 0.0
            for r in results:
                all_dishes.extend(r.get("dishes", []))
                confidence_sum += r.get("confidence_score", 0)

            confidence_score = round(confidence_sum / batch_count, 1) if batch_count else 0

        # ── Deduplicate by lowercase name ──────────────────────────────────────
        seen: set = set()
        deduped: list = []
        for d in all_dishes:
            key = (d.get("name") or "").strip().lower()
            if key and key not in seen:
                seen.add(key)
                deduped.append(d)

        if not deduped:
            # Parser silently swallowed every batch — surface a clear error so the UI stops
            # spinning forever and the user knows the menu didn't actually parse.
            _jobs[job_id].update({
                "status": "failed",
                "error": (
                    "Menu parsing produced 0 dishes. The model's response was likely truncated "
                    "or malformed — try a smaller PDF, a clearer scan, or split the menu into "
                    "fewer pages."
                ),
                "progress": "Failed: 0 dishes extracted from the menu.",
            })
            return

        _jobs[job_id]["progress"] = f"Saving {len(deduped)} dishes to database…"
        db_result = _save_dishes_to_db(deduped, confidence_score, profile_id)

        # Mark the job COMPLETED immediately after DB save — the user can land on the
        # procurement page right away. USDA price trends backfill happens in a daemon
        # thread; it can take 10–30s per ingredient on the AMS API and shouldn't block
        # the upload UX. Prices will trickle in and the recipes-with-prices endpoint
        # will pick them up on subsequent fetches.
        _jobs[job_id].update({
            "status": "completed",
            "progress": (
                f"Done — {len(deduped)} unique dishes extracted from {total_pages} pages. "
                "USDA price trends backfilling in background."
            ),
            "result": db_result,
        })

        def _bg_backfill_usda(menu_id: Optional[str]) -> None:
            try:
                stats = _backfill_usda_prices_for_menu(menu_id)
                print(
                    f"[menu] background USDA backfill complete: "
                    f"{stats.get('fdc_ids', 0)} FDC ids resolved, "
                    f"{stats.get('price_rows', 0)} AMS price rows stored"
                )
            except Exception as exc:
                print(f"[menu] background USDA backfill failed: {exc}")

        threading.Thread(
            target=_bg_backfill_usda,
            args=(db_result.get("menu_id"),),
            daemon=True,
        ).start()

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

    try:
        background_tasks.add_task(_backfill_usda_prices_for_menu, str(menu.id))
    except Exception as exc:
        print(f"[menu] failed to schedule USDA backfill: {exc}")

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


def _industry_estimate(ing: Ingredient) -> Optional[Dict[str, Any]]:
    """Category-based industry-estimate fallback for the recipes page.

    Returns ``None`` unless ``build_benchmarks`` produces a category-tier
    record for this ingredient — i.e. the recipe unit is mass-compatible
    AND there's a category midpoint in ``_CATEGORY_BENCHMARK_PER_LB``.
    Passing ``session=None`` forces ``build_benchmarks`` to skip the real
    AMS tier (the recipes page already gets that via
    ``summarize_ingredient_prices``); we only want the fallback here.

    The label string is the same ``~$X.XX/lb (industry est, <cat>)`` the
    RFP "Reference Benchmark" column has been rendering all along, so the
    frontend doesn't have to re-format anything.
    """
    from services.usda_client import build_benchmarks

    records = build_benchmarks(
        [{
            "name": ing.name,
            "category": ing.category,
            "unit": ing.culinary_unit,
        }],
        session=None,
    )
    if not records or records[0].get("source") != "category":
        return None
    rec = records[0]
    return {
        "value": rec.get("value"),
        "unit": rec.get("unit"),
        "label": rec.get("label"),
        "category": rec.get("category"),
        "source": "industry_est",
    }


@router.get("/menu/recipes/with-prices")
def get_recipes_with_prices(session: Session = Depends(get_session)):
    """Recipes plus a per-ingredient USDA AMS price summary, with a
    category-based industry-estimate fallback when AMS has no data.

    Response shape per ingredient:
      ``usda_price``    — real AMS data (``has_data`` true/false, series, latest, avg).
      ``usda_estimate`` — present only when ``usda_price.has_data`` is false
                          AND a category midpoint applies. Carries a
                          pre-formatted ``label`` such as
                          ``~$4.50/lb (industry est, dairy)``.
    """
    from services.ams_pricing import summarize_ingredient_prices

    profile = session.exec(select(RestaurantProfile)).first()
    if not profile:
        return []

    menus = session.exec(
        select(Menu).where(Menu.restaurant_profile_id == profile.id)
    ).all()
    if not menus:
        return []

    latest_menu = sorted(menus, key=lambda m: m.parsed_at or datetime.min, reverse=True)[0]
    dishes = session.exec(
        select(Dish).where(Dish.menu_id == latest_menu.id, Dish.is_active == True)
    ).all()

    summary_cache: Dict[str, Dict[str, Any]] = {}
    estimate_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    def _summary(ing_id) -> Dict[str, Any]:
        key = str(ing_id)
        if key not in summary_cache:
            summary_cache[key] = summarize_ingredient_prices(session, ing_id)
        return summary_cache[key]

    def _estimate(ing: Ingredient) -> Optional[Dict[str, Any]]:
        key = str(ing.id)
        if key not in estimate_cache:
            estimate_cache[key] = _industry_estimate(ing)
        return estimate_cache[key]

    result: list = []
    for dish in dishes:
        recipe = session.exec(select(Recipe).where(Recipe.dish_id == dish.id)).first()
        ingredients: list = []
        if recipe:
            for ri in session.exec(
                select(RecipeIngredient).where(RecipeIngredient.recipe_id == recipe.id)
            ).all():
                ing = session.get(Ingredient, ri.ingredient_id)
                if not ing:
                    continue
                price = _summary(ing.id)
                # Only attach the industry estimate when AMS has nothing,
                # so the frontend can pick whichever it sees.
                estimate = None if price.get("has_data") else _estimate(ing)
                ingredients.append({
                    "recipe_ingredient_id": str(ri.id),
                    "ingredient_id": str(ing.id),
                    "name": ing.name,
                    "quantity": ri.quantity_required,
                    "unit": ing.culinary_unit,
                    "category": ing.category,
                    "shelf_life_days": ing.shelf_life_days,
                    "usda_fdc_id": ing.usda_fdc_id,
                    "usda_price": price,
                    "usda_estimate": estimate,
                })
        result.append({
            "dish_id": str(dish.id),
            "name": dish.name,
            "base_price": dish.base_price,
            "ingredients": ingredients,
        })

    return result


# ─── Recipe ingredient editing ────────────────────────────────────────────────

class AddRecipeIngredientRequest(BaseModel):
    name: str
    quantity: Optional[float] = None
    unit: Optional[str] = None
    category: Optional[str] = None
    shelf_life_days: Optional[int] = None


class EditRecipeIngredientRequest(BaseModel):
    name: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    category: Optional[str] = None
    shelf_life_days: Optional[int] = None


def _schedule_usda_backfill_for_ingredient(ingredient_id: uuid.UUID) -> None:
    """Fire-and-forget USDA enrichment (FDC search + AMS price fetch) for a
    single ingredient. Best-effort only; logs and swallows on failure so the
    edit/add response stays snappy.
    """
    def _worker() -> None:
        try:
            from database import engine
            from sqlmodel import Session as _Session
            from services.usda_client import search_fdc_id
            from services.ams_pricing import (
                fetch_and_store_prices_for_ingredient, find_mapping_for,
            )

            with _Session(engine) as s:
                ing = s.get(Ingredient, ingredient_id)
                if not ing:
                    return
                if not ing.usda_fdc_id:
                    fdc_id = search_fdc_id(ing.name)
                    if fdc_id:
                        ing.usda_fdc_id = fdc_id
                        s.add(ing)
                        s.commit()
                if find_mapping_for(ing.name):
                    try:
                        n = fetch_and_store_prices_for_ingredient(s, ing)
                        if n:
                            print(f"[edit] USDA AMS backfill stored {n} rows for {ing.name}")
                    except Exception as exc:
                        print(f"[edit] USDA AMS backfill failed for {ing.name}: {exc}")
        except Exception as exc:
            print(f"[edit] USDA backfill worker crashed: {exc}")

    threading.Thread(target=_worker, daemon=True).start()


def _find_or_create_ingredient(
    session: Session,
    *,
    name: str,
    unit: Optional[str],
    category: Optional[str],
    shelf_life_days: Optional[int],
) -> tuple:
    """Return (Ingredient, was_newly_created).

    Looks up by case-insensitive trimmed name. Never mutates an existing row's
    shared metadata — that's safer for ingredients used by multiple dishes.
    Backfill of missing fields on existing ingredients is a separate concern.
    """
    needle = (name or "").strip()
    if not needle:
        raise HTTPException(status_code=422, detail="Ingredient name is required")
    # Case-insensitive lookup so "Mozzarella" and "mozzarella" don't fork
    existing = session.exec(
        select(Ingredient).where(Ingredient.name.ilike(needle))
    ).first()
    if existing:
        return existing, False
    new_ing = Ingredient(
        name=needle,
        culinary_unit=unit,
        category=category,
        shelf_life_days=shelf_life_days,
    )
    session.add(new_ing)
    session.flush()
    return new_ing, True


def _normalize_user_ingredient(
    *,
    name: str,
    quantity: Optional[float],
    unit: Optional[str],
    category: Optional[str],
    shelf_life_days: Optional[int],
) -> Dict[str, Any]:
    """Run user-typed edits through the same canonicalizer the LLM rows go
    through, so a user typing "5 ounces" lands as q=0.3125 unit='lb'.
    """
    from agents.ingredient_units import canonicalize_ingredient_row

    raw = {
        "name": name,
        "q": quantity,
        "unit": unit,
        "cat": category,
        "sl": shelf_life_days,
    }
    norm = canonicalize_ingredient_row(raw) or raw
    return {
        "name": (norm.get("name") or name).strip(),
        "q": norm.get("q", quantity),
        "unit": norm.get("unit") or unit,
        "cat": norm.get("cat") or category,
        "sl": norm.get("sl") if norm.get("sl") is not None else shelf_life_days,
    }


def _serialize_recipe_ingredient(
    session: Session, ri: RecipeIngredient
) -> Dict[str, Any]:
    from services.ams_pricing import summarize_ingredient_prices
    ing = session.get(Ingredient, ri.ingredient_id)
    if not ing:
        raise HTTPException(status_code=404, detail="Ingredient not found")
    price = summarize_ingredient_prices(session, ing.id)
    estimate = None if price.get("has_data") else _industry_estimate(ing)
    return {
        "recipe_ingredient_id": str(ri.id),
        "ingredient_id": str(ing.id),
        "name": ing.name,
        "quantity": ri.quantity_required,
        "unit": ing.culinary_unit,
        "category": ing.category,
        "shelf_life_days": ing.shelf_life_days,
        "usda_fdc_id": ing.usda_fdc_id,
        "usda_price": price,
        "usda_estimate": estimate,
    }


def _ensure_recipe_for_dish(session: Session, dish_id: uuid.UUID) -> Recipe:
    recipe = session.exec(select(Recipe).where(Recipe.dish_id == dish_id)).first()
    if recipe:
        return recipe
    # Edge case: dish created without a recipe row (very old data). Make one.
    recipe = Recipe(dish_id=dish_id)
    session.add(recipe)
    session.flush()
    return recipe


@router.post("/menu/dishes/{dish_id}/ingredients")
def add_recipe_ingredient(
    dish_id: str,
    payload: AddRecipeIngredientRequest,
    session: Session = Depends(get_session),
):
    try:
        did = uuid.UUID(dish_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid dish_id")
    dish = session.get(Dish, did)
    if not dish:
        raise HTTPException(status_code=404, detail="Dish not found")

    norm = _normalize_user_ingredient(
        name=payload.name,
        quantity=payload.quantity,
        unit=payload.unit,
        category=payload.category,
        shelf_life_days=payload.shelf_life_days,
    )

    recipe = _ensure_recipe_for_dish(session, dish.id)
    ing, was_new = _find_or_create_ingredient(
        session,
        name=norm["name"],
        unit=norm["unit"],
        category=norm["cat"],
        shelf_life_days=norm["sl"],
    )

    # Don't double-add: if this dish already has this ingredient, just bump qty
    existing_ri = session.exec(
        select(RecipeIngredient)
        .where(RecipeIngredient.recipe_id == recipe.id)
        .where(RecipeIngredient.ingredient_id == ing.id)
    ).first()
    if existing_ri:
        if norm["q"] is not None:
            existing_ri.quantity_required = float(norm["q"])
            session.add(existing_ri)
        ri = existing_ri
    else:
        ri = RecipeIngredient(
            recipe_id=recipe.id,
            ingredient_id=ing.id,
            quantity_required=float(norm["q"]) if norm["q"] is not None else None,
        )
        session.add(ri)
        session.flush()

    session.commit()
    session.refresh(ri)

    if was_new:
        _schedule_usda_backfill_for_ingredient(ing.id)

    return {
        "ok": True,
        "was_new_ingredient": was_new,
        "row": _serialize_recipe_ingredient(session, ri),
    }


@router.patch("/menu/recipe-ingredients/{ri_id}")
def edit_recipe_ingredient(
    ri_id: str,
    payload: EditRecipeIngredientRequest,
    session: Session = Depends(get_session),
):
    try:
        rid = uuid.UUID(ri_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid recipe-ingredient id")
    ri = session.get(RecipeIngredient, rid)
    if not ri:
        raise HTTPException(status_code=404, detail="Recipe-ingredient row not found")

    current_ing = session.get(Ingredient, ri.ingredient_id)
    if not current_ing:
        raise HTTPException(status_code=500, detail="Ingredient row vanished")

    # Decide whether the user is editing identity (name/unit/category) — that
    # requires swapping to a different Ingredient — or just the quantity.
    new_name = (payload.name or "").strip() if payload.name is not None else None
    identity_changed = (
        (new_name is not None and new_name.lower() != (current_ing.name or "").lower())
        or (payload.unit is not None and (payload.unit or "") != (current_ing.culinary_unit or ""))
        or (payload.category is not None and (payload.category or "") != (current_ing.category or ""))
        or (
            payload.shelf_life_days is not None
            and payload.shelf_life_days != current_ing.shelf_life_days
        )
    )

    was_new_ingredient = False
    if identity_changed:
        norm = _normalize_user_ingredient(
            name=new_name or current_ing.name,
            quantity=payload.quantity,
            unit=payload.unit if payload.unit is not None else current_ing.culinary_unit,
            category=payload.category if payload.category is not None else current_ing.category,
            shelf_life_days=(
                payload.shelf_life_days
                if payload.shelf_life_days is not None
                else current_ing.shelf_life_days
            ),
        )
        target_ing, was_new_ingredient = _find_or_create_ingredient(
            session,
            name=norm["name"],
            unit=norm["unit"],
            category=norm["cat"],
            shelf_life_days=norm["sl"],
        )
        # Re-point the row to the swapped ingredient
        ri.ingredient_id = target_ing.id
        if norm["q"] is not None:
            ri.quantity_required = float(norm["q"])
    else:
        # Quantity-only edit — re-canonicalize through the same pipeline so
        # "5 ounces" gets normalized even if the user re-typed the unit.
        if payload.quantity is not None:
            norm = _normalize_user_ingredient(
                name=current_ing.name,
                quantity=payload.quantity,
                unit=current_ing.culinary_unit,
                category=current_ing.category,
                shelf_life_days=current_ing.shelf_life_days,
            )
            ri.quantity_required = float(norm["q"]) if norm["q"] is not None else None

    session.add(ri)
    session.commit()
    session.refresh(ri)

    if was_new_ingredient:
        _schedule_usda_backfill_for_ingredient(ri.ingredient_id)

    return {
        "ok": True,
        "swapped_ingredient": identity_changed,
        "was_new_ingredient": was_new_ingredient,
        "row": _serialize_recipe_ingredient(session, ri),
    }


@router.delete("/menu/recipe-ingredients/{ri_id}")
def delete_recipe_ingredient(
    ri_id: str,
    session: Session = Depends(get_session),
):
    try:
        rid = uuid.UUID(ri_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid recipe-ingredient id")
    ri = session.get(RecipeIngredient, rid)
    if not ri:
        raise HTTPException(status_code=404, detail="Recipe-ingredient row not found")
    session.delete(ri)
    session.commit()
    return {"ok": True, "deleted_id": ri_id}


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
