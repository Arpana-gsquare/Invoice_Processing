"""
Dashboard Blueprint – KPI overview & activity feed.
All KPI counts computed in a single MongoDB aggregation pass.
"""
from datetime import datetime, timezone, timedelta
from flask import Blueprint, render_template
from flask_login import login_required
from ...extensions import get_db
from ...models.audit import AuditLog

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
@login_required
def index():
    db  = get_db()
    now = datetime.now(timezone.utc)

    # ── Single-pass KPI aggregation (replaces 9 separate count_documents) ──
    kpi_pipeline = [
        {"$match": {"is_deleted": {"$ne": True}}},
        {"$group": {
            "_id": None,
            # Status counts
            "total_invoices":  {"$sum": 1},
            "pending":         {"$sum": {"$cond": [{"$eq": ["$status", "pending"]},  1, 0]}},
            "approved":        {"$sum": {"$cond": [{"$eq": ["$status", "approved"]}, 1, 0]}},
            "rejected":        {"$sum": {"$cond": [{"$eq": ["$status", "rejected"]}, 1, 0]}},
            # Risk counts
            "safe_count":      {"$sum": {"$cond": [{"$eq": ["$risk_flag", "SAFE"]},      1, 0]}},
            "moderate_count":  {"$sum": {"$cond": [{"$eq": ["$risk_flag", "MODERATE"]},  1, 0]}},
            "high_risk_count": {"$sum": {"$cond": [{"$eq": ["$risk_flag", "HIGH RISK"]}, 1, 0]}},
            "duplicate_count": {"$sum": {"$cond": [{"$eq": ["$risk_flag", "DUPLICATE"]}, 1, 0]}},
            # Financial
            "total_amount":    {"$sum": "$total_amount"},
            "approved_amount": {"$sum": {"$cond": [
                {"$eq": ["$status", "approved"]}, "$total_amount", 0
            ]}},
            # Overdue: pending + due_date < now
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
        "total_invoices":  k.get("total_invoices",  0),
        "pending":         k.get("pending",         0),
        "approved":        k.get("approved",        0),
        "rejected":        k.get("rejected",        0),
        "safe_count":      k.get("safe_count",      0),
        "moderate_count":  k.get("moderate_count",  0),
        "high_risk_count": k.get("high_risk_count", 0),
        "duplicate_count": k.get("duplicate_count", 0),
        "total_amount":    k.get("total_amount",    0),
        "approved_amount": k.get("approved_amount", 0),
        "overdue":         k.get("overdue",         0),
    }

    # ── Monthly trend (last 6 months) ──────────────────────────────────────
    six_months_ago = now - timedelta(days=180)
    monthly_pipeline = [
        {"$match": {"upload_timestamp": {"$gte": six_months_ago},
                    "is_deleted": {"$ne": True}}},
        {"$group": {
            "_id":   {"year": {"$year": "$upload_timestamp"},
                      "month": {"$month": "$upload_timestamp"}},
            "count": {"$sum": 1},
            "total": {"$sum": "$total_amount"},
        }},
        {"$sort": {"_id.year": 1, "_id.month": 1}},
    ]
    monthly_data = list(db.invoices.aggregate(monthly_pipeline))

    # ── Top vendors by invoice count ───────────────────────────────────────
    top_vendors_pipeline = [
        {"$match": {"is_deleted": {"$ne": True}}},
        {"$group": {
            "_id":          "$vendor_name",
            "count":        {"$sum": 1},
            "total_amount": {"$sum": "$total_amount"},
        }},
        {"$sort": {"count": -1}},
        {"$limit": 8},
    ]
    top_vendors = list(db.invoices.aggregate(top_vendors_pipeline))

    # ── Recent invoices (last 10) ──────────────────────────────────────────
    recent_invoices = list(
        db.invoices.find(
            {"is_deleted": {"$ne": True}},
            {"invoice_number": 1, "vendor_name": 1, "total_amount": 1,
             "currency_symbol": 1, "risk_flag": 1, "status": 1,
             "upload_timestamp": 1},
        ).sort("upload_timestamp", -1).limit(10)
    )
    for inv in recent_invoices:
        inv["_id"] = str(inv["_id"])
        if hasattr(inv.get("upload_timestamp"), "strftime"):
            inv["upload_timestamp"] = inv["upload_timestamp"].strftime("%b %d, %Y")

    # ── Recent audit activity ──────────────────────────────────────────────
    recent_activity = AuditLog.get_recent(limit=10)

    return render_template(
        "dashboard.html",
        stats=stats,
        monthly_data=monthly_data,
        top_vendors=top_vendors,
        recent_invoices=recent_invoices,
        recent_activity=recent_activity,
    )
