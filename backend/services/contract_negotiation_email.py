"""Outbound HTML email builders for contract renewal / competitor RFP (Phase 3).

Tone guidelines baked into copy:

  * Exploratory, non-binding language except where presenting USDA AMS data as
    factual context.
  * Price discussions framed as **target ranges** or directional benchmarks —
    never a firm commitment without explicit manager approval.
  * Reference line ``Contract <uuid> / Negotiation <uuid>`` so IMAP ingestion
    can attach replies without scraping Gmail threads.
"""
from __future__ import annotations

import html as html_lib
from datetime import date
from typing import Any, Dict, List, Optional

from services.email_daemon import send_html_email


def _esc(s: str) -> str:
    return html_lib.escape(s or "", quote=True)


def send_contract_renewal_email(
    *,
    to_email: str,
    vendor_name: str,
    restaurant_name: str,
    contract_nickname: str,
    contract_id: str,
    negotiation_id: str,
    end_date: Optional[date],
    days_remaining: Optional[int],
    category_blob: str,
    line_items_html: str,
    ams_context_html: str,
    trust_footer_html: str = "",
) -> None:
    """Ask the incumbent for renewal pricing / updated terms."""
    end_part = ""
    if end_date:
        end_part = (
            f"<p><strong>Current agreement ends:</strong> {_esc(end_date.isoformat())}"
            f"{f' ({days_remaining} days from today)' if days_remaining is not None else ''}.</p>"
        )

    body = f"""
<html><body>
<p>Hi {_esc(vendor_name)} team,</p>

<p>We hope you're doing well. {_esc(restaurant_name)} is reviewing our long-term
supplier partnerships ahead of our upcoming renewal window for
<strong>{_esc(contract_nickname)}</strong> ({_esc(category_blob)}).</p>

{end_part}

<p>Could you please share <strong>renewal pricing</strong> and any updated
commercial terms you're comfortable proposing for the next agreement period?
We're evaluating several continuity factors — total landed cost, delivery cadence,
and flexibility — alongside directional USDA AMS wholesale benchmarks where they apply.</p>

<h3>Categories / scope reminder</h3>
<p>{_esc(category_blob)}</p>

<h3>Line items / formulas from our current agreement</h3>
{line_items_html}

<h3>USDA AMS market context (directional)</h3>
{ams_context_html}

{trust_footer_html}

<p>This outreach is exploratory and does not constitute a binding commitment on either side.</p>

<p><strong>Reference: Contract {_esc(contract_id)} / Negotiation {_esc(negotiation_id)}</strong></p>

<p>Thank you,<br>HeavenlySourcing — Procurement desk<br>{_esc(restaurant_name)}</p>
</body></html>
"""
    send_html_email(
        to_email,
        f"Renewal discussion — {contract_nickname} ({vendor_name})",
        body,
    )


def send_contract_competitor_rfp_email(
    *,
    to_email: str,
    vendor_name: str,
    restaurant_name: str,
    contract_nickname: str,
    contract_id: str,
    negotiation_id: str,
    category_blob: str,
    incumbent_benchmark_blob: str,
    line_items_html: str,
    ams_context_html: str,
) -> None:
    """Invite alternative distributors to bid — incumbent never named."""
    body = f"""
<html><body>
<p>Hi {_esc(vendor_name)} team,</p>

<p>{_esc(restaurant_name)} is issuing an exploratory <strong>request for proposal</strong>
for a multi-category restaurant supply agreement comparable to our existing footprint:</p>
<p><strong>{_esc(contract_nickname)}</strong> — {_esc(category_blob)}</p>

<p>We're benchmarking incumbent-equivalent economics plus directional USDA AMS wholesale trends.
Below is a <strong>non-binding illustrative benchmark summary</strong> from our current programme —
individual SKU specifics remain commercially sensitive; quote whatever wholesale packs make sense for your catalogue.</p>

<div style="background:#f8fafc;border:1px solid #e2e8f0;padding:12px;margin:12px 0;">
{incumbent_benchmark_blob}
</div>

<h3>Requested pricing inputs</h3>
{line_items_html}

<h3>USDA AMS directional reference</h3>
{ams_context_html}

<p>Please reply with per-unit or formula quotes plus MOQ, payment terms, and delivery cadence.
We're optimising for <strong>total landed cost first</strong> with flexibility as a close second.</p>

<p>This message is exploratory and does not obligate {_esc(restaurant_name)} to purchase.</p>

<p><strong>Reference: Contract {_esc(contract_id)} / Negotiation {_esc(negotiation_id)}</strong></p>

<p>Thank you,<br>HeavenlySourcing — Procurement desk<br>{_esc(restaurant_name)}</p>
</body></html>
"""
    send_html_email(
        to_email,
        f"Contract RFP — {contract_nickname} ({vendor_name})",
        body,
    )


def send_contract_counter_offer_email(
    *,
    to_email: str,
    vendor_name: str,
    restaurant_name: str,
    contract_nickname: str,
    contract_id: str,
    negotiation_id: str,
    intro_paragraph: str,
    benchmark_comparison_html: str,
) -> None:
    """Polite bargaining round — compares anonymised competing ranges."""
    body = f"""
<html><body>
<p>Hi {_esc(vendor_name)} team,</p>

<p>{_esc(intro_paragraph)}</p>

<h3>What we're seeing in this procurement cycle</h3>
{benchmark_comparison_html}

<p>We're still consolidating volumes wherever reliability aligns — nothing below assumes exclusivity.</p>

<p>This email explores economics only and does not constitute an obligation to award volume.</p>

<p><strong>Reference: Contract {_esc(contract_id)} / Negotiation {_esc(negotiation_id)}</strong></p>

<p>Thank you,<br>HeavenlySourcing — Procurement desk<br>{_esc(restaurant_name)}</p>
</body></html>
"""
    send_html_email(
        to_email,
        f"Contract discussion — {contract_nickname} ({vendor_name})",
        body,
    )


def render_contract_line_items_table(rows: List[Dict[str, Any]]) -> str:
    """Turn extracted ``ContractLineItem`` shapes into an HTML table."""
    if not rows:
        return (
            "<p><em>No itemised SKUs were extracted — pricing methodology-driven agreement.</em></p>"
        )

    header = "<tr><th>SKU / bundle</th><th>Pack</th><th>Last quoted formula</th></tr>"
    body_rows = []
    for r in rows:
        sku = _esc(str(r.get("sku_name", "")))
        pack = _esc(str(r.get("pack_description") or r.get("unit_of_measure") or "—"))
        formula = r.get("price_formula")
        fixed = r.get("fixed_price")
        price_cell = "—"
        if formula:
            price_cell = _esc(str(formula))
        elif fixed is not None:
            price_cell = f"${float(fixed):.2f}"
        body_rows.append(f"<tr><td>{sku}</td><td>{pack}</td><td>{price_cell}</td></tr>")

    return (
        "<table border='1' cellpadding='6' cellspacing='0'>"
        f"<thead>{header}</thead><tbody>{''.join(body_rows)}</tbody></table>"
    )


def render_ams_blocks(blocks: List[Dict[str, Any]]) -> str:
    """Render AMS summary snippets."""
    if not blocks:
        return "<p><em>No USDA AMS price coverage mapped for these SKUs yet — directional benchmarks omitted.</em></p>"
    parts: List[str] = []
    for b in blocks:
        nm = _esc(str(b.get("label", "")))
        trend = b.get("trend_one_line") or ""
        parts.append(f"<li><strong>{nm}</strong> — {_esc(trend)}</li>")
    return "<ul>" + "".join(parts) + "</ul>"
