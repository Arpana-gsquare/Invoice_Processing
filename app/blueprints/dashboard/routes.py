"""
Dashboard Blueprint - KPI overview, workflow tabs, activity feed.
All KPI counts computed in a single MongoDB aggregation pass.
Supports ?range=7d | 30d | 90d | lifetime  (default: lifetime)
"""
from datetime import datetime, timezone, timedelta
from flask import Blueprint, render_template, request
from flask_login import login_required, current_user
from ...extensions import get_db
from ...models.audit import AuditLog

dashboard_bp = Blueprint("dashboard", __name__)

# ── Range options ──────────────────────────────────────────────────────────────
RANGE_OPTIONS = {
    "7d":       ("Last 7 Days",  timedelta(days=7)),
    "30d":      ("Last 30 Days", timedelta(days=30)),
    "90d":      ("Last 90 Days", timedelta(days=90)),
    "lifetime": ("Lifetime",     None),
}


@dashboard_bp.route("/")
@login_required
def index():
    db  = get_db()
    now = datetime.now(timezone.utc)

    # ── Parse date-range filter ────────────────────────────────────────────────
    selected_range = request.args.get("range", "lifetime")
    if selected_range not in RANGE_OPTIONS:
        selected_range = "lifetime"
    range_label, range_delta = RANGE_OPTIONS[selected_range]
    cutoff = (now - range_delta) if range_delta else None

    # Base match clause applied to every query
    base_match = {"is_deleted": {"$ne": True}}
    if cutoff:
        base_match["upload_timestamp"] = {"$gte": cutoff}

    # ── Single-pass KPI aggregation ────────────────────────────────────────────
    kpi_pipeline = [
        {"$match": base_match},
        {"$group": {
            "_id": None,
            "total_invoices":  {"$sum": 1},

            # ── Legacy status counts ─────────────────────────────────────────
            "pending_approval": {"$sum": {"$cond": [
                {"$and": [
                    {"$eq": ["$status", "pending"]},
                    {"$ne": ["$risk_flag", "DUPLICATE"]},
                ]}, 1, 0
            ]}},
            "approved":        {"$sum": {"$cond": [{"$eq": ["$status", "approved"]}, 1, 0]}},
            "rejected":        {"$sum": {"$cond": [{"$eq": ["$status", "rejected"]}, 1, 0]}},

            # ── Workflow_status counts ───────────────────────────────────────
            "wf_uploaded":      {"$sum": {"$cond": [{"$eq": ["$workflow_status", "uploaded"]},          1, 0]}},
            "wf_processed":     {"$sum": {"$cond": [{"$eq": ["$workflow_status", "processed"]},         1, 0]}},
            "wf_missing_po":    {"$sum": {"$cond": [{"$eq": ["$workflow_status", "missing_po"]},        1, 0]}},
            "wf_manual_review": {"$sum": {"$cond": [{"$eq": ["$workflow_status", "manual_review"]},     1, 0]}},
            "wf_pending_L1":    {"$sum": {"$cond": [{"$eq": ["$workflow_status", "pending_L1"]},        1, 0]}},
            "wf_pending_L2":    {"$sum": {"$cond": [{"$eq": ["$workflow_status", "pending_L2"]},        1, 0]}},
            "wf_pending_L3":    {"$sum": {"$cond": [{"$eq": ["$workflow_status", "pending_L3"]},        1, 0]}},
            "wf_approved":      {"$sum": {"$cond": [{"$eq": ["$workflow_status", "approved"]},          1, 0]}},
            "wf_ready":         {"$sum": {"$cond": [{"$eq": ["$workflow_status", "ready_for_payment"]}, 1, 0]}},

            # ── Approved-by counts (status_history) ──────────────────────────
            "approved_by_L1": {"$sum": {"$cond": [
                {"$gt": [{"$size": {"$filter": {
                    "input": {"$ifNull": ["$status_history", []]},
                    "as": "h",
                    "cond": {"$eq": ["$$h.from_status", "pending_L1"]},
                }}}, 0]}, 1, 0,
            ]}},
            "approved_by_L2": {"$sum": {"$cond": [
                {"$gt": [{"$size": {"$filter": {
                    "input": {"$ifNull": ["$status_history", []]},
                    "as": "h",
                    "cond": {"$eq": ["$$h.from_status", "pending_L2"]},
                }}}, 0]}, 1, 0,
            ]}},
            "approved_by_L3": {"$sum": {"$cond": [
                {"$gt": [{"$size": {"$filter": {
                    "input": {"$ifNull": ["$status_history", []]},
                    "as": "h",
                    "cond": {"$eq": ["$$h.from_status", "pending_L3"]},
                }}}, 0]}, 1, 0,
            ]}},

            # ── Risk / PO counts ─────────────────────────────────────────────
            "low_risk_count":  {"$sum": {"$cond": [{"$eq": ["$risk_flag", "LOW RISK"]},  1, 0]}},
            "moderate_count":  {"$sum": {"$cond": [{"$eq": ["$risk_flag", "MODERATE"]},  1, 0]}},
            "high_risk_count": {"$sum": {"$cond": [{"$eq": ["$risk_flag", "HIGH RISK"]}, 1, 0]}},
            "duplicate_count": {"$sum": {"$cond": [{"$eq": ["$risk_flag", "DUPLICATE"]}, 1, 0]}},
            "po_full":         {"$sum": {"$cond": [{"$eq": ["$po_match_status", "full"]},    1, 0]}},
            "po_partial":      {"$sum": {"$cond": [{"$eq": ["$po_match_status", "partial"]}, 1, 0]}},
            "no_po_count":     {"$sum": {"$cond": [{"$eq": ["$po_match_status", "none"]},   1, 0]}},

            # ── Financials ───────────────────────────────────────────────────
            "total_amount":    {"$sum": "$total_amount"},
            "approved_amount": {"$sum": {"$cond": [
                {"$eq": ["$status", "approved"]}, "$total_amount", 0
            ]}},
            "overdue": {"$sum": {"$cond": [
                {"$and": [
                    {"$eq":  ["$status", "pending"]},
                    {"$lt":  ["$due_date", now]},
                    {"$ne":  ["$due_date", None]},
                ]}, 1, 0
            ]}},
        }},
    ]
    kpi_raw = list(db.invoices.aggregate(kpi_pipeline))
    k = kpi_raw[0] if kpi_raw else {}

    stats = {
        "total_invoices":   k.get("total_invoices",   0),
        "pending_approval": k.get("pending_approval",  0),
        "approved":         k.get("approved",          0),
        "rejected":         k.get("rejected",          0),
        "low_risk_count":   k.get("low_risk_count",    0),
        "moderate_count":   k.get("moderate_count",    0),
        "high_risk_count":  k.get("high_risk_count",   0),
        "duplicate_count":  k.get("duplicate_count",   0),
        "no_po_count":      k.get("no_po_count",       0),
        "total_amount":     k.get("total_amount",      0),
        "approved_amount":  k.get("approved_amount",   0),
        "overdue":          k.get("overdue",           0),
        "wf_uploaded":      k.get("wf_uploaded",      0),
        "wf_processed":     k.get("wf_processed",     0),
        "wf_missing_po":    k.get("wf_missing_po",    0),
        "wf_manual_review": k.get("wf_manual_review", 0),
        "wf_pending_L1":    k.get("wf_pending_L1",    0),
        "wf_pending_L2":    k.get("wf_pending_L2",    0),
        "wf_pending_L3":    k.get("wf_pending_L3",    0),
        "wf_approved":      k.get("wf_approved",      0),
        "wf_ready":         k.get("wf_ready",         0),
        "po_full":          k.get("po_full",          0),
        "po_partial":       k.get("po_partial",       0),
        "approved_by_L1":   k.get("approved_by_L1",   0),
        "approved_by_L2":   k.get("approved_by_L2",   0),
        "approved_by_L3":   k.get("approved_by_L3",   0),
    }

    # ── Role-specific workflow queue count (always unfiltered — current state) ─
    role_queue_state = {"L1": "pending_L1", "L2": "pending_L2", "L3": "pending_L3"}
    my_queue_state   = role_queue_state.get(current_user.role)
    my_queue_count   = 0
    if my_queue_state:
        my_queue_count = db.invoices.count_documents({
            "is_deleted":      {"$ne": True},
            "workflow_status": my_queue_state,
        })

    # ── Trend chart — group by day (≤30d), week (90d), or month (lifetime) ────
    if selected_range in ("7d", "30d"):
        # Daily grouping
        chart_cutoff = cutoff
        trend_pipeline = [
            {"$match": {"upload_timestamp": {"$gte": chart_cutoff}, "is_deleted": {"$ne": True}}},
            {"$group": {
                "_id": {
                    "year":  {"$year":       "$upload_timestamp"},
                    "month": {"$month":      "$upload_timestamp"},
                    "day":   {"$dayOfMonth": "$upload_timestamp"},
                },
                "count": {"$sum": 1},
                "total": {"$sum": "$total_amount"},
            }},
            {"$sort": {"_id.year": 1, "_id.month": 1, "_id.day": 1}},
        ]
        chart_mode = "daily"
    elif selected_range == "90d":
        # Weekly grouping
        trend_pipeline = [
            {"$match": {"upload_timestamp": {"$gte": cutoff}, "is_deleted": {"$ne": True}}},
            {"$group": {
                "_id": {
                    "year": {"$isoWeekYear": "$upload_timestamp"},
                    "week": {"$isoWeek":     "$upload_timestamp"},
                },
                "count": {"$sum": 1},
                "total": {"$sum": "$total_amount"},
            }},
            {"$sort": {"_id.year": 1, "_id.week": 1}},
        ]
        chart_mode = "weekly"
    else:
        # Monthly grouping — last 6 months for lifetime
        six_months_ago = now - timedelta(days=180)
        trend_pipeline = [
            {"$match": {"upload_timestamp": {"$gte": six_months_ago}, "is_deleted": {"$ne": True}}},
            {"$group": {
                "_id":   {"year": {"$year": "$upload_timestamp"}, "month": {"$month": "$upload_timestamp"}},
                "count": {"$sum": 1},
                "total": {"$sum": "$total_amount"},
            }},
            {"$sort": {"_id.year": 1, "_id.month": 1}},
        ]
        chart_mode = "monthly"

    monthly_data = list(db.invoices.aggregate(trend_pipeline))

    # ── Top vendors ────────────────────────────────────────────────────────────
    top_vendors_pipeline = [
        {"$match": base_match},
        {"$group": {
            "_id":          "$vendor_name",
            "count":        {"$sum": 1},
            "total_amount": {"$sum": "$total_amount"},
        }},
        {"$sort": {"count": -1}},
        {"$limit": 8},
    ]
    top_vendors = list(db.invoices.aggregate(top_vendors_pipeline))

    # ── Recent invoices ────────────────────────────────────────────────────────
    recent_invoices = list(
        db.invoices.find(
            base_match,
            {"invoice_number": 1, "vendor_name": 1, "total_amount": 1,
             "currency_symbol": 1, "risk_flag": 1, "status": 1,
             "workflow_status": 1, "upload_timestamp": 1},
        ).sort("upload_timestamp", -1).limit(10)
    )
    for inv in recent_invoices:
        inv["_id"] = str(inv["_id"])
        if hasattr(inv.get("upload_timestamp"), "strftime"):
            inv["upload_timestamp"] = inv["upload_timestamp"].strftime("%b %d, %Y")

    # ── Recent audit activity (always unfiltered) ──────────────────────────────
    recent_activity = AuditLog.get_recent(limit=10)

    return render_template(
        "dashboard.html",
        stats=stats,
        monthly_data=monthly_data,
        chart_mode=chart_mode,
        top_vendors=top_vendors,
        recent_invoices=recent_invoices,
        recent_activity=recent_activity,
        my_queue_count=my_queue_count,
        my_queue_state=my_queue_state,
        selected_range=selected_range,
        range_label=range_label,
    )
