import asyncio
import json
import email as email_lib
from datetime import datetime
from typing import List, Dict, Any

import aiosmtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from aioimaplib import aioimaplib
from apscheduler.schedulers.background import BackgroundScheduler
from openai import OpenAI
from sqlmodel import select

from config import settings

_scheduler = BackgroundScheduler()
_openai = OpenAI(api_key=settings.openai_api_key)

EMAIL_PARSE_SYSTEM = """\
Extract pricing from the vendor email. Return ONLY valid JSON — no markdown, no preamble.

Schema:
{
  "items": [{"ingredient_name": "string", "price_per_unit": float, "notes": "string"}],
  "gaps": ["missing_ingredient_1"],
  "substitutions": [{"original": "string", "substitute": "string", "reason": "string"}]
}
"""

# ─── RFP HTML template ────────────────────────────────────────────────────────

def _build_rfp_html(
    distributor_name: str,
    ingredient_list: List[Dict[str, Any]],
    preferred_window: str,
    cycle_id: str,
    quote_id: str,
) -> str:
    rows = "".join(
        f"<tr><td>{i['name']}</td><td>{i['qty']:.2f}</td><td>{i['unit']}</td>"
        f"<td>{'⚠ Split-drop (≤{} days shelf life)'.format(i['shelf_life_days']) if i['shelf_life_days'] <= 4 else 'Standard'}</td></tr>"
        for i in ingredient_list
    )
    return f"""
<html><body>
<h2>Request for Pricing — {distributor_name}</h2>
<p>We are requesting pricing for the following ingredients for our weekly procurement cycle.</p>
<p><strong>Preferred delivery window:</strong> {preferred_window}</p>
<table border="1" cellpadding="6" cellspacing="0">
  <thead><tr><th>Ingredient</th><th>Qty Needed</th><th>Unit</th><th>Delivery Note</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
<p>Please reply with your pricing per unit for each item.<br>
Reference: Cycle {cycle_id} / Quote {quote_id}</p>
<p>Thank you,<br>HeavenlySourcing Procurement</p>
</body></html>
"""


# ─── SMTP helpers ─────────────────────────────────────────────────────────────

async def _send_email(to_email: str, subject: str, html_body: str):
    if not settings.smtp_user or not settings.smtp_password:
        print(f"[email] SMTP not configured; would send to {to_email}: {subject}")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_user
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))

    await aiosmtplib.send(
        msg,
        hostname=settings.smtp_server,
        port=settings.smtp_port,
        username=settings.smtp_user,
        password=settings.smtp_password,
        start_tls=True,
    )


async def send_rfp_email(
    to_email: str,
    distributor_name: str,
    ingredient_list: List[Dict[str, Any]],
    preferred_window: str,
    cycle_id: str,
    quote_id: str,
):
    html = _build_rfp_html(distributor_name, ingredient_list, preferred_window, cycle_id, quote_id)
    await _send_email(to_email, f"RFP: Pricing Request — {distributor_name}", html)


async def send_followup_email(to_email: str, distributor_name: str, quote_id: str):
    html = f"""
<html><body>
<p>Hi {distributor_name} team,</p>
<p>We have not yet received your pricing response for quote <strong>{quote_id}</strong>.
Could you please send your pricing at your earliest convenience?</p>
<p>Thank you,<br>HeavenlySourcing Procurement</p>
</body></html>
"""
    await _send_email(to_email, f"Follow-up: Pricing Request — {distributor_name}", html)


async def send_po_email(to_email: str, distributor_name: str, po_payload: Dict[str, Any]):
    rows = "".join(
        f"<tr><td>{item['ingredient']}</td><td>${item['unit_price'] or 'N/A'}</td></tr>"
        for item in po_payload.get("items", [])
    )
    html = f"""
<html><body>
<h2>Purchase Order Confirmation — {distributor_name}</h2>
<p>We are pleased to confirm our purchase order for cycle {po_payload.get('cycle_id', '')}.</p>
<p><strong>Preferred delivery:</strong> {po_payload.get('preferred_delivery_window', '')}</p>
<table border="1" cellpadding="6" cellspacing="0">
  <thead><tr><th>Ingredient</th><th>Unit Price</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
<p><strong>Total: ${po_payload.get('total') or 'TBD'}</strong></p>
<p>Thank you,<br>HeavenlySourcing Procurement</p>
</body></html>
"""
    await _send_email(to_email, f"Purchase Order Confirmed — {distributor_name}", html)


# ─── IMAP poller ──────────────────────────────────────────────────────────────

def _parse_email_body_with_llm(body: str) -> dict:
    response = _openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": EMAIL_PARSE_SYSTEM},
            {"role": "user", "content": body},
        ],
        temperature=0,
        max_tokens=1024,
    )
    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


def _extract_cycle_quote_ids(subject: str, body: str):
    """Pull cycle_id and quote_id from email subject/body reference line."""
    import re
    cycle_id = quote_id = None
    m = re.search(r"Cycle\s+([\w-]+)\s*/\s*Quote\s+([\w-]+)", body + " " + subject)
    if m:
        cycle_id, quote_id = m.group(1), m.group(2)
    return cycle_id, quote_id


async def _poll_imap_once():
    if not settings.imap_user or not settings.imap_password:
        return

    from database import engine
    from sqlmodel import Session
    from models import DistributorQuote, DistributorQuoteItem, Ingredient, Notification, Distributor
    import uuid as _uuid

    try:
        client = aioimaplib.IMAP4_SSL(host=settings.imap_server, port=993)
        await client.wait_hello_from_server()
        await client.login(settings.imap_user, settings.imap_password)
        await client.select("INBOX")

        _, msg_ids_data = await client.search("UNSEEN")
        msg_ids = msg_ids_data[0].split() if msg_ids_data and msg_ids_data[0] else []

        for msg_id in msg_ids:
            _, msg_data = await client.fetch(msg_id, "(RFC822)")
            raw = msg_data[1]
            msg = email_lib.message_from_bytes(raw)

            subject = msg.get("Subject", "")
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                        break
            else:
                body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

            cycle_id_str, quote_id_str = _extract_cycle_quote_ids(subject, body)
            if not quote_id_str:
                continue

            try:
                parsed = _parse_email_body_with_llm(body)
            except Exception as e:
                print(f"[imap] LLM parse failed for msg {msg_id}: {e}")
                continue

            with Session(engine) as session:
                try:
                    quote_id = _uuid.UUID(quote_id_str)
                except ValueError:
                    continue

                quote = session.get(DistributorQuote, quote_id)
                if not quote:
                    continue

                dist = session.get(Distributor, quote.distributor_id)
                total = 0.0

                for item_data in parsed.get("items", []):
                    ing = session.exec(
                        select(Ingredient).where(Ingredient.name == item_data["ingredient_name"])
                    ).first()
                    if not ing:
                        ing = Ingredient(name=item_data["ingredient_name"])
                        session.add(ing)
                        session.flush()

                    qi = DistributorQuoteItem(
                        distributor_quote_id=quote.id,
                        ingredient_id=ing.id,
                        quoted_price_per_unit=item_data.get("price_per_unit"),
                    )
                    session.add(qi)
                    total += item_data.get("price_per_unit") or 0.0

                quote.quote_status = "RECEIVED"
                quote.total_quoted_price = total
                quote.received_at = datetime.utcnow()
                session.add(quote)

                # Gap detection → auto follow-up
                gaps = parsed.get("gaps", [])
                if gaps:
                    try:
                        asyncio.run(send_followup_email(
                            to_email=dist.demo_routing_email if dist else "",
                            distributor_name=dist.name if dist else "Vendor",
                            quote_id=quote_id_str,
                        ))
                    except Exception:
                        pass
                    notif_msg = f"Quote from {dist.name if dist else 'vendor'} received. Missing: {', '.join(gaps)}. Follow-up sent."
                else:
                    notif_msg = f"Quote received from {dist.name if dist else 'vendor'}. Total: ${total:.2f}."

                # Run scoring and generate recommendation
                from agents.scoring_engine import score_quotes, generate_recommendation
                quotes_for_scoring = [
                    {
                        "quote_id": str(quote.id),
                        "distributor_name": dist.name if dist else "",
                        "total_quoted_price": total,
                        "delivery_window": quote.procurement_cycle.preferred_delivery_window if hasattr(quote, 'procurement_cycle') else "",
                        "handles_split_drop": False,
                    }
                ]
                scored = score_quotes(quotes_for_scoring, preferred_window="Morning")
                if scored:
                    quote.score = scored[0]["score"]
                    rec_text = generate_recommendation(scored)
                    quote.recommendation_text = rec_text
                    session.add(quote)

                notif = Notification(title="Quote Received", message=notif_msg)
                session.add(notif)
                session.commit()

        await client.logout()
    except Exception as e:
        print(f"[imap] poll error: {e}")


def _poll_imap_sync():
    asyncio.run(_poll_imap_once())


# ─── Scheduler lifecycle ──────────────────────────────────────────────────────

def start_imap_scheduler():
    _scheduler.add_job(_poll_imap_sync, "interval", seconds=60, id="imap_poll")
    _scheduler.start()
    print("[email_daemon] IMAP scheduler started (60s interval)")


def stop_imap_scheduler():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
