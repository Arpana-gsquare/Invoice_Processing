"""
REST API Blueprint – JSON endpoints for programmatic access.
All responses follow:  { "success": bool, "data": {...} | [...], "error": str }
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps

from bson import ObjectId
from flask import Blueprint, jsonify, request, g
from flask_login import current_user, login_required

from ...extensions import get_db
from ...models.invoice import Invoice
from ...models.audit import AuditLog
from ...services.gemini_service import gemini_service
from ...services.fraud_detection import analyse_invoice
from ...utils.helpers import build_filters, paginate
from ...utils.validators import allowed_file, validate_invoice_data
from ...utils.helpers import save_uploaded_file

api_bp = Blueprint("api", __name__)


def api_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({"success": False, "error": "Authentication required"}), 401
        return f(*args, **kwargs)
    return decorated


def ok(data=None, status=200):
    return jsonify({"success": True, "data": data}), status


def err(message: str, status: int = 400):
    return jsonify({"success": False, "error": message}), status


# ── Invoices ───────────────────────────────────────────────────────────────
@api_bp.route("/invoices", methods=["GET"])
@api_login_required
def get_invoices():
    page = int(request.args.get("page", 1))
    per_page = min(int(request.args.get("per_page", 20)), 100)
    filters = build_filters(request.args)
    invoices, total = Invoice.list_all(filters=filters, page=page, per_page=per_page)
    return ok({
        "invoices": [inv.to_dict(full=False) for inv in invoices],
        "pagination": paginate(total, page, per_page),
    })


@api_bp.route("/invoices/<invoice_id>", methods=["GET"])
@api_login_required
def get_invoice(invoice_id: str):
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        return err("Invoice not found", 404)
    return ok(invoice.to_dict())


@api_bp.route("/invoices/<invoice_id>", methods=["PATCH"])
@api_login_required
def update_invoice(invoice_id: str):
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        return err("Invoice not found", 404)

    allowed_editable = {"notes", "payment_terms", "po_number", "category"}
    body = request.get_json(silent=True) or {}
    updates = {k: v for k, v in body.items() if k in allowed_editable}

    if not updates:
        return err("No editable fields provided")

    invoice.update(updates)
    AuditLog.log(invoice_id=invoice_id, action="edit",
                 actor_id=current_user.id, actor_name=current_user.name,
                 details={"fields": list(updates.keys())})
    return ok(invoice.to_dict())


@api_bp.route("/invoices/<invoice_id>/status", methods=["POST"])
@api_login_required
def change_status(invoice_id: str):
    if not current_user.can_approve():
        return err("Insufficient permissions", 403)
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        return err("Invoice not found", 404)

    body = request.get_json(silent=True) or {}
    new_status = body.get("status")
    if new_status not in ("approved", "rejected", "pending"):
        return err("Invalid status")

    updates = {"status": new_status}
    if new_status == "approved":
        updates["approved_by"] = current_user.id
        updates["approved_at"] = datetime.now(timezone.utc)
    elif new_status == "rejected":
        updates["rejected_by"] = current_user.id
        updates["rejected_at"] = datetime.now(timezone.utc)
        updates["rejection_reason"] = body.get("reason", "")

    invoice.update(updates)
    AuditLog.log(invoice_id=invoice_id, action=new_status,
                 actor_id=current_user.id, actor_name=current_user.name)
    return ok({"status": new_status})


# ── Upload via API ─────────────────────────────────────────────────────────
@api_bp.route("/invoices/upload", methods=["POST"])
@api_login_required
def api_upload():
    file = request.files.get("invoice")
    if not file or file.filename == "":
        return err("No file provided")
    if not allowed_file(file.filename):
        return err("Unsupported file type. Use PDF, JPG, or PNG.")

    try:
        file_path, original_name = save_uploaded_file(file)
        ext = original_name.rsplit(".", 1)[-1].lower()
        extracted = gemini_service.extract_invoice(file_path, ext)
        validate_invoice_data(extracted)
        doc = {
            **extracted,
            "file_path": file_path,
            "original_filename": original_name,
            "file_type": ext,
            "upload_timestamp": datetime.now(timezone.utc),
            "uploaded_by": current_user.id,
        }
        risk = analyse_invoice(doc)
        doc.update(risk)
        invoice = Invoice.create(doc)
        AuditLog.log(invoice_id=invoice.id, action="upload",
                     actor_id=current_user.id, actor_name=current_user.name,
                     details={"filename": original_name})
        return ok(invoice.to_dict(), status=201)
    except Exception as exc:
        return err(str(exc), 500)


# ── Stats / Dashboard ─────────────────────────────────────────────────────
@api_bp.route("/stats", methods=["GET"])
@api_login_required
def stats():
    db = get_db()
    pipeline = [
        {"$group": {
            "_id": "$risk_flag",
            "count": {"$sum": 1},
            "total_amount": {"$sum": "$total_amount"},
        }}
    ]
    risk_breakdown = {d["_id"]: {"count": d["count"], "total": d["total_amount"]}
                      for d in db.invoices.aggregate(pipeline)}

    status_pipeline = [
        {"$group": {"_id": "$status", "count": {"$sum": 1}}}
    ]
    status_breakdown = {d["_id"]: d["count"]
                        for d in db.invoices.aggregate(status_pipeline)}

    return ok({
        "total_invoices": db.invoices.count_documents({}),
        "risk_breakdown": risk_breakdown,
        "status_breakdown": status_breakdown,
        "overdue": db.invoices.count_documents({
            "status": "pending",
            "due_date": {"$lt": datetime.now(timezone.utc)},
        }),
    })


# ── Audit Trail ───────────────────────────────────────────────────────────
@api_bp.route("/invoices/<invoice_id>/audit", methods=["GET"])
@api_login_required
def audit_trail(invoice_id: str):
    return ok(AuditLog.get_for_invoice(invoice_id))


# ── Vendors ───────────────────────────────────────────────────────────────
@api_bp.route("/vendors", methods=["GET"])
@api_login_required
def vendors():
    db = get_db()
    pipeline = [
        {"$group": {
            "_id": "$vendor_name",
            "invoice_count": {"$sum": 1},
            "total_amount": {"$sum": "$total_amount"},
            "risk_flags": {"$push": "$risk_flag"},
        }},
        {"$sort": {"invoice_count": -1}},
        {"$limit": 50},
    ]
    result = list(db.invoices.aggregate(pipeline))
    for v in result:
        from collections import Counter
        flag_counts = Counter(v["risk_flags"])
        v["risk_breakdown"] = dict(flag_counts)
        del v["risk_flags"]
    return ok(result)
