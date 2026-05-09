import base64
import re
from json import JSONDecodeError, JSONDecoder
from typing import List
from openai import OpenAI
from config import settings

_client = OpenAI(api_key=settings.openai_api_key)

# Text PDFs can exceed a single completion (gpt-4o max ~16k out tokens). Parse in page batches.
TEXT_PAGES_PER_BATCH = 2

# ─── System prompts ───────────────────────────────────────────────────────────

# Used for text-based PDFs (preferred path — no vision needed)
MENU_TEXT_SYSTEM_PROMPT = """\
You are a restaurant procurement data extractor. Given raw menu text, produce a complete, accurate \
ingredient list for every purchasable dish.

━━ DEDUPLICATION RULES ━━
• Size variants are ONE dish. "12\" Mediterranean" + "18\" Mediterranean" → "Mediterranean Pizza" \
(use the lower price).
• Same filling name in DIFFERENT sections are SEPARATE dishes — prefix with section type:
  Pizzas section      → "Mediterranean Pizza"
  Strombolis section  → "Mediterranean Stromboli"
  Calzones section    → "Mediterranean Calzone"
  Pizza Bowls section → "Mediterranean Bowl"
• SKIP ENTIRELY: Deals, Bundles, Jumbo Slices (duplicates of pizzas), Additional Sauces, \
Build-Your-Own Kit combos.
• Build Your Own pizza items (Cheese, Pepperoni, Homemade Sausage) → include as Pizza dishes if \
not already present.

━━ INGREDIENT RULES ━━
Pizzas & Strombolis always start with:
  1. Pizza Dough     (cat=Bakery,      sl=3)
  2. [Sauce]         (cat=Condiments,  sl=7)
  3. Mozzarella Cheese (cat=Dairy,     sl=7)

Calzones always start with:
  1. Pizza Dough       (cat=Bakery,     sl=3)
  2. [Sauce]           (cat=Condiments, sl=7)
  3. Mozzarella Cheese (cat=Dairy,      sl=7)
  4. Ricotta Cheese    (cat=Dairy,      sl=7)

Then add toppings inferred from the dish name.

SAUCE selection — pick the ONE sauce that fits, treat it as a single ingredient:
  Default / "Pizza Sauce"   → Pizza Sauce
  Bacon Chicken Ranch       → Ranch Sauce
  BBQ Chicken / GodspeedTM  → BBQ Sauce
  Chicken Pesto             → Pesto Sauce
  Chicken Spinach Alfredo   → Alfredo Sauce
  Buffalo Chicken           → Buffalo Sauce
  Viva Mexico!              → Salsa Sauce
  White                     → Alfredo Sauce
  Honey Pig / Honey Crisp   → Pizza Sauce + Honey (add Honey as extra ingredient)

Compound sauces (Ranch, Alfredo, Pesto, BBQ, Buffalo, Salsa) are SINGLE ingredients — \
never decompose them further.

Goat Cheese pizza → replace Mozzarella with Goat Cheese; use Olive Oil as sauce.
Hawaiian / Hawaii Five-O  → Pizza Sauce, add Ham + Pineapple.
Happy Family              → Pizza Sauce, add Ham + Mushrooms + Bell Peppers.
Cheeseburger              → Pizza Sauce, add Ground Beef + Pickles.
GodspeedTM Brisket        → BBQ Sauce, add Smoked Brisket + Red Onion.

━━ CATEGORY + SHELF LIFE DEFAULTS ━━
  Bakery    (dough, bread, pretzels, cookies, cakes, brownies)  sl=3
  Dairy     (cheese, ricotta, cream cheese, butter, cream)      sl=7
  Proteins  (chicken, beef, sausage, pepperoni, bacon, ham,     sl=3
             brisket, anchovies, shrimp)
  Produce   (vegetables, greens, mushrooms, onions, peppers,    sl=4
             tomatoes, spinach, zucchini, pineapple, apple)
  Condiments(all sauces, dressings, honey, hot sauce, oil)      sl=7
  Dry Goods (pasta, flour, breadcrumbs, sugar, chocolate chips) sl=180
  Pantry    (chocolate, cocoa, vanilla, caramel, spices, salt)  sl=365
  Frozen    (ice cream, frozen items)                           sl=90

━━ OTHER SECTIONS ━━
Appetizers: list realistic main ingredients (e.g. Mozzarella Sticks → Mozzarella, Breadcrumbs, Oil).
Pizza Bowls: same toppings as equivalent pizza, NO Pizza Dough.
Penne Pasta: Penne Pasta (Dry Goods), Marinara Sauce (Condiments), Parmesan Cheese (Dairy).
Heavenly Salad: Romaine Lettuce, Tomatoes, Red Onion, Croutons, Parmesan Cheese, Caesar Dressing.
Desserts: list 3-5 main baking ingredients (e.g. Chocolate Cupcake → Flour, Butter, Sugar, Cocoa Powder, Eggs).
Drinks: Include every listed beverage. Each drink is its own dish with exactly one ingredient — the drink \
itself (menu name). Use **unit "each"** with **q** = number of servings (usually 1.0) for bottles/cans, OR \
**fl oz** / **ml** for fountain sizes (estimate 16–20 fl oz if unknown). cat=Pantry or Dry Goods. Never use \
the word "portion" as unit.

━━ QUANTITIES (per ONE standard serving of that dish) ━━
Every ingredient MUST have a positive number **q** and **unit** from this list ONLY (lowercase in JSON):
  Mass: **oz**, **lb**, **g**, **kg**
  Volume: **fl oz**, **ml**, **l**, **cup**, **tbsp**, **tsp**
  Count: **each** (dough balls, eggs, bottles, chicken breasts, slices of bacon, etc.)

Estimate realistic procurement amounts using typical US restaurant yields:
  • Pizza cheese: ~6–10 **oz** per pie (more for meat-lovers); sauce ~4–6 **fl oz**; dough: **1** **each** ball (~9–12 oz raw).
  • Proteins on pizza: sliced pepperoni ~3–5 **oz** cooked equivalent; chicken ~5–7 **oz** raw weight.
  • Produce toppings: mushrooms/peppers ~2–4 **oz** prepared; fresh herbs a few **tbsp** or **g**.
  • Dressings / aioli: 1–3 **fl oz** per salad portion.
  • Dry pasta entrées: dry penne ~4–6 **oz**; oil/butter small mass or volume as appropriate.

Do NOT use **portion** or **serving** as unit. Choose oz, fl oz, each, etc.

━━ OUTPUT ━━
Return ONLY valid JSON — no markdown fences, no explanation, no trailing text.

{
  "dishes": [
    {
      "name": "string",
      "base_price": float,
      "ingredients": [
        {"name": "string", "q": 6.0, "unit": "oz", "cat": "Bakery|Dairy|Proteins|Produce|Condiments|Dry Goods|Pantry|Frozen", "sl": int}
      ]
    }
  ],
  "confidence_score": 95
}
"""

# Used for single-image uploads (JPEG/PNG/WebP)
IMAGE_SYSTEM_PROMPT = """\
You are a culinary data extractor. Given a menu image, extract every dish and its likely ingredients. \
Include beverages: each drink is its own dish with one ingredient (menu name); bottles/cans → **each**, \
fountain drinks → **fl oz** (estimate 16–20 if unknown).

For EVERY ingredient use realistic **q** (positive float) and **unit** per ONE serving: \
oz, lb, g, kg, fl oz, ml, l, cup, tbsp, tsp, or each. Never use "portion" or "serving" as the unit.

Return ONLY valid JSON — no markdown, no preamble.

{
  "dishes": [
    {
      "name": "string",
      "base_price": float or null,
      "ingredients": [
        {"name": "string", "q": 6.0, "unit": "oz", "cat": "Bakery|Dairy|Proteins|Produce|Condiments|Dry Goods|Pantry|Frozen", "sl": int}
      ]
    }
  ],
  "confidence_score": 0-100
}

Shelf-life defaults: Bakery sl=3, Dairy sl=7, Proteins sl=3, Produce sl=4, Condiments sl=7, Dry Goods sl=180, Pantry sl=365, Frozen sl=90.
"""

# Used for image-only (scanned) PDFs — fallback path
PDF_IMAGE_SYSTEM_PROMPT = """\
You are a culinary data extractor. These images are pages from a restaurant menu. Extract every \
dish visible across ALL pages and its likely procurement ingredients.

Same rules as menu extraction:
- Sauces are single ingredients (Ranch Sauce, Alfredo Sauce, etc.) — never decompose them.
- Pizzas/Strombolis: Pizza Dough + Sauce + Mozzarella + toppings.
- Calzones: Pizza Dough + Sauce + Mozzarella + Ricotta + toppings.
- Size variants (12"/18") = ONE dish.
- Drinks: include each beverage; one drink = one dish with one ingredient (the drink itself), not decomposed. \
Use **each** (bottle/can) or **fl oz** (fountain).

Each ingredient must have realistic **q** and allowed **unit** (oz lb g kg fl oz ml l cup tbsp tsp each) per ONE \
serving — never use "portion" as the unit.

Return ONLY valid JSON — no markdown, no explanation:
{
  "dishes": [
    {
      "name": "string",
      "base_price": float or null,
      "ingredients": [
        {"name": "string", "q": 6.0, "unit": "oz", "cat": "Bakery|Dairy|Proteins|Produce|Condiments|Dry Goods|Pantry|Frozen", "sl": int}
      ]
    }
  ],
  "confidence_score": 0-100
}
"""


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _strip_markdown_fences(raw: str) -> str:
    s = raw.strip()
    if not s.startswith("```"):
        return s
    s = s[3:].lstrip()
    if s.lower().startswith("json"):
        s = s[4:].lstrip("\n\r \t")
    if s.rstrip().endswith("```"):
        s = s.rstrip()[:-3].rstrip()
    return s


def _strip_trailing_commas(text: str) -> str:
    """Remove trailing commas before } or ] (common invalid JSON from models)."""
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r",(\s*[}\]])", r"\1", text)
    return text


def _loads_menu_json(text: str) -> dict:
    s = _strip_markdown_fences(text)
    start = s.find("{")
    if start < 0:
        raise ValueError("no JSON object start")

    decoder = JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(s, start)
    except JSONDecodeError:
        obj, _ = decoder.raw_decode(_strip_trailing_commas(s), start)

    if not isinstance(obj, dict):
        raise ValueError("top-level JSON must be an object")
    return obj


def _safe_parse(raw: str, context: str = "") -> dict:
    from agents.ingredient_units import apply_sanitized_dishes

    try:
        return apply_sanitized_dishes(_loads_menu_json(raw))
    except (JSONDecodeError, ValueError) as e:
        ctx = f" ({context})" if context else ""
        print(f"[menu_parser] JSON decode failed{ctx}: {e}")
        print(f"[menu_parser] Raw (first 500 chars): {raw[:500]}")
        if len(raw) > 500:
            print(f"[menu_parser] Raw (last 400 chars): {raw[-400:]}")
        return {"dishes": [], "confidence_score": 0}


# ─── Text PDF chunking ───────────────────────────────────────────────────────


def _split_menu_text_into_page_blocks(raw: str) -> List[str]:
    """
    Split PyMuPDF output on `=== PAGE N ===` lines. If no markers, treat whole text as one block.
    """
    text = (raw or "").strip()
    if not text:
        return []
    pattern = r"(?m)^=== PAGE \d+ ===\s*$"
    matches = list(re.finditer(pattern, text))
    if not matches:
        return [text]
    blocks: List[str] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end].strip()
        if block:
            blocks.append(block)
    return blocks


def _parse_menu_text_single(user_content: str, context: str = "text path") -> dict:
    """Single GPT-4o call for one text chunk (must fit within max_tokens output)."""
    response = _client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": MENU_TEXT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0,
        max_tokens=16384,
        response_format={"type": "json_object"},
    )
    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        print(f"[menu_parser] WARNING: completion truncated ({context}) — reduce TEXT_PAGES_PER_BATCH if JSON fails")
    return _safe_parse((choice.message.content or "").strip(), context)


# ─── Public API ───────────────────────────────────────────────────────────────

def parse_menu_text(text: str) -> dict:
    """
    Primary path for text-extractable PDFs.
    Splits on page boundaries and batches pages so each JSON response stays within model output limits.
    """
    pages = _split_menu_text_into_page_blocks(text)
    if not pages:
        return {"dishes": [], "confidence_score": 0}

    batches: List[str] = []
    for i in range(0, len(pages), TEXT_PAGES_PER_BATCH):
        batches.append("\n\n".join(pages[i : i + TEXT_PAGES_PER_BATCH]))

    if len(batches) == 1:
        wrapped = (
            "This is the full restaurant menu text from a PDF. "
            "Extract every dish and ingredient.\n\n"
            + batches[0]
        )
        return _parse_menu_text_single(wrapped, "text path")

    all_dishes: list = []
    confidence_sum = 0.0
    n_batches = len(batches)
    for bi, batch in enumerate(batches):
        start_p = bi * TEXT_PAGES_PER_BATCH + 1
        end_p = min((bi + 1) * TEXT_PAGES_PER_BATCH, len(pages))
        wrapped = (
            f"This is an excerpt of a {len(pages)}-page restaurant menu (pages {start_p}–{end_p} of {len(pages)}). "
            "Extract every purchasable dish and its ingredients that appear ONLY in this excerpt. "
            "Do not invent dishes from other pages.\n\n"
            + batch
        )
        part = _parse_menu_text_single(wrapped, f"text path batch {bi + 1}/{n_batches}")
        all_dishes.extend(part.get("dishes") or [])
        confidence_sum += float(part.get("confidence_score") or 0)

    return {
        "dishes": all_dishes,
        "confidence_score": round(confidence_sum / n_batches, 1) if n_batches else 0,
    }


def parse_menu(base64_content: str, mime_type: str = "image/jpeg") -> dict:
    """Single-image path — JPEG/PNG/WebP uploads."""
    data_url = f"data:{mime_type};base64,{base64_content}"
    response = _client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": IMAGE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Extract all dishes and ingredients from this menu."},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                ],
            },
        ],
        temperature=0,
        max_tokens=8192,
        response_format={"type": "json_object"},
    )
    return _safe_parse(response.choices[0].message.content.strip(), "single image")


def parse_menu_pages(page_images: List[bytes], page_offset: int = 0) -> dict:
    """
    Fallback vision path — used only for image-only (scanned) PDFs.
    Batch size is controlled by the caller (PDF_BATCH_SIZE in api/menu.py).
    Uses 'high' detail so text in rendered pages is legible.
    """
    start = page_offset + 1
    end = page_offset + len(page_images)

    content: list = [
        {
            "type": "text",
            "text": (
                f"These are pages {start} to {end} of a restaurant menu. "
                "Extract every dish visible across ALL pages."
            ),
        }
    ]
    for img_bytes in page_images:
        b64 = base64.b64encode(img_bytes).decode()
        content.append({
            "type": "image_url",
            # 'high' is required — 'low' renders text too small to read
            "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
        })

    response = _client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": PDF_IMAGE_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        temperature=0,
        max_tokens=8192,
        response_format={"type": "json_object"},
    )
    return _safe_parse(response.choices[0].message.content.strip(), f"pages {start}-{end}")
