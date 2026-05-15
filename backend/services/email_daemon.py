"""Email send + IMAP poll daemon.

Uses **synchronous** smtplib + imaplib intentionally. Earlier we used aiosmtplib
+ aioimaplib called via ``asyncio.run`` from a sync APScheduler/BackgroundTasks
context, which produced two recurring failure modes:

  1. ``Fatal error on SSL transport`` / ``RuntimeError: Event loop is closed``
     when ``asyncio.run()`` returned before aiosmtplib finished tearing down
     its SSL connection.
  2. ``[imap] poll error: list index out of range`` because Gmail's FETCH
     responses don't always have the RFC822 body at ``msg_data[1]``.

Sync libraries side-step both: APScheduler and FastAPI BackgroundTasks both
already run jobs in worker threads, so blocking I/O is fine, and ``imaplib``
returns a single bytes blob per FETCH which is much easier to parse safely.
"""
from __future__ import annotations

import email as email_lib
import html as html_std
import imaplib
import json
import re
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from openai import OpenAI
from sqlmodel import select

from config import settings

_scheduler = BackgroundScheduler()
_openai = OpenAI(api_key=settings.openai_api_key)


# ─── LLM prompts ──────────────────────────────────────────────────────────────

QUOTE_PARSE_SYSTEM = """\
You read a vendor pricing email reply for a restaurant procurement RFP.
Return ONLY valid JSON — no markdown, no preamble.

{
  "items": [
    {"ingredient_name": "string", "price_per_unit": float, "unit": "string|null", "notes": "string|null"}
  ],
  "gaps": ["ingredient name we asked about but vendor cannot supply"],
  "substitutions": [{"original": "string", "substitute": "string", "reason": "string"}],
  "handles_split_drop": false
}

CRITICAL — avoid these pitfalls:

- The body MAY contain a quoted copy of OUR original RFP (Gmail and most
  clients append the original below "On <date>, <name> wrote:"). Inside that
  RFP we include a column called "Reference Benchmark" that looks like
  "$1.80/lb (USDA AMS, Apr 28)" or "~$5.00/lb (industry est, condiments)".
  Those are OUR reference prices — they are NEVER vendor prices.
  IGNORE that column entirely. Never return a benchmark value as
  price_per_unit.

- Only extract prices the vendor themselves quoted (typically near the top
  of the message, before the "On ... wrote:" quote separator). If the only
  numbers you find are in the "Reference Benchmark" column, the vendor has
  not yet replied with prices — return items=[] and add nothing to gaps.

- Vendor prices usually appear in a column named "Per-Unit Price",
  "Unit Price", "Your Price", or inline as "<ingredient>: $X.XX". When in
  doubt, prefer the column whose header contains the word "Price" without
  "Reference" / "Benchmark" / "USDA" / "AMS" / "industry est".

Rules:
- Match ingredient_name to the names in the original RFP table when possible.
- price_per_unit is the per-unit numeric price (no currency symbol, no slash).
- If the vendor cannot supply something, add it to "gaps".
- If they propose a substitute, add to "substitutions".
- handles_split_drop=true only if the vendor confirms split delivery (Fri+Mon).
"""

RECEIPT_PARSE_SYSTEM = """\
You read a vendor receipt / invoice email confirming a purchase order.
Return ONLY valid JSON — no markdown, no preamble.

{
  "receipt_number": "string|null",
  "total_amount": float|null,
  "items": [
    {"ingredient_name": "string", "qty": float|null, "unit_price": float|null, "line_total": float|null}
  ]
}

Be permissive: if any field is missing in the email, set it to null instead of guessing.
"""

CONTRACT_REPLY_PARSE_SYSTEM = """\
You read an email reply from a wholesale vendor responding to a restaurant contract renewal or exploratory RFP.
Return ONLY valid JSON — no markdown, no preamble.

{
  "summary": "string — one paragraph",
  "pricing_items": [
    {
      "label": "string",
      "price_numeric": float|null,
      "price_low": float|null,
      "price_high": float|null,
      "unit": "string|null",
      "notes": "string|null"
    }
  ],
  "term_months": int|null,
  "firm_commitment_detected": false,
  "confidence": "high|medium|low"
}

Rules:
- Ignore quoted thread history when judging vendor-authored content (the excerpt may already be truncated).
- firm_commitment_detected=true only if they quote a single final binding price without hedges like \"approximately\", \"indicative\", or \"subject to approval\".
- Prefer price_numeric when present; else use midpoint of price_low and price_high when both are numbers.
- If no usable numeric anchors exist, use pricing_items=[].
"""


# ─── Reference helpers ────────────────────────────────────────────────────────

_QUOTE_REF_RE = re.compile(r"Cycle\s+([\w-]+)\s*/\s*Quote\s+([\w-]+)", re.I)
_PO_REF_RE = re.compile(r"Cycle\s+([\w-]+)\s*/\s*PO\s+([\w-]+)", re.I)
_CONTRACT_NEG_REF_RE = re.compile(
    r"Contract\s+([\w-]{8,})\s*/\s*Negotiation\s+([\w-]{8,})",
    re.I,
)
_RECEIPT_HINTS = (
    "receipt",
    "invoice",
    "purchase order confirmation",
    "po confirmation",
    "delivery confirmation",
)


def _extract_quote_ref(subject: str, body: str) -> Optional[Tuple[str, str]]:
    m = _QUOTE_REF_RE.search(f"{subject}\n{body}")
    if m:
        return m.group(1), m.group(2)
    return None


def _extract_po_ref(subject: str, body: str) -> Optional[Tuple[str, str]]:
    m = _PO_REF_RE.search(f"{subject}\n{body}")
    if m:
        return m.group(1), m.group(2)
    return None


def _extract_contract_neg_ref(subject: str, body: str) -> Optional[Tuple[str, str]]:
    m = _CONTRACT_NEG_REF_RE.search(f"{subject}\n{body}")
    if m:
        return m.group(1), m.group(2)
    return None


def _looks_like_receipt(subject: str, body: str) -> bool:
    haystack = (subject + "\n" + body).lower()
    return any(h in haystack for h in _RECEIPT_HINTS)


# Subject prefixes for emails the system itself sends. If we see one of these
# bouncing back via the inbox (which happens with plus-addressing demo routing,
# e.g. ani2nem+vendor@gmail.com loops back into ani2nem@gmail.com), we MUST
# ignore it — otherwise the LLM extracts the "Reference Benchmark" column
# from our own outbound RFP and treats USDA averages as vendor prices.
_OUTBOUND_SUBJECT_PREFIXES = (
    "rfp:",
    "follow-up:",
    "final order —",
    "purchase order confirmed",
    "price match request",
    "invoice request",
    "renewal discussion",
    "contract rfp",
    "contract discussion",
)


def _normalize_address(value: str) -> str:
    """Lowercase + strip plus-tag from a single email address.

    'Ani <Ani2Nem+Royal_Food@Gmail.com>' -> 'ani2nem@gmail.com'
    """
    import email.utils as _eutils
    _name, addr = _eutils.parseaddr(value or "")
    addr = (addr or "").strip().lower()
    if "@" not in addr:
        return ""
    local, _, domain = addr.partition("@")
    local = local.split("+", 1)[0]
    return f"{local}@{domain}"


def _is_self_sent(msg) -> bool:
    """True if this message's From: header is the operator's own mailbox.

    Catches the plus-addressing self-loop: when we send
    From: ani2nem@gmail.com -> To: ani2nem+vendor@gmail.com, Gmail delivers a
    copy back into INBOX. We must NOT process those as vendor replies.
    """
    own_addrs = {
        _normalize_address(settings.smtp_user),
        _normalize_address(settings.imap_user),
    }
    own_addrs.discard("")
    if not own_addrs:
        return False
    sender = _normalize_address(msg.get("From", ""))
    return sender in own_addrs


def _decode_subject(raw_subject: str) -> str:
    """Decode RFC 2047 / MIME-encoded-words back to a plain unicode string.

    Our outbound emails contain em-dashes ("—"), which Python's MIME layer
    auto-encodes as ``=?utf-8?Q?RFP=3A_..._=E2=80=94_...?=``. Without
    decoding, no startswith() check would ever match.
    """
    if not raw_subject:
        return ""
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(raw_subject)))
    except Exception:
        return raw_subject


def _is_our_outbound_subject(subject: str) -> bool:
    """True if the (already-decoded) subject starts with one of our outbound prefixes.

    We deliberately do NOT strip ``Re:`` / ``Fwd:`` here — a legitimate vendor
    reply will be ``Re: RFP: …`` which we WANT to process, but a self-loop
    copy of our own outbound is ``RFP: …`` (no Re: prefix) which we want to
    skip. Caller is responsible for decoding via :func:`_decode_subject`.
    """
    s = (subject or "").strip().lower()
    return any(s.startswith(p) for p in _OUTBOUND_SUBJECT_PREFIXES)


# ─── Email templates ──────────────────────────────────────────────────────────

def _build_rfp_html(
    distributor_name: str,
    ingredient_list: List[Dict[str, Any]],
    cycle_id: str,
    quote_id: str,
    benchmarks: Optional[List[Dict[str, Any]]] = None,
) -> str:
    esc = lambda s: html_std.escape(str(s or ""), quote=False)
    safe_dist = esc(distributor_name)

    bench_by_name = {b["name"]: b for b in (benchmarks or [])}

    def _bench_cell(name: str) -> str:
        """Render the benchmark cell using the pre-formatted label from
        :func:`services.usda_client.build_benchmarks`. The label already
        encodes the correct unit (lb / gal / each / etc.) and an honest
        source tag (`USDA AMS` for real data, `industry est` for the
        category fallback). Items with no signal render as `—`.
        """
        b = bench_by_name.get(name)
        if not b:
            return "—"
        label = b.get("label")
        if label:
            return label
        # Legacy fallback: very old benchmark dicts only had `benchmark_per_lb`.
        legacy = b.get("benchmark_per_lb")
        if legacy is not None:
            return f"~${legacy:.2f}/lb (industry est)"
        return "—"

    def _delivery_cell(shelf_life_days: int) -> str:
        if shelf_life_days <= 4:
            return "Fri AM (½) + Mon AM (½) — split drop"
        return "Mon AM"

    def _recipe_need_cell(item: Dict[str, Any]) -> str:
        """The amount our recipes actually consume per cycle (informational)."""
        plan = item.get("purchase_plan")
        if plan:
            return f"{plan['recipe_need_qty']:.2f} {esc(plan['recipe_need_unit'])}"
        return f"{item['qty']:.2f} {esc(item['unit'])}"

    def _order_cell(item: Dict[str, Any]) -> str:
        """What we actually want the vendor to ship — pack-rounded.
        Falls back to the raw recipe need when we don't have a pack rule
        so the column never blanks out."""
        plan = item.get("purchase_plan")
        if plan:
            return (
                f"<strong>{esc(plan['packs_needed'])} × {esc(plan['pack_label'])}</strong>"
                f"<br><span style=\"color:#64748b;font-size:90%\">"
                f"= {plan['total_in_pack_unit']:.0f} {esc(plan['pack_unit'])} total</span>"
            )
        return f"{item['qty']:.2f} {esc(item['unit'])}"

    has_split = any((i.get("shelf_life_days") or 99) <= 4 for i in ingredient_list)
    has_pack_plan = any(i.get("purchase_plan") for i in ingredient_list)

    rows = "".join(
        (
            f"<tr>"
            f"<td>{esc(i['name'])}</td>"
            f"<td>{_recipe_need_cell(i)}</td>"
            f"<td>{_order_cell(i)}</td>"
            f"<td>{_delivery_cell(i['shelf_life_days'])}</td>"
            f"<td>{esc(_bench_cell(i['name']))}</td>"
            f"</tr>"
        )
        for i in ingredient_list
    )

    delivery_block = """
<h3 style="font-size:13px;text-transform:uppercase;letter-spacing:0.04em;color:#64748b;margin-top:1.25em;">Delivery &amp; service (scope)</h3>
<ul style="margin-top:0.35em;">
  <li><strong>Requested windows:</strong> default <strong>Monday morning</strong> for standard items.</li>"""
    if has_split:
        delivery_block += """
  <li><strong>Split-drop items:</strong> short shelf-life SKUs — please confirm ability to deliver
      <strong>half Friday AM</strong> and <strong>half Monday AM</strong> (or propose an equivalent that protects yield).</li>"""
    delivery_block += """
  <li>If you cannot meet a window, please state your nearest feasible schedule and any cut-off times.</li>
</ul>
"""

    pack_note = ""
    if has_pack_plan:
        pack_note = (
            "<h3 style=\"font-size:13px;text-transform:uppercase;letter-spacing:0.04em;color:#64748b;margin-top:1.25em;\">"
            "How to structure your quote</h3>"
            "<p>Recipes consume the <em>Recipe Need</em> quantity; please quote against the wholesale pack described in "
            "<em>Order This</em> (price <strong>per pack</strong> unless you specify otherwise). "
            "If an alternate case/pack is standard for your catalogue, list it explicitly.</p>"
        )

    lifecycle_note = """
<h3 style="font-size:13px;text-transform:uppercase;letter-spacing:0.04em;color:#64748b;margin-top:1.25em;">Where this fits in our process</h3>
<p style="font-size:13px;color:#334155;">
  In foodservice procurement, pricing requests typically precede a formal supplier relationship:
  <strong>RFP / bid → evaluation → negotiation → master agreement</strong> (pricing methodology, SLAs, delivery cadence,
  credit terms such as Net 7 / 15 / 30 or COD). <strong>Purchase orders</strong> then drive individual deliveries;
  <strong>invoices</strong> reconcile to those POs; <strong>payment</strong> follows the agreed terms after delivery —
  not as a single upfront payment for an entire multi-month agreement. This message is a pricing RFP for
  <strong>the current cycle only</strong>; any award leads to a separate PO / fulfillment step under those eventual terms.
</p>
"""

    return f"""
<html><body style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;line-height:1.55;max-width:720px;">
<h1 style="font-size:20px;margin:0 0 4px 0;">Request for Proposal (RFP)</h1>
<p style="margin:0;font-size:15px;color:#475569;font-weight:600;">Invitation to bid — weekly ingredient basket · {safe_dist}</p>

<h3 style="font-size:13px;text-transform:uppercase;letter-spacing:0.04em;color:#64748b;margin-top:1.5em;">1. Introduction &amp; background</h3>
<p>
  Our restaurant is sourcing distributors for a defined weekly ingredient requirement through HeavenlySourcing.
  This RFP is a <strong>formal request for written pricing</strong> so we can compare proposals on a consistent basis.
  You are invited because your firm appears capable of supplying the categories below.
</p>

<h3 style="font-size:13px;text-transform:uppercase;letter-spacing:0.04em;color:#64748b;margin-top:1.25em;">2. Scope &amp; objectives</h3>
<p>
  <strong>Objective:</strong> obtain firm, line-item pricing for the basket in the schedule, confirm feasibility of the
  requested delivery windows, and surface any MOQs, split-case charges, or substitutions we should plan for.
  This RFP does <strong>not</strong> constitute a purchase order or binding commitment to buy; it supports evaluation only.
</p>

<table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:13px;margin-top:0.75em;">
  <thead style="background:#f8fafc;">
    <tr>
      <th align="left">Ingredient</th>
      <th align="left">Recipe need<br/><span style="font-weight:normal;color:#64748b;">(kitchen use)</span></th>
      <th align="left">Order this<br/><span style="font-weight:normal;color:#64748b;">(quote basis)</span></th>
      <th align="left">Delivery window<br/><span style="font-weight:normal;color:#64748b;">(requested)</span></th>
      <th align="left">Reference benchmark<br/><span style="font-weight:normal;color:#64748b;">(context only)</span></th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>
<p style="font-size:12px;color:#64748b;margin-top:0.5em;">
  The &ldquo;Reference benchmark&rdquo; column reflects directional market context for our kitchen &mdash;
  <strong>not</strong> an instruction to match a price. Please quote your real wholesale economics.
</p>

{delivery_block}
{pack_note}

<h3 style="font-size:13px;text-transform:uppercase;letter-spacing:0.04em;color:#64748b;margin-top:1.25em;">3. Budget &amp; timeline</h3>
<ul style="margin-top:0.35em;">
  <li><strong>Cycle:</strong> weekly procurement comparison; we intend to finalize awards promptly after all replies are in.</li>
  <li><strong>Response:</strong> please send your proposal within <strong>three business days</strong> where possible. If you need more time, reply with a proposed date.</li>
  <li><strong>Budget posture:</strong> we evaluate proposals on <strong>landed economics</strong> (price, pack fit, reliability of supply), not headline price alone.</li>
</ul>

<h3 style="font-size:13px;text-transform:uppercase;letter-spacing:0.04em;color:#64748b;margin-top:1.25em;">4. Submission guidelines</h3>
<ul style="margin-top:0.35em;">
  <li><strong>Channel:</strong> reply to this email (same thread).</li>
  <li><strong>Format:</strong> line-by-line prices keyed to each ingredient (unit or per-pack, as quoted).</li>
  <li><strong>Required:</strong> include the reference block below verbatim so we can attach your reply to this RFP.</li>
  <li>Flag any items you cannot supply, proposed substitutes, and delivery constraints explicitly.</li>
</ul>

<h3 style="font-size:13px;text-transform:uppercase;letter-spacing:0.04em;color:#64748b;margin-top:1.25em;">5. Evaluation criteria</h3>
<ul style="margin-top:0.35em;">
  <li><strong>Total landed cost</strong> for comparable coverage of the basket.</li>
  <li><strong>Service &amp; delivery fit</strong> vs. the windows and split-drop needs above.</li>
  <li><strong>Pack alignment</strong> and clarity of quotation (reduces invoice / receipt discrepancies later).</li>
  <li><strong>Commercial terms:</strong> willingness to discuss standard foodservice arrangements &mdash; e.g. stated payment terms
      (Net 7 / 15 / 30, statement billing, COD for new accounts). Final credit and billing mechanics are confirmed before recurring POs.</li>
</ul>

{lifecycle_note}

<p style="margin-top:1.5em;padding:12px;background:#f1f5f9;border-radius:6px;font-size:13px;">
  <strong>Reference (required in reply):</strong><br/>
  Cycle {esc(cycle_id)} / Quote {esc(quote_id)}
</p>

<p style="margin-top:1.25em;">Respectfully,<br/><strong>HeavenlySourcing</strong> — Procurement desk<br/>
<span style="color:#64748b;font-size:13px;">Acting on behalf of our restaurant operator</span></p>
</body></html>
"""


def _build_followup_html(distributor_name: str, cycle_id: str, quote_id: str) -> str:
    dn = html_std.escape(distributor_name or "", quote=False)
    cid = html_std.escape(str(cycle_id or ""), quote=False)
    qid = html_std.escape(str(quote_id or ""), quote=False)
    return f"""
<html><body style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#0f172a;line-height:1.55;max-width:560px;">
<p>Dear {dn} team,</p>
<p>
  This is a courtesy follow-up regarding our <strong>Request for Proposal (RFP)</strong> for the current weekly
  ingredient basket. We have not yet received your written pricing response and would appreciate your reply so we can
  complete our comparative evaluation on schedule.
</p>
<p>
  Please respond in line-item form as described in the original RFP, and retain the reference identifier below.
</p>
<p style="padding:10px;background:#f1f5f9;border-radius:6px;font-size:13px;">
  <strong>Reference:</strong> Cycle {cid} / Quote {qid}
</p>
<p>Thank you,<br/><strong>HeavenlySourcing</strong> — Procurement desk</p>
</body></html>
"""


def _build_match_request_html(
    distributor_name: str,
    winning_items: List[Dict[str, Any]],
    losing_items: List[Dict[str, Any]],
    cycle_id: str,
    quote_id: str,
) -> str:
    """Bargaining email: tell the vendor what we'd buy from them today,
    where we have a better deal elsewhere, and offer them a chance to win
    the consolidated order via per-item price match OR overall discount.
    """
    winning_total = sum(i["your_price"] for i in winning_items)
    your_losing_total = sum(i["your_price"] for i in losing_items)
    target_losing_total = sum(i["target_price"] for i in losing_items)
    consolidated_if_match = winning_total + target_losing_total
    split_plan_total = winning_total + target_losing_total  # same math, but framed as the split outcome

    winning_block = ""
    if winning_items:
        winning_rows = "".join(
            f"<tr><td>{i['ingredient_name']}</td><td>${i['your_price']:.2f}</td></tr>"
            for i in winning_items
        )
        winning_block = f"""
<p><strong>Items where you already have the best price</strong> — we'd love to order these from you:</p>
<table border="1" cellpadding="6" cellspacing="0">
  <thead><tr><th>Ingredient</th><th>Your Price</th></tr></thead>
  <tbody>{winning_rows}</tbody>
</table>
<p style="font-size: 12px; color: #555;">Subtotal we plan to buy from you today: <strong>${winning_total:.2f}</strong></p>
"""

    losing_rows = "".join(
        f"<tr><td>{i['ingredient_name']}</td>"
        f"<td>${i['your_price']:.2f}</td>"
        f"<td><strong>${i['target_price']:.2f}</strong></td></tr>"
        for i in losing_items
    )

    return f"""
<html><body>
<p>Hi {distributor_name} team,</p>

<p>Thanks for your quote — we've now received pricing from every vendor we
contacted and are putting together our final order. Wanted to come to you
first before we split it across vendors.</p>

{winning_block}

<p><strong>Items where another vendor came in lower:</strong></p>
<table border="1" cellpadding="6" cellspacing="0">
  <thead><tr><th>Ingredient</th><th>Your Price</th><th>Best Competing Price</th></tr></thead>
  <tbody>{losing_rows}</tbody>
</table>
<p style="font-size: 12px; color: #555;">
  Your total for those items: ${your_losing_total:.2f} — best competing total: <strong>${target_losing_total:.2f}</strong>
</p>

<p>Honestly, we'd much rather consolidate this whole order with one vendor —
fewer deliveries, easier reconciliation, faster reorders. <strong>If you can
work with us on price, we'll happily send you the entire order.</strong></p>

<p>Two ways to win the full basket:</p>
<ol>
  <li><strong>Match the competing prices above</strong> — reply with new per-unit prices for the items where you're higher. Match all of them and we'll send you the full PO.</li>
  <li><strong>Offer an overall discount</strong> on the consolidated basket. Our split-vendor plan currently lands at <strong>${split_plan_total:.2f}</strong>. Beat that with a flat % off your full quote and we'll consolidate with you.</li>
</ol>

<p>Reply format: list each ingredient with your new per-unit price
(e.g. "Cream Cheese: $4.00"), or just say "X% off everything" if going the
discount route. If you can't match a specific item, just say so and we'll
order that one elsewhere.</p>

<p><strong>Reference: Cycle {cycle_id} / Quote {quote_id}</strong></p>

<p>Thank you,<br>HeavenlySourcing Procurement</p>
</body></html>
"""


def _build_po_html(distributor_name: str, po_payload: Dict[str, Any]) -> str:
    dn = html_std.escape(distributor_name or "", quote=False)
    rows = "".join(
        f"<tr><td>{html_std.escape(str(item['ingredient']), quote=False)}</td>"
        f"<td>${(item['unit_price'] if item['unit_price'] is not None else 'N/A')}</td></tr>"
        for item in po_payload.get("items", [])
    )
    cycle_id = html_std.escape(str(po_payload.get("cycle_id", "")), quote=False)
    po_id = html_std.escape(str(po_payload.get("po_id", "")), quote=False)
    return f"""
<html><body style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#0f172a;line-height:1.5;max-width:560px;">
<h2 style="font-size:18px;">Purchase order — {dn}</h2>
<p>
  Please fulfill the following line items for procurement cycle <strong>{cycle_id}</strong>.
  This PO is issued under our routine <strong>order → delivery → invoice</strong> cadence; payment will follow the credit terms
  agreed in our supplier agreement (e.g. Net 7 / 15 / 30 or as otherwise stated on your invoice).
</p>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:13px;">
  <thead style="background:#f8fafc;"><tr><th align="left">Ingredient</th><th align="left">Unit price</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
<p><strong>Total: ${po_payload.get('total') or 'TBD'}</strong></p>
<p>
  Upon delivery, please provide documentation that ties to this PO. We will reconcile quantity and price against the PO before processing payment.
  If you reply by email with an invoice or PO confirmation, we can ingest it automatically and attach it to this cycle for our records.
</p>
<p><strong>Reference: Cycle {cycle_id} / PO {po_id}</strong></p>
<p>Thank you,<br/><strong>HeavenlySourcing</strong> — Procurement</p>
</body></html>
"""


# ─── SMTP (sync) ──────────────────────────────────────────────────────────────

def _send_email(to_email: str, subject: str, html_body: str) -> None:
    if not settings.smtp_user or not settings.smtp_password:
        print(f"[email] SMTP not configured; would send to {to_email}: {subject}")
        return
    if not to_email:
        print(f"[email] missing to_email; skipping subject={subject}")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_user
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(settings.smtp_server, settings.smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
        print(f"[email] sent → {to_email}: {subject}")
    except Exception as exc:
        print(f"[email] send failed to {to_email}: {exc}")


def send_html_email(to_email: str, subject: str, html_body: str) -> None:
    """Public wrapper for HTML outbound mail (contract lifecycle, etc.)."""
    _send_email(to_email, subject, html_body)


def send_rfp_email(
    to_email: str,
    distributor_name: str,
    ingredient_list: List[Dict[str, Any]],
    cycle_id: str,
    quote_id: str,
    benchmarks: Optional[List[Dict[str, Any]]] = None,
) -> None:
    html = _build_rfp_html(
        distributor_name, ingredient_list, cycle_id, quote_id, benchmarks
    )
    _send_email(
        to_email,
        f"RFP: Weekly ingredient bid invitation — {distributor_name}",
        html,
    )


def send_followup_email(
    to_email: str,
    distributor_name: str,
    cycle_id: str,
    quote_id: str,
) -> None:
    html = _build_followup_html(distributor_name, cycle_id, quote_id)
    _send_email(
        to_email,
        f"Follow-up: RFP response requested — {distributor_name}",
        html,
    )


def send_match_request_email(
    to_email: str,
    distributor_name: str,
    winning_items: List[Dict[str, Any]],
    losing_items: List[Dict[str, Any]],
    cycle_id: str,
    quote_id: str,
) -> None:
    """Send a bargaining email.

    winning_items: [{ingredient_name, your_price}] — items where this vendor
                   already has the best price; we plan to buy these from them.
    losing_items:  [{ingredient_name, your_price, target_price}] — items where
                   another vendor came in lower; we want them to match.
    """
    html = _build_match_request_html(
        distributor_name, winning_items, losing_items, cycle_id, quote_id
    )
    _send_email(
        to_email,
        f"Final Order — Can You Win the Whole Basket? ({distributor_name})",
        html,
    )


def send_po_email(
    to_email: str,
    distributor_name: str,
    po_payload: Dict[str, Any],
) -> None:
    html = _build_po_html(distributor_name, po_payload)
    _send_email(to_email, f"Purchase Order Confirmed — {distributor_name}", html)


def _build_receipt_request_html(
    distributor_name: str,
    cycle_id: str,
    po_id: str,
    po_total: Optional[float],
    days_since_po: Optional[int],
) -> str:
    """Polite chase-up asking the vendor to forward the invoice/receipt for
    a purchase order we already placed."""
    elapsed_phrase = ""
    if days_since_po is not None:
        if days_since_po <= 0:
            elapsed_phrase = "earlier today"
        elif days_since_po == 1:
            elapsed_phrase = "yesterday"
        else:
            elapsed_phrase = f"{days_since_po} days ago"
    elapsed_block = (
        f"<p>We placed our purchase order with you {elapsed_phrase}, "
        f"but haven't received the invoice / receipt yet.</p>"
        if elapsed_phrase
        else "<p>We placed our purchase order with you and haven't received the invoice / receipt yet.</p>"
    )
    total_block = (
        f"<p>For reference, our PO total was <strong>${po_total:.2f}</strong>.</p>"
        if po_total is not None
        else ""
    )
    return f"""
<html><body>
<p>Hi {distributor_name} team,</p>
{elapsed_block}
{total_block}
<p>Could you reply to this email with a copy of the invoice (or a forwarded delivery
confirmation) when you get a moment? Our books reconcile against it, so even a quick
PDF attachment is plenty.</p>
<p>If the order has already shipped, please confirm the delivery date so we can match
it against our receiving log.</p>
<p><strong>Reference: Cycle {cycle_id} / PO {po_id}</strong></p>
<p>Thank you,<br>HeavenlySourcing Procurement</p>
</body></html>
"""


def send_receipt_request_email(
    to_email: str,
    distributor_name: str,
    cycle_id: str,
    po_id: str,
    po_total: Optional[float] = None,
    days_since_po: Optional[int] = None,
) -> None:
    html = _build_receipt_request_html(
        distributor_name, cycle_id, po_id, po_total, days_since_po
    )
    _send_email(
        to_email,
        f"Invoice Request — {distributor_name}",
        html,
    )


# ─── LLM parsing ──────────────────────────────────────────────────────────────

def _strip_fence(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = s[3:]
        if s.lower().startswith("json"):
            s = s[4:]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _llm_extract(system_prompt: str, body: str) -> dict:
    response = _openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": body},
        ],
        temperature=0,
        max_tokens=1024,
        response_format={"type": "json_object"},
    )
    return json.loads(_strip_fence(response.choices[0].message.content or "{}"))


# ─── Email body extraction ────────────────────────────────────────────────────

def _decode_part(part) -> str:
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="ignore")
    except Exception:
        return ""


def _extract_text_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return _decode_part(part)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html_text = _decode_part(part)
                return re.sub(r"<[^>]+>", " ", html_text)
        return ""
    return _decode_part(msg)


# Patterns Gmail / Outlook / Apple Mail use to delimit the quoted original
# message when someone clicks Reply. We chop the body at the FIRST match so
# the LLM only ever sees the vendor's freshly-typed reply.
_REPLY_QUOTE_MARKERS = [
    re.compile(r"^On\s+.{1,200}\bwrote:\s*$", re.MULTILINE),         # Gmail
    re.compile(r"^From:\s+.{1,200}@", re.MULTILINE),                 # Outlook
    re.compile(r"^>\s?-+\s?Original Message\s?-+\s?$", re.MULTILINE),
    re.compile(r"^_{5,}\s*$", re.MULTILINE),                          # Outlook signature divider
    re.compile(r"^-{5,}\s*Forwarded message\s*-{5,}\s*$", re.MULTILINE),
    re.compile(r"\n>\s+", re.MULTILINE),                              # any blockquote
]


def _strip_quoted_history(body: str) -> str:
    """Truncate an email body at the first reply / forward delimiter.

    Avoids feeding our own quoted RFP (with its USDA "Reference Benchmark"
    column) to the LLM as if it were vendor-supplied prices.
    """
    if not body:
        return body
    earliest: Optional[int] = None
    for pat in _REPLY_QUOTE_MARKERS:
        m = pat.search(body)
        if m and (earliest is None or m.start() < earliest):
            earliest = m.start()
    if earliest is None or earliest < 50:
        # Either no quote markers, or the quote starts at the very top
        # (rare; means there's no real new content). Return as-is.
        return body
    return body[:earliest].rstrip()


# Phrases vendors use when they decline an RFP without sending prices. The
# tuple is ordered roughly by specificity so the first hit wins (better than
# matching just "regret" when the body also says "out of stock").
_DECLINE_KEYWORDS: Tuple[str, ...] = (
    "out of stock",
    "out-of-stock",
    "currently unavailable",
    "supply chain",
    "cannot fulfill",
    "cannot fulfil",
    "unable to fulfill",
    "unable to fulfil",
    "unable to supply",
    "cannot supply",
    "cannot quote",
    "regret to inform",
    "we must decline",
    "have to decline",
    "have to pass",
    "respectfully decline",
    "no inventory",
    "not in stock",
    "won't be able",
    "will not be able",
    "no availability",
    "cannot accommodate",
)


def _detect_decline_signal(body: str) -> Tuple[str, Optional[str]]:
    """Return (excerpt, matched_keyword) if the body looks like a decline.

    The excerpt is a ~200-char window around the keyword so the user gets
    immediate context in the notification. matched_keyword is None when no
    decline phrase fired.
    """
    if not body:
        return "", None
    lower = body.lower()
    for keyword in _DECLINE_KEYWORDS:
        idx = lower.find(keyword)
        if idx == -1:
            continue
        start = max(0, idx - 80)
        end = min(len(body), idx + len(keyword) + 120)
        snippet = body[start:end].strip()
        snippet = " ".join(snippet.split())  # collapse whitespace
        if start > 0:
            snippet = "…" + snippet
        if end < len(body):
            snippet = snippet + "…"
        return snippet, keyword
    return "", None


# ─── Inbox processing (sync) ─────────────────────────────────────────────────

def _uuid_or_none(value: str):
    import uuid as _uuid
    try:
        return _uuid.UUID(value)
    except (ValueError, TypeError):
        return None


# In-memory dedupe so we don't email the same vendor twice in a short window
# for the same (quote_id, ingredient_id) match request. Survives until restart.
_MATCH_REQUESTS_SENT: set = set()


def _upsert_quote_item(session, quote_id, ingredient_id, price: Optional[float]) -> None:
    """Update an existing line for this (quote, ingredient) or create it."""
    from models import DistributorQuoteItem
    existing = session.exec(
        select(DistributorQuoteItem)
        .where(DistributorQuoteItem.distributor_quote_id == quote_id)
        .where(DistributorQuoteItem.ingredient_id == ingredient_id)
    ).first()
    if existing:
        existing.quoted_price_per_unit = price
        session.add(existing)
    else:
        session.add(DistributorQuoteItem(
            distributor_quote_id=quote_id,
            ingredient_id=ingredient_id,
            quoted_price_per_unit=price,
        ))


def _autotrigger_price_match(session, cycle_id) -> int:
    """Bargain with vendors who are losing on items where a peer came in lower.

    Gating rules (in order):
      1. Need at least 2 RECEIVED quotes (otherwise nothing to compare).
      2. Each vendor gets at most ONE bargain email per cycle (dedupe by
         ``(cycle_id, distributor_id)``). A single bargaining round per
         vendor is enough; their reply lowers prices in place.

    We deliberately do NOT wait for every vendor to reply before bargaining.
    In a 6-vendor RFP it's normal for 1-2 vendors to never respond, which
    would otherwise leave bargaining permanently disabled. Bargaining each
    time a new RECEIVED quote arrives is safe because (a) it only ever
    *lowers* a winner's price (the loser is being asked to match), and
    (b) the per-vendor dedupe means no one gets spammed.

    For each eligible vendor we compute:
      * winning_items — ingredients where this vendor is already the cheapest
                        among RECEIVED quotes (we WILL order these from them).
      * losing_items  — ingredients where another vendor came in lower
                        (we ask them to match the target price).

    Vendors with no losing items don't get a bargain email (no leverage).

    Returns the number of bargain emails dispatched.
    """
    from models import Distributor, DistributorQuote, DistributorQuoteItem, Ingredient

    all_quotes = session.exec(
        select(DistributorQuote)
        .where(DistributorQuote.procurement_cycle_id == cycle_id)
    ).all()

    received = [q for q in all_quotes if q.quote_status == "RECEIVED"]
    pending = [
        q for q in all_quotes
        if q.quote_status in ("PENDING", "FOLLOW_UP_SENT")
    ]

    if len(received) < 2:
        print(f"[bargain] skipping — only {len(received)} received quote(s); need ≥2")
        return 0
    if pending:
        print(
            f"[bargain] proceeding with {len(received)} RECEIVED quote(s); "
            f"{len(pending)} vendor(s) still pending — bargaining on what we have"
        )

    # Build (ingredient_id -> [(distributor_id, quote_id, price)])
    items_by_ing: Dict[Any, List[Any]] = {}
    quote_by_dist: Dict[Any, Any] = {q.distributor_id: q for q in received}
    for q in received:
        items = session.exec(
            select(DistributorQuoteItem)
            .where(DistributorQuoteItem.distributor_quote_id == q.id)
        ).all()
        for it in items:
            if it.quoted_price_per_unit is None:
                continue
            items_by_ing.setdefault(it.ingredient_id, []).append(
                (q.distributor_id, q.id, float(it.quoted_price_per_unit))
            )

    # Per-vendor winning + losing item lists
    winners_by_dist: Dict[Any, List[Dict[str, Any]]] = {}
    losers_by_dist: Dict[Any, List[Dict[str, Any]]] = {}
    for ing_id, offers in items_by_ing.items():
        offers.sort(key=lambda o: o[2])
        winner_did, _winner_qid, winner_price = offers[0]
        ing = session.get(Ingredient, ing_id)
        ing_name = ing.name if ing else str(ing_id)

        winners_by_dist.setdefault(winner_did, []).append({
            "ingredient_name": ing_name,
            "your_price": winner_price,
        })

        for loser_did, _loser_qid, loser_price in offers[1:]:
            if loser_price <= winner_price:
                continue
            losers_by_dist.setdefault(loser_did, []).append({
                "ingredient_name": ing_name,
                "your_price": loser_price,
                "target_price": winner_price,
            })

    sent = 0
    for dist_id, losing_items in losers_by_dist.items():
        # Per-vendor, per-cycle dedupe
        key = (str(cycle_id), str(dist_id))
        if key in _MATCH_REQUESTS_SENT:
            continue
        _MATCH_REQUESTS_SENT.add(key)

        q = quote_by_dist.get(dist_id)
        dist = session.get(Distributor, dist_id) if q else None
        if not dist or not dist.demo_routing_email:
            continue
        winning_items = winners_by_dist.get(dist_id, [])

        try:
            send_match_request_email(
                to_email=dist.demo_routing_email,
                distributor_name=dist.name,
                winning_items=winning_items,
                losing_items=losing_items,
                cycle_id=str(cycle_id),
                quote_id=str(q.id),
            )
            sent += 1
        except Exception as exc:
            print(f"[imap] match-request to {dist.name} failed: {exc}")

    return sent


def _process_quote_reply(session, quote_id: str, body: str, dist) -> None:
    """Handle both initial RFP replies AND price-match responses (same shape).

    Strategy:
      1. LLM extracts items + prices.
      2. Upsert each item against this quote (price-match replies update
         existing rows in place; initial replies create them).
      3. Recompute the quote's total + status.
      4. Run multi-vendor scoring + auto-trigger price-match outreach for
         any ingredient where this vendor (or a peer) is strictly more
         expensive than another received quote.
    """
    from models import (
        DistributorQuote, DistributorQuoteItem, Ingredient, Notification,
    )

    qid = _uuid_or_none(quote_id)
    if not qid:
        return
    quote = session.get(DistributorQuote, qid)
    if not quote:
        return

    fresh_body = _strip_quoted_history(body)
    if fresh_body != body:
        print(
            f"[imap] stripped {len(body) - len(fresh_body)} chars of quoted "
            "history before LLM extraction"
        )

    try:
        parsed = _llm_extract(QUOTE_PARSE_SYSTEM, fresh_body)
    except Exception as exc:
        print(f"[imap] quote LLM parse failed: {exc}")
        return

    extracted_items = parsed.get("items", []) or []
    if not extracted_items:
        # No prices parsed — but this could be a deliberate decline
        # ("out of stock", "regret", "cannot fulfill"). Don't leave the
        # user blind: detect that intent and surface a notification, plus
        # mark the quote as DECLINED so it doesn't sit in PENDING forever
        # and so the optimal-cart logic stops counting it as outstanding.
        decline_excerpt, decline_keyword = _detect_decline_signal(fresh_body)
        vendor_label = dist.name if dist else "vendor"
        if decline_keyword:
            quote.quote_status = "DECLINED"
            quote.received_at = datetime.utcnow()
            session.add(quote)
            session.add(Notification(
                title=f"{vendor_label} declined the RFP",
                message=(
                    f"Reason hint: \"{decline_keyword}\". "
                    f"Excerpt: {decline_excerpt}"
                ),
            ))
            session.commit()
            print(
                f"[imap] {vendor_label} reply matched decline keyword "
                f"{decline_keyword!r}; marked quote {quote_id} as DECLINED"
            )
        else:
            # Genuinely empty / unparseable reply — nudge the user to look.
            session.add(Notification(
                title=f"{vendor_label} replied — no prices found",
                message=(
                    "We received a reply but couldn't extract any prices. "
                    "Open your inbox to check whether they need clarification."
                ),
            ))
            session.commit()
            print(
                f"[imap] LLM returned 0 items for quote {quote_id} and no "
                "decline keywords matched — notifying user."
            )
        return

    for item_data in extracted_items:
        ing_name = (item_data.get("ingredient_name") or "").strip()
        if not ing_name:
            continue
        ing = session.exec(select(Ingredient).where(Ingredient.name == ing_name)).first()
        if not ing:
            ing = Ingredient(name=ing_name)
            session.add(ing)
            session.flush()
        price = item_data.get("price_per_unit")
        _upsert_quote_item(session, quote.id, ing.id, price)

    # Recompute total from the now-current set of line items
    session.flush()
    line_items = session.exec(
        select(DistributorQuoteItem)
        .where(DistributorQuoteItem.distributor_quote_id == quote.id)
    ).all()
    total = sum((li.quoted_price_per_unit or 0) for li in line_items)

    quote.quote_status = "RECEIVED"
    quote.total_quoted_price = round(float(total), 2)
    quote.received_at = datetime.utcnow()
    session.add(quote)

    # Gaps mean the vendor replied but couldn't supply specific items
    # (e.g. "Tomato — not available"). DO NOT auto-fire the chase-up email
    # in that case: send_followup_email's body says "we have not yet
    # received your pricing reply", which is plainly wrong for a vendor
    # who DID reply — they just can't carry one SKU. The gap is already
    # surfaced in the comparison matrix and the notification below, so
    # the operator can decide whether to ping them or just source it
    # elsewhere. Bargaining (a different email) still fires below.
    gaps = parsed.get("gaps") or []
    if gaps and dist:
        notif_msg = (
            f"Quote from {dist.name} received. Missing: {', '.join(gaps)}. "
            "No auto-email sent — review and source elsewhere if needed."
        )
    else:
        notif_msg = f"Quote received from {dist.name if dist else 'vendor'}. Total: ${total:.2f}."

    session.commit()

    # Auto-trigger price-match outreach (committed in its own pass so the
    # in-memory dedupe is consistent even if we crash in the middle).
    try:
        n_matches = _autotrigger_price_match(session, quote.procurement_cycle_id)
    except Exception as exc:
        n_matches = 0
        print(f"[imap] auto price-match failed: {exc}")

    # Refresh per-quote score using win-rate from the optimal cart
    try:
        from agents.scoring_engine import build_optimal_cart, generate_recommendation, score_quotes
        cart_items: List[Dict[str, Any]] = []
        cycle_quotes = session.exec(
            select(DistributorQuote)
            .where(DistributorQuote.procurement_cycle_id == quote.procurement_cycle_id)
            .where(DistributorQuote.quote_status == "RECEIVED")
        ).all()
        for q in cycle_quotes:
            from models import Distributor as _Dist
            d = session.get(_Dist, q.distributor_id)
            for li in session.exec(
                select(DistributorQuoteItem)
                .where(DistributorQuoteItem.distributor_quote_id == q.id)
            ).all():
                if li.quoted_price_per_unit is None:
                    continue
                ing = session.get(Ingredient, li.ingredient_id)
                cart_items.append({
                    "distributor_id": str(q.distributor_id),
                    "distributor_name": d.name if d else "",
                    "ingredient_id": str(li.ingredient_id),
                    "ingredient_name": ing.name if ing else "",
                    "unit_price": float(li.quoted_price_per_unit),
                })
        cart = build_optimal_cart(cart_items)
        per_vendor = cart["by_vendor"]
        # Update each received quote with win_rate-derived score + recommendation
        rec_text = generate_recommendation(cart, auto_match_sent=n_matches) if cart["by_ingredient"] else ""
        for q in cycle_quotes:
            v = per_vendor.get(str(q.distributor_id))
            if not v or v["items_quoted"] == 0:
                continue
            scored = score_quotes([{
                "win_rate": v["items_won"] / max(v["items_quoted"], 1),
                "handles_split_drop": False,
            }])
            if scored:
                q.score = scored[0]["score"]
                q.recommendation_text = rec_text
                session.add(q)
        session.commit()
    except Exception as exc:
        print(f"[imap] scoring failed: {exc}")

    session.add(Notification(title="Quote Received", message=notif_msg))
    if n_matches:
        session.add(Notification(
            title="Price-Match Requested",
            message=f"Auto-emailed {n_matches} vendor(s) to beat lower prices.",
        ))
    session.commit()
    print(f"[imap] processed quote reply for {dist.name if dist else 'vendor'} (quote_id={quote_id}) match_emails={n_matches}")


def _avg_mid_from_contract_parsed(parsed: Dict[str, Any]) -> Optional[float]:
    mids: List[float] = []
    for item in parsed.get("pricing_items") or []:
        if not isinstance(item, dict):
            continue
        pn = item.get("price_numeric")
        if pn is not None:
            try:
                mids.append(float(pn))
            except (TypeError, ValueError):
                pass
            continue
        lo = item.get("price_low")
        hi = item.get("price_high")
        if lo is not None and hi is not None:
            try:
                mids.append((float(lo) + float(hi)) / 2.0)
            except (TypeError, ValueError):
                pass
    if not mids:
        return None
    return sum(mids) / len(mids)


def _process_contract_negotiation_reply(
    session,
    contract_id_str: str,
    negotiation_id_str: str,
    subject: str,
    body: str,
) -> None:
    from models import Negotiation, NegotiationRound, Notification, Vendor

    cid = _uuid_or_none(contract_id_str)
    nid = _uuid_or_none(negotiation_id_str)
    if not cid or not nid:
        return

    neg = session.get(Negotiation, nid)
    if not neg or neg.contract_id != cid:
        print(
            f"[imap] contract negotiation ref mismatch contract_id={cid} negotiation_id={nid}"
        )
        return

    fresh_body = _strip_quoted_history(body)
    if fresh_body != body:
        print(
            f"[imap] stripped {len(body) - len(fresh_body)} chars of quoted "
            "history before contract reply LLM extraction"
        )

    try:
        parsed = _llm_extract(CONTRACT_REPLY_PARSE_SYSTEM, fresh_body)
    except Exception as exc:
        print(f"[imap] contract reply LLM parse failed: {exc}")
        return

    avg_mid = _avg_mid_from_contract_parsed(parsed)

    prev_idxs = session.exec(
        select(NegotiationRound.round_index).where(
            NegotiationRound.negotiation_id == neg.id
        )
    ).all()
    next_idx = max(prev_idxs, default=-1) + 1

    firm = bool(parsed.get("firm_commitment_detected"))

    offer_snapshot: Dict[str, Any] = {
        "parsed": parsed,
        "avg_quote_midpoint": avg_mid,
        "firm_commitment_detected": firm,
    }

    session.add(
        NegotiationRound(
            negotiation_id=neg.id,
            round_index=next_idx,
            direction="INBOUND",
            status="RECEIVED",
            subject=subject[:512] if subject else None,
            body=fresh_body[:8000] if fresh_body else None,
            offer_snapshot=offer_snapshot,
            received_at=datetime.utcnow(),
        )
    )

    vendor = session.get(Vendor, neg.vendor_id)
    vlabel = vendor.name if vendor else "vendor"
    summary = (parsed.get("summary") or "").strip()
    parts: List[str] = []
    if summary:
        parts.append(summary[:400])
    if avg_mid is not None:
        parts.append(f"Blended midpoint ~${avg_mid:.2f}")
    if firm:
        parts.append("Possible firm commitment flagged — review before auto-follow-up.")
    message = " — ".join(parts) if parts else "Parsed contract negotiation reply."

    session.add(
        Notification(
            title=f"Contract reply from {vlabel}",
            message=message,
        )
    )
    session.commit()

    try:
        from agents.contract_lifecycle import maybe_send_counter_offers

        maybe_send_counter_offers(session, cid)
    except Exception as exc:
        print(f"[imap] counter-offer trigger failed: {exc}")


def _process_receipt_reply(
    session,
    cycle_id: str,
    po_id: str,
    subject: str,
    body: str,
    dist,
) -> None:
    from models import (
        DistributorQuote, ProcurementCycle, PurchaseReceipt, Notification,
    )

    cid = _uuid_or_none(cycle_id)
    qid = _uuid_or_none(po_id)
    if not cid or not qid:
        return

    cycle = session.get(ProcurementCycle, cid)
    quote = session.get(DistributorQuote, qid)
    if not cycle or not quote:
        return

    try:
        parsed = _llm_extract(RECEIPT_PARSE_SYSTEM, body)
    except Exception as exc:
        print(f"[imap] receipt LLM parse failed: {exc}")
        parsed = {}

    receipt = PurchaseReceipt(
        procurement_cycle_id=cycle.id,
        distributor_quote_id=quote.id,
        distributor_id=quote.distributor_id,
        receipt_number=(parsed.get("receipt_number") or None),
        total_amount=parsed.get("total_amount"),
        line_items=parsed.get("items") or None,
        raw_email_subject=subject[:255],
        raw_email_excerpt=body[:1000],
    )
    session.add(receipt)
    session.flush()  # so the receipt counts in the "do all POs have invoices?" query below

    # Only flip the cycle to COMPLETED once EVERY APPROVED PO on this cycle
    # has at least one PurchaseReceipt. Multi-vendor split orders generate
    # several POs; receiving one invoice doesn't mean we're done. We compare
    # the set of approved distributor_ids against the set of distributors
    # that have a receipt — if there's any approved vendor without a
    # receipt yet, the cycle stays AWAITING_RECEIPT.
    approved_dist_ids = {
        q.distributor_id for q in session.exec(
            select(DistributorQuote)
            .where(DistributorQuote.procurement_cycle_id == cycle.id)
            .where(DistributorQuote.quote_status == "APPROVED")
        ).all()
    }
    receipted_dist_ids = {
        r.distributor_id for r in session.exec(
            select(PurchaseReceipt)
            .where(PurchaseReceipt.procurement_cycle_id == cycle.id)
        ).all()
    }
    if approved_dist_ids and approved_dist_ids.issubset(receipted_dist_ids):
        cycle.status = "COMPLETED"
        session.add(cycle)
        cycle_done = True
    else:
        cycle_done = False
        outstanding = approved_dist_ids - receipted_dist_ids
        print(
            f"[imap] cycle {str(cycle.id)[:8]} still AWAITING_RECEIPT — "
            f"{len(outstanding)}/{len(approved_dist_ids)} vendor(s) owe an invoice"
        )

    vendor_label = dist.name if dist else "vendor"
    notif_msg = (
        f"Receipt received from {vendor_label} for cycle {str(cycle.id)[:8]}…"
        + (f" total ${receipt.total_amount:.2f}" if receipt.total_amount is not None else "")
    )
    if not cycle_done and approved_dist_ids:
        outstanding_count = len(approved_dist_ids - receipted_dist_ids)
        notif_msg += (
            f" — {outstanding_count} more invoice"
            f"{'s' if outstanding_count != 1 else ''} pending."
        )
    session.add(Notification(title="Receipt Received", message=notif_msg))
    session.commit()
    print(f"[imap] processed receipt for {vendor_label} (po_id={po_id}) cycle_done={cycle_done}")


# ─── IMAP polling (sync) ─────────────────────────────────────────────────────

def _fetch_message_bytes(client: imaplib.IMAP4, msg_num: bytes) -> Optional[bytes]:
    """Robustly extract the raw RFC822 bytes from an IMAP fetch response.

    imaplib.fetch returns ``(status, [data])`` where data items can be either
    bytes (status text / closing parens) or 2-tuples ``(envelope, payload)``.
    The payload is what we want.
    """
    try:
        status, data = client.fetch(msg_num, "(RFC822)")
    except Exception as exc:
        print(f"[imap] fetch {msg_num!r} failed: {exc}")
        return None
    if status != "OK" or not data:
        return None
    for entry in data:
        if isinstance(entry, tuple) and len(entry) >= 2 and isinstance(entry[1], (bytes, bytearray)):
            return bytes(entry[1])
    return None


def _poll_imap_once() -> None:
    if not settings.imap_user or not settings.imap_password:
        return

    from database import engine
    from sqlmodel import Session as DBSession
    from models import Distributor, DistributorQuote

    client: Optional[imaplib.IMAP4_SSL] = None
    try:
        client = imaplib.IMAP4_SSL(settings.imap_server, 993, timeout=30)
        client.login(settings.imap_user, settings.imap_password)
        client.select("INBOX")

        status, search_data = client.search(None, "UNSEEN")
        if status != "OK" or not search_data or not search_data[0]:
            return

        msg_nums = search_data[0].split()
        if not msg_nums:
            return

        for msg_num in msg_nums:
            raw = _fetch_message_bytes(client, msg_num)
            if not raw:
                continue
            try:
                msg = email_lib.message_from_bytes(raw)
            except Exception as exc:
                print(f"[imap] parse failed: {exc}")
                continue

            raw_subject = msg.get("Subject", "") or ""
            subject = _decode_subject(raw_subject)
            body = _extract_text_body(msg)

            # Self-loop guard.
            #
            # In the demo the operator sends RFPs to ani2nem+vendor@gmail.com
            # (which delivers back to ani2nem@gmail.com) AND replies to those
            # RFPs in Gmail acting as the vendor. Both messages have
            # From: ani2nem@gmail.com, so we cannot use From alone to decide.
            #
            # The differentiator is the Subject:
            #   * Our outbound:        "RFP: ..."         -> SKIP
            #   * Outbound loopback:   "RFP: ..."         -> SKIP (same)
            #   * User-as-vendor reply: "Re: RFP: ..."    -> PROCESS
            #   * Real vendor reply:    "Re: RFP: ..." or anything else -> PROCESS
            #
            # So we only skip when *both* From == self AND subject matches one
            # of our outbound prefixes. Everything else falls through to the
            # normal Reference: parsing.
            if _is_self_sent(msg) and _is_our_outbound_subject(subject):
                print(
                    f"[imap] skipping self-loop subject={subject!r} "
                    f"from={msg.get('From')!r}"
                )
                continue

            # Belt-and-suspenders: even if From is different (e.g. forwarded
            # by a Gmail filter), an exact outbound subject is still ours.
            # Anything with "Re:" or any other prefix is not.
            if _is_our_outbound_subject(subject) and not _is_self_sent(msg):
                # Almost never the case, but be explicit so we don't loop on
                # weird forwarders.
                print(f"[imap] skipping non-self outbound-shaped subject={subject!r}")
                continue

            contract_ref = _extract_contract_neg_ref(subject, body)
            if contract_ref:
                cid_str, nid_str = contract_ref
                print(
                    f"[imap] processing contract negotiation reply subject={subject!r} "
                    f"from={msg.get('From')!r} contract={cid_str} negotiation={nid_str}"
                )
                with DBSession(engine) as session:
                    _process_contract_negotiation_reply(
                        session, cid_str, nid_str, subject, body
                    )
                continue

            po_ref = _extract_po_ref(subject, body)
            quote_ref = _extract_quote_ref(subject, body)

            if not po_ref and not quote_ref:
                continue

            print(
                f"[imap] processing reply subject={subject!r} "
                f"from={msg.get('From')!r} "
                f"quote_ref={quote_ref} po_ref={po_ref}"
            )

            with DBSession(engine) as session:
                if po_ref and (_looks_like_receipt(subject, body) or not quote_ref):
                    cycle_id_str, po_id_str = po_ref
                    quote = session.get(DistributorQuote, _uuid_or_none(po_id_str))
                    dist = session.get(Distributor, quote.distributor_id) if quote else None
                    _process_receipt_reply(session, cycle_id_str, po_id_str, subject, body, dist)
                elif quote_ref:
                    cycle_id_str, quote_id_str = quote_ref
                    quote = session.get(DistributorQuote, _uuid_or_none(quote_id_str))
                    dist = session.get(Distributor, quote.distributor_id) if quote else None
                    _process_quote_reply(session, quote_id_str, body, dist)
    except Exception as exc:
        print(f"[imap] poll error: {exc}")
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


# ─── Scheduler lifecycle ──────────────────────────────────────────────────────


def _contract_lifecycle_tick_once() -> None:
    from database import engine
    from sqlmodel import Session as DBSession

    from agents.contract_lifecycle import daily_contract_lifecycle_tick

    try:
        with DBSession(engine) as session:
            summary = daily_contract_lifecycle_tick(session)
            started = summary.get("started_contract_ids") or []
            if started:
                print(f"[contract_lifecycle] started renewals for {len(started)} contract(s)")
    except Exception as exc:
        print(f"[contract_lifecycle] tick failed: {exc}")


def start_imap_scheduler() -> None:
    _scheduler.add_job(
        _poll_imap_once, "interval", seconds=60, id="imap_poll", replace_existing=True
    )
    _scheduler.add_job(
        _contract_lifecycle_tick_once,
        CronTrigger(hour=12, minute=0),
        id="contract_lifecycle_daily",
        replace_existing=True,
    )
    _scheduler.start()
    print(
        "[email_daemon] scheduler: IMAP poll every 60s; "
        "contract lifecycle daily at 12:00 (server local time)"
    )


def stop_imap_scheduler() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
