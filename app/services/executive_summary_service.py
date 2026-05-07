"""
Executive Summary Service
--------------------------
Architecture:
  1. MongoDB aggregation  -> all countable KPIs (free, instant)
  2. Python anomaly rules -> deterministic flags from aggregated data (free)
  3. Gemini              -> interprets anomaly flags + headline numbers
                           into plain-English insights (~400-600 input tokens)

Gemini only sees: headline numbers + pre-detected anomaly list.
It does NOT see raw invoice documents or full vendor tables.

Cache: in-process dict, 5-minute TTL.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from flask import current_app
from ..extensions import get_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
_cache: dict[str, dict] = {}
CACHE_TTL_SECONDS = 300


def _cache_get(key: str):
    e = _cache.get(key)
    return e["data"] if e and time.time() < e["expires_at"] else None


def _cache_set(key: str, data: dict):
    _cache[key] = {"data": data, "expires_at": time.time() + CACHE_TTL_SECONDS}


def _pct_change(old: float, new: float):
    return round((new - old) / old * 100, 1) if old else None


# =============================================================================
# 1. MongoDB Aggregations  (zero AI tokens)
# =============================================================================

def aggregate_kpis() -> dict[str, Any]:
    db  = get_db()
    now = datetime.now(timezone.utc)

    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_end    = month_start - timedelta(seconds=1)
    prev_start  = prev_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    base        = {"is_deleted": {"$ne": True}}

    # Global single-pass
    g_raw = list(db.invoices.aggregate([
        {"$match": base},
        {"$group": {
            "_id":           None,
            "total":         {"$sum": 1},
            "total_amount":  {"$sum": "$total_amount"},
            "avg_amount":    {"$avg": "$total_amount"},
            "pending":       {"$sum": {"$cond": [{"$eq": ["$status", "pending"]},  1, 0]}},
            "approved":      {"$sum": {"$cond": [{"$eq": ["$status", "approved"]}, 1, 0]}},
            "rejected":      {"$sum": {"$cond": [{"$eq": ["$status", "rejected"]}, 1, 0]}},
            "high_risk":     {"$sum": {"$cond": [{"$eq": ["$risk_flag", "HIGH RISK"]}, 1, 0]}},
            "moderate":      {"$sum": {"$cond": [{"$eq": ["$risk_flag", "MODERATE"]},  1, 0]}},
            "low_risk":      {"$sum": {"$cond": [{"$eq": ["$risk_flag", "LOW RISK"]},  1, 0]}},
            "duplicates":    {"$sum": {"$cond": [{"$eq": ["$risk_flag", "DUPLICATE"]}, 1, 0]}},
            # po_match_status is stored as "full" / "partial" / "none" (workflow values)
            "po_matched":    {"$sum": {"$cond": [{"$eq": ["$po_match_status", "full"]}, 1, 0]}},
            # proposal_match_status is stored as "MATCHED" / "PARTIAL_MATCH" / "MISMATCH" / "NO_PROPOSAL"
            "prop_matched":  {"$sum": {"$cond": [{"$eq": ["$proposal_match_status", "MATCHED"]}, 1, 0]}},
            "overdue":       {"$sum": {"$cond": [
                {"$and": [{"$eq": ["$status", "pending"]},
                          {"$lt": ["$due_date", now]},
                          {"$ne": ["$due_date", None]}]}, 1, 0]}},
        }}
    ]))
    g     = g_raw[0] if g_raw else {}
    total = g.get("total", 0) or 1

    global_kpis = {
        "total_invoices":          g.get("total", 0),
        "total_amount":            round(g.get("total_amount", 0), 2),
        "avg_invoice_amount":      round(g.get("avg_amount", 0), 2),
        "pending_count":           g.get("pending", 0),
        "approved_count":          g.get("approved", 0),
        "rejected_count":          g.get("rejected", 0),
        "high_risk_count":         g.get("high_risk", 0),
        "high_risk_pct":           round(g.get("high_risk", 0) / total * 100, 1),
        "moderate_count":          g.get("moderate", 0),
        "low_risk_count":          g.get("low_risk", 0),
        "duplicate_count":         g.get("duplicates", 0),
        "overdue_count":           g.get("overdue", 0),
        "overdue_pct":             round(g.get("overdue", 0) / total * 100, 1),
        "approval_rate_pct":       round(g.get("approved", 0) / total * 100, 1),
        "rejection_rate_pct":      round(g.get("rejected", 0) / total * 100, 1),
        "po_match_rate_pct":       round(g.get("po_matched", 0) / total * 100, 1),
        "proposal_match_rate_pct": round(g.get("prop_matched", 0) / total * 100, 1),
    }

    # Month-over-month
    def _month_agg(match_extra: dict) -> dict:
        rows = list(db.invoices.aggregate([
            {"$match": {**base, **match_extra}},
            {"$group": {"_id": None,
                        "count": {"$sum": 1},
                        "total": {"$sum": "$total_amount"},
                        "avg":   {"$avg": "$total_amount"}}},
        ]))
        return rows[0] if rows else {}

    m = _month_agg({"upload_timestamp": {"$gte": month_start}})
    p = _month_agg({"upload_timestamp": {"$gte": prev_start, "$lte": prev_end}})

    mom = {
        "current_month_count":   m.get("count", 0),
        "current_month_total":   round(m.get("total", 0), 2),
        "current_month_avg":     round(m.get("avg",   0), 2),
        "previous_month_count":  p.get("count", 0),
        "previous_month_total":  round(p.get("total", 0), 2),
        "previous_month_avg":    round(p.get("avg",   0), 2),
        "mom_count_change_pct":  _pct_change(p.get("count", 0), m.get("count", 0)),
        "mom_amount_change_pct": _pct_change(p.get("total", 0), m.get("total", 0)),
    }

    # Top 5 vendors by spend
    vendors_raw = list(db.invoices.aggregate([
        {"$match": base},
        {"$group": {
            "_id":           "$vendor_name",
            "invoice_count": {"$sum": 1},
            "total_amount":  {"$sum": "$total_amount"},
            "avg_amount":    {"$avg": "$total_amount"},
            "high_risk":     {"$sum": {"$cond": [{"$eq": ["$risk_flag", "HIGH RISK"]}, 1, 0]}},
            "rejected":      {"$sum": {"$cond": [{"$eq": ["$status", "rejected"]}, 1, 0]}},
        }},
        {"$sort": {"total_amount": -1}},
        {"$limit": 5},
    ]))

    prev_avgs = {
        row["_id"]: row["avg"]
        for row in db.invoices.aggregate([
            {"$match": {**base, "upload_timestamp": {"$gte": prev_start, "$lte": prev_end}}},
            {"$group": {"_id": "$vendor_name", "avg": {"$avg": "$total_amount"}}},
        ])
        if row["_id"]
    }

    top_vendors = []
    for v in vendors_raw:
        name = v["_id"] or "Unknown"
        cnt  = v["invoice_count"] or 1
        top_vendors.append({
            "vendor_name":     name,
            "invoice_count":   v["invoice_count"],
            "total_amount":    round(v["total_amount"], 2),
            "avg_amount":      round(v["avg_amount"], 2),
            "prev_month_avg":  round(prev_avgs.get(name, 0), 2),
            "avg_change_pct":  _pct_change(prev_avgs.get(name, 0), v["avg_amount"]),
            "high_risk_count": v["high_risk"],
            "high_risk_pct":   round(v["high_risk"] / cnt * 100, 1),
            "rejection_count": v["rejected"],
            "rejection_pct":   round(v["rejected"] / cnt * 100, 1),
        })

    # Workflow pipeline stage counts
    wf_raw = list(db.invoices.aggregate([
        {"$match": base},
        {"$group": {
            "_id":               None,
            "pending_l1":        {"$sum": {"$cond": [{"$eq": ["$workflow_status", "pending_L1"]},        1, 0]}},
            "pending_l2":        {"$sum": {"$cond": [{"$eq": ["$workflow_status", "pending_L2"]},        1, 0]}},
            "pending_l3":        {"$sum": {"$cond": [{"$eq": ["$workflow_status", "pending_L3"]},        1, 0]}},
            "manual_review":     {"$sum": {"$cond": [{"$eq": ["$workflow_status", "manual_review"]},     1, 0]}},
            "missing_po":        {"$sum": {"$cond": [{"$eq": ["$workflow_status", "missing_po"]},        1, 0]}},
            "ready_for_payment": {"$sum": {"$cond": [{"$eq": ["$workflow_status", "ready_for_payment"]}, 1, 0]}},
        }}
    ]))
    wf = wf_raw[0] if wf_raw else {}
    workflow_kpis = {
        "pending_l1":        wf.get("pending_l1",        0),
        "pending_l2":        wf.get("pending_l2",        0),
        "pending_l3":        wf.get("pending_l3",        0),
        "manual_review":     wf.get("manual_review",     0),
        "missing_po":        wf.get("missing_po",        0),
        "ready_for_payment": wf.get("ready_for_payment", 0),
    }

    return {
        "generated_at": now.isoformat(),
        "global":       global_kpis,
        "mom":          mom,
        "top_vendors":  top_vendors,
        "workflow":     workflow_kpis,
    }


# =============================================================================
# 2. Anomaly Detection  (pure Python on aggregated data — zero AI tokens)
# =============================================================================

def detect_anomalies(kpis: dict) -> list[dict]:
    g       = kpis["global"]
    mom     = kpis["mom"]
    vendors = kpis["top_vendors"]
    total   = g["total_invoices"] or 1
    flags   = []

    # Duplicates
    if g["duplicate_count"] > 0:
        flags.append({
            "type": "duplicates", "severity": "HIGH",
            "message": (
                "%d duplicate invoice(s) detected — potential double-payment risk."
                % g["duplicate_count"]
            ),
            "data": {"count": g["duplicate_count"]},
        })

    # High-risk rate
    if g["high_risk_pct"] >= 25:
        flags.append({
            "type": "high_risk_rate", "severity": "HIGH",
            "message": (
                "%.1f%% of invoices flagged HIGH RISK (%d of %d)."
                % (g["high_risk_pct"], g["high_risk_count"], g["total_invoices"])
            ),
            "data": {"pct": g["high_risk_pct"], "count": g["high_risk_count"]},
        })
    elif g["high_risk_pct"] >= 10:
        flags.append({
            "type": "high_risk_rate", "severity": "MEDIUM",
            "message": (
                "%.1f%% high-risk rate (%d invoices) — above the 10%% threshold."
                % (g["high_risk_pct"], g["high_risk_count"])
            ),
            "data": {"pct": g["high_risk_pct"], "count": g["high_risk_count"]},
        })

    # Overdue backlog
    if g["overdue_pct"] >= 30:
        flags.append({
            "type": "overdue_backlog", "severity": "HIGH",
            "message": (
                "%d invoices overdue (%.1f%% of total) — payment delays accumulating."
                % (g["overdue_count"], g["overdue_pct"])
            ),
            "data": {"count": g["overdue_count"], "pct": g["overdue_pct"]},
        })
    elif g["overdue_count"] > 0:
        flags.append({
            "type": "overdue_backlog", "severity": "LOW",
            "message": "%d overdue invoice(s) pending payment." % g["overdue_count"],
            "data": {"count": g["overdue_count"]},
        })

    # Approval bottleneck
    pending_pct = round(g["pending_count"] / total * 100, 1)
    if pending_pct >= 60 and g["total_invoices"] >= 5:
        flags.append({
            "type": "approval_bottleneck", "severity": "MEDIUM",
            "message": (
                "%.1f%% of invoices still pending approval (%d invoices) — workflow bottleneck."
                % (pending_pct, g["pending_count"])
            ),
            "data": {"pct": pending_pct, "count": g["pending_count"]},
        })

    # PO coverage gap
    if g["po_match_rate_pct"] < 30 and g["total_invoices"] >= 5:
        flags.append({
            "type": "po_coverage_gap", "severity": "MEDIUM",
            "message": (
                "Only %.1f%% of invoices matched to a PO — procurement controls may be insufficient."
                % g["po_match_rate_pct"]
            ),
            "data": {"pct": g["po_match_rate_pct"]},
        })

    # MoM spend spike / drop
    mom_amt = mom.get("mom_amount_change_pct")
    if mom_amt is not None and mom_amt >= 50:
        flags.append({
            "type": "mom_spend_spike", "severity": "HIGH",
            "message": (
                "Invoice spend surged %+.1f%% month-over-month ($%s -> $%s)."
                % (mom_amt,
                   "{:,.0f}".format(mom["previous_month_total"]),
                   "{:,.0f}".format(mom["current_month_total"]))
            ),
            "data": {"pct": mom_amt,
                     "prev": mom["previous_month_total"],
                     "curr": mom["current_month_total"]},
        })
    elif mom_amt is not None and mom_amt <= -40:
        flags.append({
            "type": "mom_spend_drop", "severity": "LOW",
            "message": (
                "Invoice spend dropped %.1f%% month-over-month ($%s -> $%s)."
                % (mom_amt,
                   "{:,.0f}".format(mom["previous_month_total"]),
                   "{:,.0f}".format(mom["current_month_total"]))
            ),
            "data": {"pct": mom_amt},
        })

    # Per-vendor anomalies
    for v in vendors:
        name = v["vendor_name"]
        chg  = v.get("avg_change_pct")

        if chg is not None and chg >= 40 and v["invoice_count"] >= 2:
            flags.append({
                "type": "vendor_price_spike",
                "severity": "HIGH" if chg >= 80 else "MEDIUM",
                "message": (
                    "%s avg invoice value rose %+.1f%% vs last month ($%s -> $%s)."
                    % (name, chg,
                       "{:,.0f}".format(v["prev_month_avg"]),
                       "{:,.0f}".format(v["avg_amount"]))
                ),
                "data": {"vendor": name, "pct": chg,
                         "prev_avg": v["prev_month_avg"],
                         "curr_avg": v["avg_amount"]},
            })

        if v["rejection_pct"] >= 40 and v["invoice_count"] >= 3:
            flags.append({
                "type": "vendor_high_rejections", "severity": "MEDIUM",
                "message": (
                    "%s has a %.1f%% rejection rate (%d of %d invoices rejected)."
                    % (name, v["rejection_pct"], v["rejection_count"], v["invoice_count"])
                ),
                "data": {"vendor": name, "pct": v["rejection_pct"]},
            })

        if v["high_risk_pct"] >= 50 and v["invoice_count"] >= 2:
            flags.append({
                "type": "vendor_risk_pattern", "severity": "HIGH",
                "message": (
                    "%s: %.1f%% of their invoices are HIGH RISK (%d of %d)."
                    % (name, v["high_risk_pct"], v["high_risk_count"], v["invoice_count"])
                ),
                "data": {"vendor": name, "pct": v["high_risk_pct"]},
            })

    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    flags.sort(key=lambda x: order.get(x["severity"], 3))
    return flags


# =============================================================================
# 3. Gemini — interprets anomaly flags into plain-English insights
#    Payload: ~400-600 tokens (headline numbers + anomaly list only)
# =============================================================================

INSIGHT_PROMPT = """\
You are a financial analyst AI generating executive insights for a finance team.

CRITICAL ACCURACY RULES — you MUST follow these without exception:
1. Each metric below is DISTINCT. Never combine two metrics into one sentence just because they share the same numeric value.
2. Treat FINAL_STATUS metrics (approved/rejected/pending) separately from PO_MATCHING metrics. They measure completely different things.
3. Use ONLY the numbers provided — do not invent, estimate, or round differently.
4. If a metric is labelled "(NOTE: ...)" read the note carefully — it explains what the metric actually means.
5. Write 5-8 insights. Each insight must be 1-2 sentences max.
6. Prioritise HIGH-severity anomalies first.
7. If no anomalies exist, summarise portfolio health positively using the metrics.

FINAL STATUS METRICS  (these track the final approval decision on invoices):
{final_status_json}

PO MATCHING METRICS  (these track whether invoices were matched to a Purchase Order — independent of approval):
{po_matching_json}

PROPOSAL MATCHING METRICS  (these track whether invoices were matched to a vendor Proposal — independent of approval):
{proposal_matching_json}

WORKFLOW PIPELINE METRICS  (these track which approval stage each invoice is currently at):
{workflow_json}

VOLUME & SPEND METRICS:
{volume_json}

PRE-DETECTED ANOMALIES (flagged by automated rules):
{anomalies_json}

Return ONLY valid JSON — no markdown, no fences, no explanation:
{{"insights": ["insight 1", "insight 2", ...]}}"""


def generate_insights(kpis: dict, anomalies: list[dict]) -> list[str]:
    from .gemini_service import gemini_service

    g   = kpis["global"]
    mom = kpis["mom"]
    wf  = kpis.get("workflow", {})

    total = g["total_invoices"] or 1

    # ── Final approval-status metrics (what happened at the END of the workflow)
    final_status = {
        "total_invoices":  g["total_invoices"],
        "approved_count":  g["approved_count"],
        "final_approved_pct": {
            "value": g["approval_rate_pct"],
            "NOTE":  "% of ALL invoices that reached FINAL status=approved (completed L1+L2+L3 chain)",
        },
        "rejected_count":  g["rejected_count"],
        "final_rejected_pct": {
            "value": g["rejection_rate_pct"],
            "NOTE":  "% of ALL invoices that reached FINAL status=rejected",
        },
        "still_pending_count": g["pending_count"],
    }

    # ── PO matching metrics (independent of approval; measures procurement coverage)
    po_matching = {
        "po_fully_matched_count": g.get("po_match_rate_pct", 0) / 100 * total,
        "po_fully_matched_pct": {
            "value": g["po_match_rate_pct"],
            "NOTE":  "% of ALL invoices where a Purchase Order was found with FULL match score (>=85/100). "
                     "This is SEPARATE from the approval rate above.",
        },
    }

    # ── Proposal matching metrics
    proposal_matching = {
        "proposal_matched_pct": {
            "value": g["proposal_match_rate_pct"],
            "NOTE":  "% of ALL invoices matched to a vendor Proposal. "
                     "Independent of PO matching and approval status.",
        },
    }

    # ── Workflow pipeline (in-progress stage distribution)
    workflow = {
        "pending_L1_review":      wf.get("pending_l1", 0),
        "pending_L2_review":      wf.get("pending_l2", 0),
        "pending_L3_review":      wf.get("pending_l3", 0),
        "in_manual_review":       wf.get("manual_review", 0),
        "missing_po_stage":       wf.get("missing_po", 0),
        "ready_for_payment":      wf.get("ready_for_payment", 0),
        "NOTE": "These counts show where invoices currently sit in the approval pipeline.",
    }

    # ── Volume & spend
    volume = {
        "total_amount":          g["total_amount"],
        "avg_invoice_amount":    g["avg_invoice_amount"],
        "high_risk_count":       g["high_risk_count"],
        "high_risk_pct":         g["high_risk_pct"],
        "overdue_count":         g["overdue_count"],
        "duplicate_count":       g["duplicate_count"],
        "mom_amount_change_pct": mom.get("mom_amount_change_pct"),
        "current_month_total":   mom["current_month_total"],
        "previous_month_total":  mom["previous_month_total"],
        "top_vendor":       kpis["top_vendors"][0]["vendor_name"] if kpis["top_vendors"] else None,
        "top_vendor_total": kpis["top_vendors"][0]["total_amount"] if kpis["top_vendors"] else 0,
    }

    slim_anomalies = [
        {"severity": a["severity"], "type": a["type"], "message": a["message"]}
        for a in anomalies
    ]

    prompt = INSIGHT_PROMPT.format(
        final_status_json=json.dumps(final_status, indent=2),
        po_matching_json=json.dumps(po_matching, indent=2),
        proposal_matching_json=json.dumps(proposal_matching, indent=2),
        workflow_json=json.dumps(workflow, indent=2),
        volume_json=json.dumps(volume, indent=2),
        anomalies_json=json.dumps(slim_anomalies, indent=2),
    )

    try:
        raw  = gemini_service._call_with_retry([prompt], max_retries=2)
        text = raw.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"\s*```\s*$",        "", text, flags=re.MULTILINE)
        parsed   = json.loads(text.strip())
        insights = parsed.get("insights", [])
        if isinstance(insights, list) and insights:
            return [str(i) for i in insights[:8]]
    except Exception as exc:
        logger.warning("Gemini insight generation failed: %s -- using fallback", exc)

    return _fallback_insights(kpis, anomalies)


# =============================================================================
# 4. Rule-based fallback (no Gemini at all)
# =============================================================================

def _fallback_insights(kpis: dict, anomalies: list[dict]) -> list[str]:
    g   = kpis["global"]
    mom = kpis["mom"]
    out = []

    if g["total_invoices"] == 0:
        return ["No invoice data available yet. Upload invoices to generate insights."]

    out.append(
        "Portfolio: %d invoices totalling $%s (avg $%s each)."
        % (g["total_invoices"],
           "{:,.0f}".format(g["total_amount"]),
           "{:,.0f}".format(g["avg_invoice_amount"]))
    )

    for a in anomalies[:6]:
        icon = "🚨" if a["severity"] == "HIGH" else ("⚠️" if a["severity"] == "MEDIUM" else "ℹ️")
        out.append("%s %s" % (icon, a["message"]))

    if not any(a["type"] in ("mom_spend_spike", "mom_spend_drop") for a in anomalies):
        mom_pct = mom.get("mom_amount_change_pct")
        if mom_pct is not None:
            direction = "+%.1f%%" % mom_pct if mom_pct >= 0 else "%.1f%%" % mom_pct
            out.append(
                "Month-over-month spend: %s ($%s -> $%s)."
                % (direction,
                   "{:,.0f}".format(mom["previous_month_total"]),
                   "{:,.0f}".format(mom["current_month_total"]))
            )

    if kpis["top_vendors"] and not any(a["type"] == "vendor_price_spike" for a in anomalies):
        tv = kpis["top_vendors"][0]
        out.append(
            "Top vendor: %s — %d invoice(s), $%s total."
            % (tv["vendor_name"], tv["invoice_count"],
               "{:,.0f}".format(tv["total_amount"]))
        )

    return out[:8]


# =============================================================================
# 5. Public entry point
# =============================================================================

def get_executive_summary(force_refresh: bool = False) -> dict:
    cache_key = "executive_summary"

    if not force_refresh:
        cached = _cache_get(cache_key)
        if cached:
            logger.debug("Executive summary served from cache")
            return {**cached, "cached": True}

    try:
        kpis      = aggregate_kpis()
        anomalies = detect_anomalies(kpis)
        insights  = generate_insights(kpis, anomalies)
    except Exception as exc:
        logger.error("Executive summary failed: %s", exc)
        insights  = ["Unable to generate insights at this time. Please try again later."]
        anomalies = []
        kpis      = {"generated_at": datetime.now(timezone.utc).isoformat()}

    result = {
        "insights":     insights,
        "anomalies":    anomalies,
        "generated_at": kpis.get("generated_at",
                        datetime.now(timezone.utc).isoformat()),
        "cached":       False,
    }
    _cache_set(cache_key, result)
    return result
