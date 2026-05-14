"""Phase 3 — Contract lifecycle & negotiation orchestration.

Runs when an ACTIVE agreement crosses its renewal-notice window: pulls USDA AMS
directional context where mappings exist, emails the incumbent for renewal terms,
discovers up to ``MAX_CONTRACT_COMPETITORS`` alternative wholesalers via Places,
opens parallel ``Negotiation`` threads + persists outbound ``NegotiationRound``
records.

Counter-offer bargaining fires automatically once ≥ two respondents supply usable
numeric midpoint summaries via inbound reply parsing (``email_daemon``), capped per
negotiation by ``Negotiation.max_rounds`` excluding the round‑zero outreach blast.

Designed constraints:

  * No outbound negotiation fires unless ``manager_verified`` + ACTIVE contract.
  * ``renewal_cycle_started_at`` is idempotent — first scheduler/manual kick wins.
"""
from __future__ import annotations

import logging
import uuid as uuid_mod
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import Session, select

from models import (
    Contract,
    ContractLineItem,
    Ingredient,
    ManagerAlert,
    Negotiation,
    NegotiationRound,
    Notification,
    RestaurantProfile,
    Vendor,
    VendorRestaurantLink,
    VendorTrustScore,
)
from services.contract_competitor_discovery import discover_competitor_vendors
from services.contract_negotiation_email import (
    render_ams_blocks,
    render_contract_line_items_table,
    send_contract_competitor_rfp_email,
    send_contract_counter_offer_email,
    send_contract_renewal_email,
)
from services.places_discovery import build_demo_routing_email


log = logging.getLogger(__name__)


def _incumbent_trust_footer_html(
    session: Session, profile_id: uuid_mod.UUID, vendor_id: uuid_mod.UUID
) -> str:
    """Phase 4 — optional first-party trust line in incumbent renewal email."""
    t = session.exec(
        select(VendorTrustScore)
        .where(VendorTrustScore.vendor_id == vendor_id)
        .where(VendorTrustScore.restaurant_profile_id == profile_id)
    ).first()
    if not t or t.trust_score is None:
        return ""
    otr = t.on_time_rate
    otr_txt = f"{100 * otr:.0f}%" if isinstance(otr, (int, float)) else "n/a"
    return (
        "<p style=\"font-size:12px;color:#64748b\">"
        "First-party delivery trust from our receipts: "
        f"<strong>{float(t.trust_score):.0f}</strong>/100 "
        f"(on-time {otr_txt}, {t.deliveries_total} deliveries logged)."
        "</p>"
    )


def _html_esc(s: str) -> str:
    import html as _html

    return _html.escape(s or "", quote=True)


def _contact_email(session: Session, profile_id: uuid_mod.UUID, vendor: Vendor) -> str:
    link = session.exec(
        select(VendorRestaurantLink)
        .where(VendorRestaurantLink.vendor_id == vendor.id)
        .where(VendorRestaurantLink.restaurant_profile_id == profile_id)
    ).first()
    if link and link.contact_email:
        return link.contact_email.strip()

    profile = session.get(RestaurantProfile, profile_id)
    mail = profile.email if profile else "vendor@example.com"
    return build_demo_routing_email(mail, vendor.name)


def _categories_blob(contract: Contract) -> str:
    cov = contract.category_coverage or []
    if isinstance(cov, list) and cov:
        parts = [str(x) for x in cov]
    elif contract.primary_category:
        parts = [contract.primary_category]
    else:
        parts = ["Mixed categories"]
    return ", ".join(parts)


def _ams_snapshot_for_ingredient(session: Session, ing_id: uuid_mod.UUID) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return ``(block_dict_for_UI_email, trend_sentence)``."""
    try:
        from services.ams_pricing import fetch_and_store_prices_for_ingredient
        from services.ams_pricing import summarize_ingredient_prices, find_mapping_for
        ing = session.get(Ingredient, ing_id)
        if not ing:
            return None, None
        if not find_mapping_for(ing.name):
            return None, None
        fetch_and_store_prices_for_ingredient(session, ing)
        summary = summarize_ingredient_prices(session, ing_id)
        if not summary.get("has_data"):
            return None, None
        mids = [
            row["midpoint"]
            for row in (summary.get("series") or [])
            if row.get("midpoint") is not None
        ]
        if not mids:
            return None, None
        latest = mids[-1]
        baseline_vals = mids[:-1]
        unit = summary.get("unit") or "unit"
        label = summary.get("commodity_label") or ing.name
        if baseline_vals:
            baseline = sum(baseline_vals) / len(baseline_vals)
            if baseline > 0:
                pct = (latest - baseline) / baseline * 100
                direction = "trending up" if pct > 4 else "trending down" if pct < -4 else "roughly flat"
                sentence = (
                    f"{label}: latest approx ${latest:.2f}/{unit} vs trailing avg "
                    f"${baseline:.2f}/{unit} ({direction}, ~{pct:+.1f}% vs trailing)."
                )
            else:
                sentence = f"{label}: latest approx ${latest:.2f}/{unit}."
        else:
            sentence = f"{label}: latest observation approx ${latest:.2f}/{unit}."

        block = {"label": label, "trend_one_line": sentence}
        return block, sentence
    except Exception as exc:
        log.warning("[lifecycle] AMS fetch skipped for ingredient %s: %s", ing_id, exc)
        return None, None


def _resolve_line_item_ingredient(session: Session, row: ContractLineItem) -> Optional[Ingredient]:
    if row.ingredient_id:
        return session.get(Ingredient, row.ingredient_id)

    sku = (row.sku_name or "").strip()
    if len(sku) < 3:
        return None
    cand = session.exec(select(Ingredient).where(Ingredient.name == sku)).first()
    if cand:
        return cand

    prefix = sku.split(",")[0].strip().split("(")[0].strip()
    if prefix != sku:
        cand = session.exec(select(Ingredient).where(Ingredient.name == prefix)).first()
        if cand:
            return cand

    like_term = f"%{prefix[:32]}%"
    rows = session.exec(
        select(Ingredient).where(Ingredient.name.ilike(like_term)).limit(1)
    ).all()
    return rows[0] if rows else None


def build_ams_blocks(session: Session, contract_id: uuid_mod.UUID) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    rows = session.exec(
        select(ContractLineItem)
        .where(ContractLineItem.contract_id == contract_id)
        .limit(24)
    ).all()

    seen_ids: set = set()
    for row in rows:
        ing = _resolve_line_item_ingredient(session, row)
        if not ing or ing.id in seen_ids:
            continue
        seen_ids.add(ing.id)
        blk, _trend = _ams_snapshot_for_ingredient(session, ing.id)
        if blk:
            blocks.append(blk)
        if len(blocks) >= 6:
            break

    return blocks


def _incumbent_benchmark_bullet_lines(li_rows: List[Dict[str, Any]]) -> str:
    bullets: List[str] = []
    for r in li_rows[:10]:
        sku = _html_esc(str(r.get("sku_name", "")))
        fix = r.get("fixed_price")
        frm = r.get("price_formula")
        fragment_raw = ""
        if frm:
            fragment_raw = str(frm)
        elif fix is not None:
            fragment_raw = (
                f"approx ${float(fix):.2f} locked indicator where historical captures existed."
            )
        else:
            fragment_raw = "formula-driven"
        fragment = _html_esc(fragment_raw)
        bullets.append(f"<li><strong>{sku}</strong> — {fragment}</li>")
    if not bullets:
        return "<p><em>No SKU spreadsheet extracted — incumbent economics summarised orally.</em></p>"
    return "<ul>" + "".join(bullets) + "</ul>"


def start_renewal_cycle(
    session: Session,
    contract_id: uuid_mod.UUID,
    *,
    force: bool = False,
    skip_email: bool = False,
) -> Dict[str, Any]:
    """Kick off renewal outreach + competitor RFP blast.

    ``skip_email=True`` is for automated tests / CI dry runs.
    ``force=True`` bypasses calendar-window eligibility (still requires ACTIVE +
    verified + incumbent vendor).
    """
    contract = session.get(Contract, contract_id)
    if not contract:
        raise ValueError("contract_not_found")

    stats = {
        "contract_id": str(contract.id),
        "started": False,
        "reason": "",
        "competitors_discovered": 0,
        "emails_sent": 0,
    }

    if not contract.manager_verified:
        stats["reason"] = "contract_not_verified"
        return stats

    if contract.status != "ACTIVE":
        stats["reason"] = "contract_not_active"
        return stats

    if contract.vendor_id is None:
        stats["reason"] = "contract_missing_vendor"
        return stats

    if contract.end_date is None:
        stats["reason"] = "contract_missing_end_date"
        return stats

    today = date.today()
    days_left = (contract.end_date - today).days

    if days_left < 0:
        stats["reason"] = "contract_already_expired"
        return stats

    if not force:
        if contract.renewal_cycle_started_at is not None:
            stats["reason"] = "renewal_cycle_already_started"
            return stats

        if days_left > contract.renewal_notice_days:
            stats["reason"] = "outside_renewal_notice_window"
            return stats

    profile = session.get(RestaurantProfile, contract.restaurant_profile_id)
    if not profile:
        stats["reason"] = "profile_missing"
        return stats

    incumbent = session.get(Vendor, contract.vendor_id)
    if not incumbent:
        stats["reason"] = "incumbent_vendor_missing"
        return stats

    line_rows = session.exec(
        select(ContractLineItem).where(ContractLineItem.contract_id == contract.id)
    ).all()
    li_payload = [
        {
            "sku_name": r.sku_name,
            "pack_description": r.pack_description,
            "unit_of_measure": r.unit_of_measure,
            "fixed_price": r.fixed_price,
            "price_formula": r.price_formula,
        }
        for r in line_rows
    ]

    ams_blocks = build_ams_blocks(session, contract.id)
    ams_html = render_ams_blocks(ams_blocks)
    line_html = render_contract_line_items_table(li_payload)
    incumbent_blob = _incumbent_benchmark_bullet_lines(li_payload)

    category_blob = _categories_blob(contract)

    # Persist lifecycle markers.
    contract.renewal_cycle_started_at = datetime.utcnow()
    contract.status = "EXPIRING_SOON"
    contract.updated_at = datetime.utcnow()
    session.add(contract)
    session.flush()

    # ── Incumbent negotiation ──────────────────────────────────────────────
    neg_inc = session.exec(
        select(Negotiation)
        .where(Negotiation.contract_id == contract.id)
        .where(Negotiation.vendor_id == incumbent.id)
        .where(Negotiation.intent == "RENEWAL")
    ).first()

    if neg_inc is None:
        neg_inc = Negotiation(
            contract_id=contract.id,
            vendor_id=incumbent.id,
            intent="RENEWAL",
            status="OPEN",
            max_rounds=3,
            rounds_used=0,
        )
        session.add(neg_inc)
        session.flush()

    inc_email = _contact_email(session, profile.id, incumbent)

    subj_preview = f"Renewal discussion — {contract.nickname}"
    body_preview = ""  # captured below via helper constructing HTML inside mailer

    if not skip_email:
        send_contract_renewal_email(
            to_email=inc_email,
            vendor_name=incumbent.name,
            restaurant_name=profile.name,
            contract_nickname=contract.nickname,
            contract_id=str(contract.id),
            negotiation_id=str(neg_inc.id),
            end_date=contract.end_date,
            days_remaining=days_left,
            category_blob=category_blob,
            line_items_html=line_html,
            ams_context_html=ams_html,
            trust_footer_html=_incumbent_trust_footer_html(
                session, profile.id, incumbent.id
            ),
        )
        stats["emails_sent"] += 1

    session.add(
        NegotiationRound(
            negotiation_id=neg_inc.id,
            round_index=0,
            direction="OUTBOUND",
            status="SENT",
            subject=subj_preview[:512],
            body="<lifecycle outbound — renewal request>",
            offer_snapshot={
                "phase": "renewal_request",
                "ams_blocks": ams_blocks,
                "category_blob": category_blob,
            },
            manager_approved_to_send=True,
            sent_at=datetime.utcnow(),
        )
    )

    # ── Competitors ─────────────────────────────────────────────────────────
    competitors = discover_competitor_vendors(profile, session, incumbent)
    stats["competitors_discovered"] = len(competitors)

    for comp in competitors:
        neg_c = session.exec(
            select(Negotiation)
            .where(Negotiation.contract_id == contract.id)
            .where(Negotiation.vendor_id == comp.id)
            .where(Negotiation.intent == "NEW_CONTRACT")
        ).first()

        if neg_c is None:
            neg_c = Negotiation(
                contract_id=contract.id,
                vendor_id=comp.id,
                intent="NEW_CONTRACT",
                status="OPEN",
                max_rounds=3,
                rounds_used=0,
            )
            session.add(neg_c)
            session.flush()

        comp_email = _contact_email(session, profile.id, comp)

        if not skip_email:
            send_contract_competitor_rfp_email(
                to_email=comp_email,
                vendor_name=comp.name,
                restaurant_name=profile.name,
                contract_nickname=contract.nickname,
                contract_id=str(contract.id),
                negotiation_id=str(neg_c.id),
                category_blob=category_blob,
                incumbent_benchmark_blob=incumbent_blob,
                line_items_html=line_html,
                ams_context_html=ams_html,
            )
            stats["emails_sent"] += 1

        session.add(
            NegotiationRound(
                negotiation_id=neg_c.id,
                round_index=0,
                direction="OUTBOUND",
                status="SENT",
                subject=f"Contract RFP — {contract.nickname}",
                body="<lifecycle outbound — competitor rfp>",
                offer_snapshot={
                    "phase": "competitor_rfp",
                    "ams_blocks": ams_blocks,
                },
                manager_approved_to_send=True,
                sent_at=datetime.utcnow(),
            )
        )

    session.add(
        Notification(
            title="Renewal cycle started",
            message=(
                f"{contract.nickname}: emailed incumbent + {len(competitors)} competitor(s). "
                "Review Negotiations in the Contracts dashboard."
            ),
        )
    )

    renewal_alert = ManagerAlert(
        restaurant_profile_id=profile.id,
        grouping_key=str(contract.id),
        severity="ACTION_REQUIRED",
        title=f"Contract renewal outreach — {contract.nickname}",
        body=(
            "We've emailed your incumbent for renewal quotes and issued exploratory "
            f"RFPs to {len(competitors)} alternative distributors. "
            "Watch vendor replies in your inbox and in-app negotiation logs."
        ),
        action_url="/contracts",
        action_label="Open Contracts",
    )
    session.add(renewal_alert)
    session.flush()
    from services.manager_alert_dispatch import deliver_manager_alert_sms

    deliver_manager_alert_sms(session, renewal_alert, profile)

    session.commit()
    stats["started"] = True
    stats["reason"] = "ok"
    return stats


def daily_contract_lifecycle_tick(session: Session) -> Dict[str, Any]:
    """Scheduler hook — scans every ACTIVE verified contract with future end dates."""
    contracts = session.exec(
        select(Contract).where(Contract.status == "ACTIVE")
        .where(Contract.manager_verified == True)  # noqa: E712
        .where(Contract.end_date.isnot(None))
        .where(Contract.vendor_id.isnot(None))
        .where(Contract.renewal_cycle_started_at.is_(None))
    ).all()

    today = date.today()
    started = []
    for c in contracts:
        days_left = (c.end_date - today).days  # type: ignore[operator]
        if days_left < 0:
            continue
        if days_left <= c.renewal_notice_days:
            try:
                stats = start_renewal_cycle(session, c.id, force=False, skip_email=False)
                if stats.get("started"):
                    started.append(stats["contract_id"])
            except Exception as exc:
                log.exception("[lifecycle] failed contract=%s: %s", c.id, exc)
                session.rollback()

    return {"scanned": len(contracts), "started_contract_ids": started}


def maybe_send_counter_offers(session: Session, contract_id: uuid_mod.UUID) -> int:
    """Optional bargaining wave once ≥2 negotiations publish midpoint anchors."""
    contract = session.get(Contract, contract_id)
    if not contract or contract.status not in ("EXPIRING_SOON", "IN_NEGOTIATION"):
        return 0

    profile = session.get(RestaurantProfile, contract.restaurant_profile_id)
    if not profile:
        return 0

    negotiations = session.exec(
        select(Negotiation).where(Negotiation.contract_id == contract.id)
    ).all()

    quotes: List[Tuple[Negotiation, float]] = []
    for neg in negotiations:
        latest_in = session.exec(
            select(NegotiationRound)
            .where(NegotiationRound.negotiation_id == neg.id)
            .where(NegotiationRound.direction == "INBOUND")
            .where(NegotiationRound.status == "RECEIVED")
            .order_by(NegotiationRound.round_index.desc())
        ).first()
        if not latest_in or not latest_in.offer_snapshot:
            continue
        mid = latest_in.offer_snapshot.get("avg_quote_midpoint")
        if isinstance(mid, (int, float)) and mid > 0:
            quotes.append((neg, float(mid)))

    if len(quotes) < 2:
        return 0

    best_neg, best_val = min(quotes, key=lambda item: item[1])

    comparison_lines = [
        "<li>Benchmark midpoint drawn from responses received so far "
        f"(anonymous aggregation): <strong>${best_val:.2f}</strong></li>"
    ]

    sent = 0
    for neg, mid in quotes:
        if neg.id == best_neg.id:
            continue
        if mid <= best_val * 1.02:
            continue

        counters = session.exec(
            select(NegotiationRound)
            .where(NegotiationRound.negotiation_id == neg.id)
            .where(NegotiationRound.direction == "OUTBOUND")
            .where(NegotiationRound.subject.is_not(None))
            .where(NegotiationRound.subject.contains("Contract discussion"))
        ).all()
        if counters:
            continue

        if neg.rounds_used >= neg.max_rounds:
            continue

        vendor = session.get(Vendor, neg.vendor_id)
        if not vendor:
            continue

        intro = (
            "Thanks for your thoughtful reply. We're consolidating directional economics "
            "and noticed another respondent landed materially lower on blended midpoint pricing "
            "for comparable coverage — we'd love to explore whether you can sharpen terms "
            "within a collaborative window."
        )

        comparison_html = "<ul>" + "".join(comparison_lines) + "</ul>"
        to_email = _contact_email(session, profile.id, vendor)

        send_contract_counter_offer_email(
            to_email=to_email,
            vendor_name=vendor.name,
            restaurant_name=profile.name,
            contract_nickname=contract.nickname,
            contract_id=str(contract.id),
            negotiation_id=str(neg.id),
            intro_paragraph=intro,
            benchmark_comparison_html=comparison_html,
        )

        neg.rounds_used += 1
        session.add(neg)

        prev_idxs = session.exec(
            select(NegotiationRound.round_index).where(
                NegotiationRound.negotiation_id == neg.id
            )
        ).all()
        idx = max(prev_idxs, default=-1) + 1

        session.add(
            NegotiationRound(
                negotiation_id=neg.id,
                round_index=idx,
                direction="OUTBOUND",
                status="SENT",
                subject=f"Contract discussion — {contract.nickname}",
                body="<counter-offer outbound>",
                offer_snapshot={
                    "phase": "counter_offer",
                    "benchmark_midpoint": best_val,
                    "their_midpoint": mid,
                },
                manager_approved_to_send=True,
                sent_at=datetime.utcnow(),
            )
        )

        session.add(
            Notification(
                title="Contract negotiation counter-offer sent",
                message=f"{vendor.name}: exploratory counter referencing aggregated benchmarks.",
            )
        )

        sent += 1

    if sent:
        session.commit()
    return sent
