"""
Contract PDF / text extractor.

Takes a contract PDF or pasted text and returns a structured dict that maps
1:1 onto the `Contract` + `ContractLineItem` + `ContractTerm` shape. Each
field carries a `needs_verification` flag so the verifier UI can highlight
the things the LLM was less sure about (typical examples: pricing
methodology when the contract is a mixed cost-plus / market-tied hybrid,
auto-renewal language buried in a clause, exclusivity carve-outs).

Design choices
~~~~~~~~~~~~~~

1. Text extraction is PDF-first (pymupdf), with a graceful fallback to
   "user pasted plain text". We deliberately do NOT do vision on contract
   PDFs — they're almost always text-extractable, and OCR introduces
   another failure mode without a clear win.

2. The LLM is given an *explicit allow-list* of pricing structures, term
   keys, and category strings so it can't invent novel enum values that
   the verifier UI then can't render. Anything truly novel goes into
   `extracted_terms` with `needs_verification: true`.

3. Output is normalized into the exact shape the contract router and the
   verifier UI consume — we don't ship the raw LLM dict around the system.

4. For demo seeding we ship a hand-written believable Sysco-style contract
   that exercises the same code path (`extract_from_text`) as a real
   upload, so the seed flow stays in lockstep with the upload flow.
"""
from __future__ import annotations

import base64
import io
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from openai import OpenAI

from config import settings

log = logging.getLogger(__name__)
_client = OpenAI(api_key=settings.openai_api_key)


# Canonical allow-lists shared with the verifier UI. Keep these in sync
# with the dropdowns in `frontend/src/components/ContractVerifier.jsx`.
ALLOWED_PRICING_STRUCTURES = ["FIXED", "COST_PLUS", "MARKET_TIED", "MIXED"]
ALLOWED_CATEGORIES = [
    "Dairy", "Produce", "Proteins", "Dry Goods", "Bakery", "Frozen",
    "Condiments", "Beverage", "Disposables", "Other",
]

# Known extracted_terms keys. We define a stable list so the verifier UI
# can render them in a predictable order — extra keys the LLM returns get
# rendered in an "Other clauses (please review)" section.
KNOWN_TERM_KEYS = [
    "payment_terms_days",        # int
    "min_order_dollars",         # float
    "min_order_lines",           # int
    "delivery_window",           # free text: "Mon/Wed/Fri AM"
    "delivery_cadence",          # WEEKLY | BIWEEKLY | DAILY | ON_DEMAND
    "auto_renewal",              # bool
    "renewal_notice_days",       # int (mirrors Contract.renewal_notice_days)
    "termination_fee_pct",       # float
    "exclusivity",               # bool
    "exclusivity_carveouts",     # list[str]
    "fuel_surcharge",            # str/free
    "broken_case_fee",           # str/free
    "volume_rebate_tiers",       # list[dict]
    "price_index_reference",     # str — e.g. "CME Cheese Block weekly"
    "price_review_cadence",      # MONTHLY | QUARTERLY | ANNUAL | FIXED
]


# ─── PDF text extraction ─────────────────────────────────────────────────────

def _extract_pdf_text(pdf_bytes: bytes) -> Dict[str, Any]:
    """Pull plain text out of a PDF using pymupdf.

    Returns ``{"text": str, "page_count": int}``. On failure returns the
    empty string and 0 pages — caller decides whether to fall back to
    asking the user for typed input.
    """
    try:
        import pymupdf  # type: ignore
    except Exception as exc:
        log.warning("[contract] pymupdf not importable: %s", exc)
        return {"text": "", "page_count": 0}

    try:
        with pymupdf.open(stream=io.BytesIO(pdf_bytes), filetype="pdf") as doc:
            chunks: List[str] = []
            for page in doc:
                chunks.append(page.get_text("text"))
            return {"text": "\n\n".join(chunks), "page_count": doc.page_count}
    except Exception as exc:
        log.warning("[contract] pymupdf extraction failed: %s", exc)
        return {"text": "", "page_count": 0}


def extract_text_from_upload(base64_content: str, mime_type: str) -> Dict[str, Any]:
    """Public helper used by the contract upload endpoint.

    Returns ``{"text", "page_count", "filename_hint"}``. For non-PDF
    uploads we currently treat the bytes as already-text (handy for the
    demo seed path); a future improvement is to OCR images.
    """
    try:
        raw = base64.b64decode(base64_content)
    except Exception as exc:
        raise ValueError(f"Invalid base64 payload: {exc}") from exc

    if mime_type == "application/pdf":
        result = _extract_pdf_text(raw)
        return {"text": result["text"], "page_count": result["page_count"]}

    if mime_type.startswith("text/"):
        try:
            return {"text": raw.decode("utf-8", errors="ignore"), "page_count": 1}
        except Exception as exc:
            raise ValueError(f"Failed to decode text payload: {exc}") from exc

    raise ValueError(
        f"Unsupported mime_type {mime_type!r}; expected application/pdf or text/*"
    )


# ─── LLM extraction prompt ───────────────────────────────────────────────────

_EXTRACTOR_SYSTEM_PROMPT = """\
You read a long-term foodservice procurement contract between a restaurant
buyer and a wholesale food distributor. Produce a structured summary as
JSON only — no markdown, no preamble.

Return EXACTLY this shape:

{
  "nickname": "string — short label the user will see in the UI",
  "vendor": {
    "name": "string — the distributor's legal/trade name",
    "primary_domain": "string|null — e.g. sysco.com",
    "headquarters_city": "string|null",
    "headquarters_state": "string|null",
    "service_region": "string|null — e.g. \\"Bay Area\\", \\"national\\""
  },
  "primary_category": "Dairy|Produce|Proteins|Dry Goods|Bakery|Frozen|Condiments|Beverage|Disposables|Other",
  "category_coverage": ["string", ...],
  "start_date": "YYYY-MM-DD|null",
  "end_date": "YYYY-MM-DD|null",
  "pricing_structure": "FIXED|COST_PLUS|MARKET_TIED|MIXED",
  "line_items": [
    {
      "sku_name": "string",
      "pack_description": "string|null",
      "unit_of_measure": "string|null",
      "fixed_price": "float|null",
      "price_formula": "string|null",
      "min_volume": "float|null",
      "min_volume_period": "annual|monthly|null",
      "notes": "string|null"
    }
  ],
  "extracted_terms": {
    "<term_key>": {"value": <any>, "needs_verification": bool, "notes": "string|null"}
  },
  "low_confidence_fields": ["dotted.path.to.field", ...]
}

Rules:

1. If a field is missing, set it to null — do NOT guess. Missing dates and
   missing pricing structure are common; signal them as null + add the
   field path to low_confidence_fields.

2. category_coverage lists every distinct category the contract spans
   (one contract often covers Dairy + Frozen, for example). Always include
   the primary_category in this list.

3. extracted_terms is the bag for the messy long tail. Use these keys
   when you can (others are allowed but mark them needs_verification:true):
     payment_terms_days, min_order_dollars, min_order_lines,
     delivery_window, delivery_cadence, auto_renewal, renewal_notice_days,
     termination_fee_pct, exclusivity, exclusivity_carveouts,
     fuel_surcharge, broken_case_fee, volume_rebate_tiers,
     price_index_reference, price_review_cadence.

4. Set needs_verification=true on a term if ANY of:
     - the underlying clause is ambiguous,
     - the term is unusual relative to standard foodservice agreements,
     - you had to choose between two plausible interpretations.

5. low_confidence_fields lists dotted paths the manager should manually
   double-check ("vendor.name", "end_date", "extracted_terms.exclusivity").
   Be generous — better to over-flag than under-flag.

6. Do NOT extract pricing into fixed_price for market-tied items. Use
   price_formula instead (e.g. "AMS Cheese Central US weekly average +
   $0.35/lb"). Only set fixed_price when the contract guarantees a
   specific dollar amount for the term.

7. The output MUST be a single JSON object. No trailing commentary.
"""


def _strip_fence(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s[3:]
        if s.lower().startswith("json"):
            s = s[4:]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _llm_extract(text: str) -> Dict[str, Any]:
    """Run the structured extraction against the OpenAI chat API.

    Falls back to a minimal stub when no API key is configured so the
    upload endpoint still works for offline / CI demos.
    """
    if not settings.openai_api_key:
        log.warning("[contract] no OpenAI key — returning minimal stub")
        return {
            "nickname": "Untitled Contract (no API key)",
            "vendor": {"name": "Unknown"},
            "primary_category": "Other",
            "category_coverage": ["Other"],
            "pricing_structure": "FIXED",
            "line_items": [],
            "extracted_terms": {},
            "low_confidence_fields": ["vendor.name", "primary_category"],
        }

    response = _client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _EXTRACTOR_SYSTEM_PROMPT},
            {"role": "user", "content": text[:60_000]},
        ],
        temperature=0,
        max_tokens=2048,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    try:
        return json.loads(_strip_fence(raw))
    except json.JSONDecodeError as exc:
        log.warning("[contract] LLM returned non-JSON: %s", exc)
        return {}


# ─── Normalisation ──────────────────────────────────────────────────────────

def _normalize_category(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    target = name.strip().lower()
    for canonical in ALLOWED_CATEGORIES:
        if canonical.lower() == target:
            return canonical
    # Fall through to "Other" so the verifier can re-tag rather than
    # rendering a free-form category that breaks downstream filters.
    return "Other"


def _normalize_pricing(value: Optional[str]) -> str:
    if not value:
        return "FIXED"
    v = value.strip().upper().replace(" ", "_").replace("-", "_")
    return v if v in ALLOWED_PRICING_STRUCTURES else "MIXED"


def _parse_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d %B %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except (TypeError, ValueError):
            continue
    return None


def _ensure_term_shape(value: Any) -> Dict[str, Any]:
    """Coerce a raw LLM value into ``{value, needs_verification, notes}``.

    Tolerates the case where the model returned just the bare value instead
    of the full wrapped dict — we still want it captured, just flagged for
    verification.
    """
    if isinstance(value, dict) and "value" in value:
        return {
            "value": value.get("value"),
            "needs_verification": bool(value.get("needs_verification", False)),
            "notes": value.get("notes"),
        }
    return {"value": value, "needs_verification": True, "notes": None}


def normalize_extraction(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Turn the raw LLM output into the canonical contract dict the router
    consumes. Pure function — easy to unit-test without the LLM.
    """
    payload = payload or {}
    vendor_raw = payload.get("vendor") or {}
    line_items_raw = payload.get("line_items") or []
    extracted_terms_raw = payload.get("extracted_terms") or {}

    extracted_terms: Dict[str, Any] = {
        key: _ensure_term_shape(extracted_terms_raw.get(key))
        for key in extracted_terms_raw.keys()
    }

    return {
        "nickname": (payload.get("nickname") or "Untitled Contract").strip()[:255],
        "vendor": {
            "name": (vendor_raw.get("name") or "Unknown Vendor").strip()[:255],
            "primary_domain": vendor_raw.get("primary_domain"),
            "headquarters_city": vendor_raw.get("headquarters_city"),
            "headquarters_state": vendor_raw.get("headquarters_state"),
            "service_region": vendor_raw.get("service_region"),
        },
        "primary_category": _normalize_category(payload.get("primary_category")),
        "category_coverage": [
            c for c in (
                _normalize_category(x) for x in (payload.get("category_coverage") or [])
            ) if c
        ],
        "start_date": _parse_date(payload.get("start_date")),
        "end_date": _parse_date(payload.get("end_date")),
        "pricing_structure": _normalize_pricing(payload.get("pricing_structure")),
        "line_items": [
            {
                "sku_name": (li.get("sku_name") or "").strip()[:255],
                "pack_description": li.get("pack_description"),
                "unit_of_measure": li.get("unit_of_measure"),
                "fixed_price": li.get("fixed_price"),
                "price_formula": li.get("price_formula"),
                "min_volume": li.get("min_volume"),
                "min_volume_period": li.get("min_volume_period"),
                "notes": li.get("notes"),
            }
            for li in line_items_raw
            if isinstance(li, dict) and (li.get("sku_name") or "").strip()
        ],
        "extracted_terms": extracted_terms,
        "low_confidence_fields": list(payload.get("low_confidence_fields") or []),
    }


def extract_from_text(text: str) -> Dict[str, Any]:
    """Top-level entry point: raw text in, normalised contract dict out.

    Used by the upload endpoint AND the demo seed endpoint so both code
    paths exercise identical normalisation logic.
    """
    if not (text or "").strip():
        # Empty input — emit a minimal valid contract that the verifier
        # UI will show as "everything needs filling in".
        return normalize_extraction({})
    raw = _llm_extract(text)
    return normalize_extraction(raw)


# ─── Demo seed contract ──────────────────────────────────────────────────────

DEMO_CONTRACT_TEXT = """\
MASTER PROCUREMENT AGREEMENT

Between:
  Buyer:    Heavenly Pizzeria, LLC
  Supplier: Riverbend Foodservice, Inc.
            HQ: 2200 Industrial Way, Oakland, CA
            Service region: Greater Bay Area

Effective Date: 2025-09-01
Expiration Date: 2026-08-31
Renewal Notice: 60 days before expiration. This agreement does NOT
auto-renew; written renewal terms must be agreed in writing by both
parties.

Categories Covered: Dairy (primary), Produce, Frozen.

Pricing Structure: MIXED.
  - Cheese (mozzarella, ricotta, parmesan): MARKET_TIED. Weekly price =
    USDA AMS Cheese - Central U.S. weighted average + $0.35/lb spread.
  - Produce (tomato, basil, mushroom, bell pepper): COST_PLUS at landed
    cost + 11%.
  - Frozen pizza dough (case of 24, 12 oz balls): FIXED at $28.50/case.

Minimum Order: $325 per delivery or 12 line items, whichever is greater.

Delivery Cadence: Weekly, Monday and Thursday between 5:00 AM and 9:00 AM.

Payment Terms: Net 21 from invoice date. 1.5% monthly finance charge on
balances over 30 days.

Termination: Either party may terminate with 30 days written notice.
Early termination by the Buyer within the first 6 months incurs a 4%
restocking fee on outstanding committed volume.

Volume Rebate:
  - Tier 1: $3,000 - $5,000 / month → 2% credit
  - Tier 2: $5,001 - $8,000 / month → 3% credit
  - Tier 3: $8,001+        / month → 4.5% credit

Exclusivity: Buyer is NOT exclusive to Supplier; Buyer may purchase any
SKU not on Supplier's published price list from other vendors without
penalty.

Fuel Surcharge: Pass-through at posted DOE national diesel average,
calculated weekly.

Signed: 2025-08-15
"""


def build_demo_contract() -> Dict[str, Any]:
    """Generate a believable seed contract for the demo path.

    Runs through the same extractor as a real upload so the verifier UI
    has something to render. When no OpenAI key is configured we return
    a hand-written normalised payload so demos still work offline.
    """
    if settings.openai_api_key:
        return extract_from_text(DEMO_CONTRACT_TEXT)

    today = date.today()
    return normalize_extraction({
        "nickname": "Riverbend Foodservice — Dairy/Produce/Frozen",
        "vendor": {
            "name": "Riverbend Foodservice, Inc.",
            "primary_domain": "riverbendfoodservice.com",
            "headquarters_city": "Oakland",
            "headquarters_state": "CA",
            "service_region": "Greater Bay Area",
        },
        "primary_category": "Dairy",
        "category_coverage": ["Dairy", "Produce", "Frozen"],
        "start_date": (today - timedelta(days=120)).isoformat(),
        "end_date": (today + timedelta(days=60)).isoformat(),
        "pricing_structure": "MIXED",
        "line_items": [
            {
                "sku_name": "Mozzarella, Low-Moisture Part-Skim",
                "pack_description": "6/5 lb loaf",
                "unit_of_measure": "lb",
                "fixed_price": None,
                "price_formula": "AMS Cheese - Central U.S. weekly avg + $0.35/lb",
            },
            {
                "sku_name": "Pizza Dough, 12oz Balls",
                "pack_description": "case of 24",
                "unit_of_measure": "case",
                "fixed_price": 28.50,
            },
            {
                "sku_name": "Roma Tomato",
                "pack_description": "25 lb case",
                "unit_of_measure": "lb",
                "price_formula": "landed cost + 11%",
            },
        ],
        "extracted_terms": {
            "payment_terms_days": {"value": 21, "needs_verification": False},
            "min_order_dollars": {"value": 325.0, "needs_verification": False},
            "min_order_lines": {"value": 12, "needs_verification": False},
            "delivery_window": {"value": "Mon & Thu, 5:00–9:00 AM",
                                 "needs_verification": False},
            "delivery_cadence": {"value": "WEEKLY", "needs_verification": False},
            "auto_renewal": {"value": False, "needs_verification": False},
            "renewal_notice_days": {"value": 60, "needs_verification": False},
            "termination_fee_pct": {"value": 4.0, "needs_verification": True,
                                     "notes": "Only in first 6 months; verify still applies"},
            "exclusivity": {"value": False, "needs_verification": False},
            "fuel_surcharge": {"value": "Pass-through at DOE national diesel avg",
                                "needs_verification": True},
            "volume_rebate_tiers": {
                "value": [
                    {"low": 3000, "high": 5000, "rebate_pct": 2.0},
                    {"low": 5001, "high": 8000, "rebate_pct": 3.0},
                    {"low": 8001, "high": None, "rebate_pct": 4.5},
                ],
                "needs_verification": False,
            },
            "price_index_reference": {
                "value": "USDA AMS Cheese - Central U.S. weighted avg",
                "needs_verification": False,
            },
            "price_review_cadence": {"value": "WEEKLY", "needs_verification": True},
        },
        "low_confidence_fields": ["extracted_terms.termination_fee_pct",
                                   "extracted_terms.fuel_surcharge"],
    })
