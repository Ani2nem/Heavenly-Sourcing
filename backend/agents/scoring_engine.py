from typing import List, Dict, Any
from openai import OpenAI
from config import settings

_client = OpenAI(api_key=settings.openai_api_key)

RECOMMENDATION_PROMPT = """\
You are a restaurant procurement advisor. Given these vendor quotes and USDA benchmarks, write a \
2-3 sentence recommendation card highlighting:
1. Winning vendor and their score
2. Key trade-off vs. the runner-up
3. Any red flags (e.g., pricing above USDA regional average)

Return ONLY natural language text — no JSON, no markdown, no bullet points.
"""


def score_quotes(
    quotes: List[Dict[str, Any]],
    preferred_window: str,
    usda_benchmarks: List[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Applies heuristic scoring:
      score = 0.50 * price_score + 0.20 * window_score + 0.15 * reliability_score + 0.15 * perishable_score
    """
    if not quotes:
        return []

    # Price score: normalize to lowest total quoted price
    prices = [q.get("total_quoted_price") or 0 for q in quotes]
    min_price = min(p for p in prices if p > 0) if any(p > 0 for p in prices) else 1

    for q in quotes:
        price = q.get("total_quoted_price") or 0
        price_score = (min_price / price * 100) if price > 0 else 0

        window_score = 100 if q.get("delivery_window") == preferred_window else 50

        # Default reliability 100 (no history yet in Phase 1)
        reliability_score = q.get("reliability_score", 100)

        # Perishable split: 100 if distributor handles split-drop, 50 otherwise
        perishable_score = 100 if q.get("handles_split_drop", False) else 50

        q["score"] = round(
            0.50 * price_score
            + 0.20 * window_score
            + 0.15 * reliability_score
            + 0.15 * perishable_score,
            2,
        )

    return sorted(quotes, key=lambda q: q["score"], reverse=True)


def generate_recommendation(
    scored_quotes: List[Dict[str, Any]],
    usda_benchmarks: List[Dict[str, Any]] = None,
    manager_preferences: str = "",
) -> str:
    """Call GPT-4o-mini to produce a plain-text recommendation card."""
    if not scored_quotes:
        return "No quotes available to evaluate."

    payload = {
        "quotes": scored_quotes[:5],
        "usda_benchmarks": usda_benchmarks or [],
        "manager_preferences": manager_preferences,
    }

    import json
    response = _client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": RECOMMENDATION_PROMPT},
            {"role": "user", "content": json.dumps(payload)},
        ],
        temperature=0.3,
        max_tokens=300,
    )
    return response.choices[0].message.content.strip()


def check_split_delivery(
    ingredient_name: str,
    culinary_qty_needed: float,
    purchasing_qty_lbs: float,
    shelf_life_days: int,
) -> Dict[str, Any]:
    """Returns split-delivery schedule if perishable span exceeds shelf life."""
    daily_usage = culinary_qty_needed / 7 if culinary_qty_needed > 0 else 0
    spanned_days = (purchasing_qty_lbs / daily_usage) if daily_usage > 0 else 999

    if spanned_days > shelf_life_days and shelf_life_days <= 4:
        return {
            "requires_split": True,
            "drop_1_qty": purchasing_qty_lbs / 2,
            "drop_2_qty": purchasing_qty_lbs / 2,
        }
    return {"requires_split": False}
