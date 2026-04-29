"""
Dashboard Blueprint – KPI overview & activity feed.
"""
from datetime import datetime, timezone, timedelta
from flask import Blueprint, render_template
from flask_login import login_required, current_user
from ...extensions import get_db
from ...models.audit import AuditLog

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
@login_required
def index():
    db = get_db()

    # ── KPI Cards ─────────────────────────────────────────────────────────
    total_invoices = db.invoices.count_documents({})
    pending = db.invoices.count_documents({"status": "pending"})
    approved = db.invoices.count_documents({"status": "approved"})
    rejected = db.invoices.count_documents({"status": "rejected"})

    # Risk breakdown
    safe_count = db.invoices.count_documents({"risk_flag": "SAFE"})
    moderate_count = db.invoices.count_documents({"risk_flag": "MODERATE"})
    high_risk_count = db.invoices.count_documents({"risk_flag": "HIGH RISK"})
    duplicate_count = db.invoices.count_documents({"risk_flag": "DUPLICATE"})

    # Financial KPIs
    total_amount_pipeline = [
        {"$group": {"_id": None, "total": {"$sum": "$total_amount"}}}
    ]
    total_amount_result = list(db.invoices.aggregate(total_amount_pipeline))
    total_amount = total_amount_result[0]["total"] if total_amount_result else 0

    approved_amount_pipeline = [
        {"$match": {"status": "approved"}},
        {"$group": {"_id": None, "total": {"$sum": "$total_amount"}}},
    ]
    approved_amount_result = list(db.invoices.aggregate(approved_amount_pipeline))
    approved_amount = approved_amount_result[0]["total"] if approved_amount_result else 0

    # Overdue invoices
    now = datetime.now(timezone.utc)
    overdue = db.invoices.count_documents({
        "status": "pending",
        "due_date": {"$lt": now},
    })

    # Monthly trend (last 6 months)
    six_months_ago = now - timedelta(days=180)
    monthly_pipeline = [
        {"$match": {"upload_timestamp": {"$gte": six_months_ago}}},
        {"$group": {
            "_id": {
                "year": {"$year": "$upload_timestamp"},
                "month": {"$month": "$upload_timestamp"},
            },
            "count": {"$sum": 1},
            "total": {"$sum": "$total_amount"},
        }},
        {"$sort": {"_id.year": 1, "_id.month": 1}},
    ]
    monthly_data = list(db.invoices.aggregate(monthly_pipeline))

    # Top vendors by invoice count
    top_vendors_pipeline = [
        {"$group": {
            "_id": "$vendor_name",
            "count": {"$sum": 1},
            "total_amount": {"$sum": "$total_amount"},
        }},
        {"$sort": {"count": -1}},
        {"$limit": 8},
    ]
    top_vendors = list(db.invoices.aggregate(top_vendors_pipeline))

    # Recent invoices (last 10)
    recent_invoices = list(
        db.invoices.find(
            {},
            {"invoice_number": 1, "vendor_name": 1, "total_amount": 1,
             "currency_symbol": 1, "risk_flag": 1, "status": 1,
             "upload_timestamp": 1},
        ).sort("upload_timestamp", -1).limit(10)
    )
    for inv in recent_invoices:
        inv["_id"] = str(inv["_id"])
        if hasattr(inv.get("upload_timestamp"), "isoformat"):
            inv["upload_timestamp"] = inv["upload_timestamp"].strftime("%b %d, %Y")

    # Recent audit activity
    recent_activity = AuditLog.get_recent(limit=10)

    stats = {
        "total_invoices": total_invoices,
        "pending": pending,
        "approved": approved,
        "rejected": rejected,
        "safe_count": safe_count,
        "moderate_count": moderate_count,
        "high_risk_count": high_risk_count,
        "duplicate_count": duplicate_count,
        "total_amount": total_amount,
        "approved_amount": approved_amount,
        "overdue": overdue,
    }

    return render_template(
        "dashboard.html",
        stats=stats,
        monthly_data=monthly_data,
        top_vendors=top_vendors,
        recent_invoices=recent_invoices,
        recent_activity=recent_activity,
    )
