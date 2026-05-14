"""Phase 6 — outbound SMS via Twilio REST.

No-ops with a log line when credentials or destination are missing.
"""
from __future__ import annotations

import re

import requests

from config import settings


def normalize_sms_destination(raw: str) -> str:
    """Best-effort E.164 for US numbers; pass through if already +prefixed."""
    s = (raw or "").strip()
    if not s:
        return ""
    if s.startswith("+"):
        return s
    digits = re.sub(r"\D", "", s)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits if digits else ""


def send_sms(to_phone: str, body: str) -> bool:
    sid = (settings.twilio_account_sid or "").strip()
    token = (settings.twilio_auth_token or "").strip()
    from_num = (settings.twilio_from_number or "").strip()
    dest = normalize_sms_destination(to_phone)
    if not all([sid, token, from_num, dest]):
        print("[sms] Twilio not fully configured or missing phone — skipping send")
        return False
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    try:
        r = requests.post(
            url,
            auth=(sid, token),
            data={
                "From": from_num,
                "To": dest,
                "Body": (body or "")[:1500],
            },
            timeout=25,
        )
        if r.status_code >= 400:
            print(f"[sms] Twilio HTTP {r.status_code}: {r.text[:400]}")
            return False
        print(f"[sms] sent → {dest}")
        return True
    except Exception as exc:
        print(f"[sms] send failed: {exc}")
        return False
