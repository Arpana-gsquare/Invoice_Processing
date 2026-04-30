"""
Executive Summary Service
--------------------------
Aggregates invoice data from MongoDB and generates AI-powered business
insights via Gemini.

Cache: In-memory, 5-minute TTL per user session (keyed by "global").
Gemini is NOT called on every page load.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from flask import current_app

from ..extensions import get_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Simple in-process cache  {key: {"data": ..., "expires_at": float}}
# ---------------------------------------------------------------------------
_cache: dict[str, dict] = {}
CACHE_TTL_SECONDS = 300   # 5 minutes


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry and time.time() < entry["expires_at"]:
        return entry["data"]
    return None


def _cache_set(key: str, data: dict):
    _cache[key] = {"data": data, "expires_at": time.time() + CACHE_TTL_SECONDS}


def _cache_invalidate(key: str = "executive_summary"):
    _cache.pop(key, None)


# ---------------------------------------------------------------------------
# Data Aggregation
# ---------------------------------------------------------------------------

def aggregate_invoice_metrics() -> dict[str, Any]:
    """
    Pull aggregated metrics from MongoDB.
    Returns a compact dict safe to send to Gemini (no raw invoice content).
    """
    db  = get_db()
    now = datetime.now(timezone.utc)

    # Current period: this calendar month
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Previous period: last calendar month
    prev_end    = month_start - timedelta(seconds=1)
    prev_start  = prev_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    base_filter = {"is_deleted": {"$ne": True}}

    # ── Global metrics ────────────────────────────────────────────────────
    pipeline_global = [
        {"$match": base_filter},
        {"$group": {
            "_id":            None,
            "total_invoices": {"$sum": 1},
            "total_amount":   {"$sum": "$total_amount"},
            "avg_amount":     {"$avg": "$total_amount"},
            "high_risk":      {"$sum": {"$cond": [{"$eq": ["$risk_flag", "HIGH RISK"]}, 1, 0]}},
            "moderate_risk":  {"$sum": {"$cond": [{"$eq": ["$risk_flag", "MODERATE"]},  1, 0]}},
            "duplicates":     {"$sum": {"$cond": [{"$eq": ["$risk_flag", "DUPLICATE"]}, 1, 0]}},
            "approved":       {"$sum": {"$cond": [{"$eq": ["$status", "approved"]}, 1, 0]}},
            "rejected":       {"$sum": {"$cond": [{"$eq": ["$status", "rejected"]}, 1, 0]}},
            "pending":        {"$sum": {"$cond": [{"$eq": ["$status", "pending"]},  1, 0]}},
            "po_matched":     {"$sum": {"$cond": [{"$eq": ["$po_match_status", "MATCHED"]}, 1, 0]}},
            "prop_matched":   {"$sum": {"$cond": [{"$eq": ["$proposal_match_status", "MATCHED"]}, 1, 0]}},
        }}
    ]
    g_raw = list(db.invoices.aggregate(pipeline_global))
    g     = g_raw[0] if g_raw else {}

    total = g.get("total_invoices", 0) or 1  # avoid div/0
    global_metrics = {
        "total_invoices":      g.get("total_invoices", 0),
        "total_amount":        round(g.get("total_amount", 0), 2),
        "avg_invoice_amount":  round(g.get("avg_amount", 0), 2),
        "high_risk_count":     g.get("high_risk", 0),
        "high_risk_pct":       round(g.get("high_risk", 0) / total * 100, 1),
        "moderate_risk_count": g.get("moderate_risk", 0),
        "duplicate_count":     g.get("duplicates", 0),
        "approval_rate_pct":   round(g.get("approved", 0) / total * 100, 1),
        "rejection_rate_pct":  round(g.get("rejected", 0) / total * 100, 1),
        "pending_count":       g.get("pending", 0),
        "po_match_rate_pct":   round(g.get("po_matched", 0) / total * 100, 1),
        "proposal_match_rate_pct": round(g.get("prop_matched", 0) / total * 100, 1),
    }

    # ── Current month metrics ────────────────────────────────────────────
    pipeline_month = [
        {"$match": {**base_filter, "upload_timestamp": {"$gte": month_start}}},
        {"$group": {
            "_id":   None,
            "count": {"$sum": 1},
            "total": {"$sum": "$total_amount"},
            "avg":   {"$avg": "$total_amount"},
        }}
    ]
    m_raw = list(db.invoices.aggregate(pipeline_month))
    m     = m_raw[0] if m_raw else {}

    # ── Previous month metrics ───────────────────────────────────────────
    pipeline_prev = [
        {"$match": {**base_filter,
                    "upload_timestamp": {"$gte": prev_start, "$lte": prev_end}}},
        {"$group": {
            "_id":   None,
            "count": {"$sum": 1},
            "total": {"$sum": "$total_amount"},
            "avg":   {"$avg": "$total_amount"},
        }}
    ]
    p_raw = list(db.invoices.aggregate(pipeline_prev))
    p     = p_raw[0] if p_raw else {}

    period_comparison = {
        "current_month_count":  m.get("count", 0),
        "current_month_total":  round(m.get("total", 0), 2),
        "current_month_avg":    round(m.get("avg", 0), 2),
        "previous_month_count": p.get("count", 0),
        "previous_month_total": round(p.get("total", 0), 2),
        "previous_month_avg":   round(p.get("avg", 0), 2),
        "mom_count_change_pct": _pct_change(p.get("count", 0), m.get("count", 0)),
        "mom_amount_change_pct": _pct_change(p.get("total", 0), m.get("total", 0)),
    }

    # ── Per-vendor metrics (top 10 by spend) ────────────────────────────
    pipeline_vendor = [
        {"$match": base_filter},
        {"$group": {
            "_id":          "$vendor_name",
            "invoice_count":    {"$sum": 1},
            "total_amount":     {"$sum": "$total_amount"},
            "avg_amount":       {"$avg": "$total_amount"},
            "approved":         {"$sum": {"$cond": [{"$eq": ["$status", "approved"]}, 1, 0]}},
            "rejected":         {"$sum": {"$cond": [{"$eq": ["$status", "rejected"]}, 1, 0]}},
            "high_risk":        {"$sum": {"$cond": [{"$eq": ["$risk_flag", "HIGH RISK"]}, 1, 0]}},
            "po_matched":       {"$sum": {"$cond": [{"$eq": ["$po_match_status", "MATCHED"]}, 1, 0]}},
            "prop_matched":     {"$sum": {"$cond": [{"$eq": ["$proposal_match_status", "MATCHED"]}, 1, 0]}},
        }},
        {"$sort": {"total_amount": -1}},
        {"$limit": 10},
    ]
    vendors_raw = list(db.invoices.aggregate(pipeline_vendor))

    # Per-vendor previous-month averages
    prev_avgs = _vendor_prev_month_avg(db, base_filter, prev_start, prev_end)

    vendors = []
    for v in vendors_raw:
        name  = v["_id"] or "Unknown"
        count = v["invoice_count"] or 1
        vendors.append({
            "vendor_name":         name,
            "invoice_count":       v["invoice_count"],
            "total_amount":        round(v["total_amount"], 2),
            "avg_amount":          round(v["avg_amount"], 2),
            "previous_month_avg":  round(prev_avgs.get(name, 0), 2),
            "avg_change_pct":      _pct_change(prev_avgs.get(name, 0), v["avg_amount"]),
            "approval_rate_pct":   round(v["approved"] / count * 100, 1),
            "rejection_rate_pct":  round(v["rejected"] / count * 100, 1),
            "high_risk_count":     v["high_risk"],
            "po_match_rate_pct":   round(v["po_matched"] / count * 100, 1),
            "proposal_match_rate_pct": round(v["prop_matched"] / count * 100, 1),
        })

    return {
        "generated_at":     now.isoformat(),
        "global":           global_metrics,
        "period_comparison": period_comparison,
        "top_vendors":      vendors,
    }


def _vendor_prev_month_avg(db, base_filter: dict, start, end) -> dict:
    """Return {vendor_name: avg_amount} for previous month."""
    pipeline = [
        {"$match": {**base_filter,
                    "upload_timestamp": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": "$vendor_name", "avg": {"$avg": "$total_amount"}}},
    ]
    return {row["_id"]: row["avg"] for row in db.invoices.aggregate(pipeline)
            if row["_id"]}


def _pct_change(old: float, new: float) -> float | None:
    if not old:
        return None
    return round((new - old) / old * 100, 1)


# ---------------------------------------------------------------------------
# Gemini Insight Generation
# ---------------------------------------------------------------------------

INSIGHT_PROMPT = """\
You are a financial analyst AI reviewing an invoice processing system.
Given the following aggregated invoice analytics (NOT raw invoice data), generate an executive summary.

DATA:
{data_json}

INSTRUCTIONS:
- Generate exactly 5 to 8 concise insights
- Focus on: vendor spend patterns, changes in average invoice values vs prior month,
  approval/rejection trends, risk signals, PO/proposal match gaps
- Include specific vendor names, amounts, and percentages where available
- Avoid generic statements — every insight must reference actual numbers from the data
- If data is insufficient (e.g. fewer than 3 invoices), say so honestly
- Each insight must be 1–2 lines maximum

OUTPUT FORMAT — return ONLY this JSON, no markdown, no explanation:
{{
  "insights": [
    "insight 1",
    "insight 2"
  ]
}}"""


def generate_insights(aggregated: dict) -> list[str]:
    """
    Call Gemini with aggregated metrics and return a list of insight strings.
    Falls back to rule-based insights if Gemini fails.
    """
    from ..services.gemini_service import gemini_service
    import json as _json
    import re

    # Compact JSON — only send what Gemini needs, strip internal timestamps
    payload = {
        "global":           aggregated["global"],
        "period_comparison": aggregated["period_comparison"],
        "top_vendors":      aggregated["top_vendors"],
    }
    data_json = _json.dumps(payload, indent=2)
    prompt    = INSIGHT_PROMPT.format(data_json=data_json)

    try:
        raw = gemini_service._call_with_retry([prompt], max_retries=2)
        # Strip markdown fences if present
        text = raw.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"\s*```\s*$",        "", text, flags=re.MULTILINE)
        parsed = _json.loads(text.strip())
        insights = parsed.get("insights", [])
        if isinstance(insights, list) and insights:
            return [str(i) for i in insights[:8]]
    except Exception as exc:
        logger.warning("Gemini insight generation failed: %s — using fallback", exc)

    return _fallback_insights(aggregated)


def _fallback_insights(data: dict) -> list[str]:
    """Rule-based fallback insights when Gemini is unavailable."""
    g   = data["global"]
    pc  = data["period_comparison"]
    vs  = data["top_vendors"]
    out = []

    total = g.get("total_invoices", 0)
    if total == 0:
        return ["No invoice data available yet. Upload invoices to generate insights."]

    out.append(
        f"Portfolio overview: {total} total invoices worth "
        f"${g['total_amount']:,.0f} with an average of ${g['avg_invoice_amount']:,.0f}."
    )

    if g["high_risk_pct"] >= 20:
        out.append(
            f"⚠ High-risk alert: {g['high_risk_pct']}% of invoices "
            f"({g['high_risk_count']}) are flagged HIGH RISK — immediate review required."
        )
    elif g["high_risk_count"] > 0:
        out.append(
            f"{g['high_risk_count']} invoice(s) flagged as HIGH RISK "
            f"({g['high_risk_pct']}% of total)."
        )

    if g["duplicate_count"] > 0:
        out.append(
            f"{g['duplicate_count']} duplicate invoice(s) detected — verify these are "
            f"not double-payments."
        )

    mom = pc.get("mom_amount_change_pct")
    if mom is not None:
        direction = "up" if mom > 0 else "down"
        out.append(
            f"Month-over-month: invoice volume is {direction} {abs(mom):.1f}% "
            f"vs last month (${pc['previous_month_total']:,.0f} → "
            f"${pc['current_month_total']:,.0f})."
        )

    if g["approval_rate_pct"] < 50 and total >= 5:
        out.append(
            f"Low approval rate: only {g['approval_rate_pct']}% of invoices approved. "
            f"Review bottlenecks in the approval workflow."
        )

    if g["po_match_rate_pct"] < 40 and total >= 5:
        out.append(
            f"PO coverage gap: only {g['po_match_rate_pct']}% of invoices are matched "
            f"to a Purchase Order — consider enforcing PO requirements."
        )

    if vs:
        top = vs[0]
        out.append(
            f"Top vendor by spend: {top['vendor_name']} — "
            f"{top['invoice_count']} invoice(s) totalling ${top['total_amount']:,.0f} "
            f"(avg ${top['avg_amount']:,.0f}, {top['approval_rate_pct']}% approved)."
        )
        # Vendor with biggest MoM avg increase
        risers = [v for v in vs if v.get("avg_change_pct") and v["avg_change_pct"] > 20]
        if risers:
            r = max(risers, key=lambda x: x["avg_change_pct"])
            out.append(
                f"{r['vendor_name']} average invoice value rose "
                f"{r['avg_change_pct']}% vs last month "
                f"(${r['previous_month_avg']:,.0f} → ${r['avg_amount']:,.0f})."
            )

    return out[:8]


# ---------------------------------------------------------------------------
# Public entry point (called by the API endpoint)
# ---------------------------------------------------------------------------

def get_executive_summary(force_refresh: bool = False) -> dict:
    """
    Return cached insights if fresh, otherwise re-aggregate and re-generate.
    """
    cache_key = "executive_summary"

    if not force_refresh:
        cached = _cache_get(cache_key)
        if cached:
            logger.debug("Executive summary served from cache")
            return cached

    try:
        aggregated = aggregate_invoice_metrics()
        insights   = generate_insights(aggregated)
    except Exception as exc:
        logger.error("Executive summary generation failed: %s", exc)
        insights   = ["Unable to generate insights at this time. Please try again later."]
        aggregated = {"generated_at": datetime.now(timezone.utc).isoformat()}

    result = {
        "insights":     insights,
        "generated_at": aggregated.get("generated_at",
                        datetime.now(timezone.utc).isoformat()),
        "cached":       False,
    }
    _cache_set(cache_key, result)
    result["cached"] = False   # first fetch is never "from cache"
    return result
