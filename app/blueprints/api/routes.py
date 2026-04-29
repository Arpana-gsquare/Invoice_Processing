"""
REST API Blueprint - JSON endpoints for programmatic access.
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
from ...services.workflow_service import transition_status, WorkflowError
from ...services.recycle_service import (
    soft_delete, restore, permanent_delete, RETENTION_OPTIONS, DEFAULT_RETENTION
)
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


# -- Invoices -----------------------------------------------------------------
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
    """
    Unified status transition endpoint.
    Body: { "status": "approved"|"rejected"|"pending", "reason": "..." }
    """
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        return err("Invoice not found", 404)

    body = request.get_json(silent=True) or {}
    new_status = body.get("status", "").strip()
    reason = body.get("reason", "")

    if new_status not in ("approved", "rejected", "pending"):
        return err("Invalid status. Must be one of: approved, rejected, pending")

    if new_status in ("approved", "rejected") and not current_user.can_approve():
        return err("Insufficient permissions to approve/reject", 403)

    try:
        transition_status(invoice, new_status, current_user.id, current_user.name, reason)
    except WorkflowError as e:
        return err(str(e), 422)

    return ok({"status": new_status, "invoice_id": invoice_id})


# -- Recycle Bin API ----------------------------------------------------------
@api_bp.route("/invoices/<invoice_id>/delete", methods=["DELETE"])
@api_login_required
def api_soft_delete(invoice_id: str):
    """Soft-delete (move to recycle bin). Admin only."""
    if not current_user.is_admin():
        return err("Admin access required", 403)
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        return err("Invoice not found", 404)
    body = request.get_json(silent=True) or {}
    retention = int(body.get("retention_days", DEFAULT_RETENTION))
    try:
        soft_delete(invoice, current_user.id, current_user.name, retention)
    except ValueError as e:
        return err(str(e), 422)
    return ok({"moved_to_recycle_bin": True, "retention_days": retention})


@api_bp.route("/recycle-bin", methods=["GET"])
@api_login_required
def api_recycle_bin():
    """List all soft-deleted invoices."""
    if not current_user.is_admin():
        return err("Admin access required", 403)
    page = int(request.args.get("page", 1))
    per_page = min(int(request.args.get("per_page", 20)), 100)
    invoices, total = Invoice.list_deleted(page=page, per_page=per_page)
    return ok({
        "invoices": [inv.to_dict(full=False) for inv in invoices],
        "pagination": paginate(total, page, per_page),
    })


@api_bp.route("/recycle-bin/<invoice_id>/restore", methods=["POST"])
@api_login_required
def api_restore(invoice_id: str):
    """Restore an invoice from the recycle bin."""
    if not current_user.is_admin():
        return err("Admin access required", 403)
    invoice = Invoice.get_by_id(invoice_id, include_deleted=True)
    if not invoice or not invoice.is_deleted:
        return err("Invoice not found in recycle bin", 404)
    try:
        restore(invoice, current_user.id, current_user.name)
    except ValueError as e:
        return err(str(e), 422)
    return ok({"restored": True, "invoice_id": invoice_id})


@api_bp.route("/recycle-bin/<invoice_id>", methods=["DELETE"])
@api_login_required
def api_permanent_delete(invoice_id: str):
    """Permanently destroy an invoice from the recycle bin."""
    if not current_user.is_admin():
        return err("Admin access required", 403)
    invoice = Invoice.get_by_id(invoice_id, include_deleted=True)
    if not invoice or not invoice.is_deleted:
        return err("Invoice not found in recycle bin", 404)
    try:
        permanent_delete(invoice, current_user.id, current_user.name)
    except ValueError as e:
        return err(str(e), 422)
    return ok({"permanently_deleted": True})


# -- Upload via API -----------------------------------------------------------
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
        AuditLog.log(
            invoice_id=invoice.id,
            action="upload",
            actor_id=current_user.id,
            actor_name=current_user.name,
            details={
                "filename": original_name,
                "confidence": extracted.get("extraction_confidence"),
                "risk_flag": risk["risk_flag"],
            },
        )
        return ok(invoice.to_dict(), 201)
    except Exception as exc:
        return err(str(exc), 500)


# -- Analytics ----------------------------------------------------------------
@api_bp.route("/analytics/summary", methods=["GET"])
@api_login_required
def analytics_summary():
    db = get_db()
    pipeline = [
        {"$match": {"is_deleted": {"$ne": True}}},
        {"$group": {
            "_id": "$status",
            "count": {"$sum": 1},
            "total_amount": {"$sum": "$total_amount"},
        }},
    ]
    status_breakdown = {
        r["_id"]: {"count": r["count"], "total_amount": r["total_amount"]}
        for r in db.invoices.aggregate(pipeline)
    }

    risk_pipeline = [
        {"$match": {"is_deleted": {"$ne": True}}},
        {"$group": {"_id": "$risk_flag", "count": {"$sum": 1}}},
    ]
    risk_breakdown = {r["_id"]: r["count"] for r in db.invoices.aggregate(risk_pipeline)}

    stats = Invoice.amount_statistics()

    return ok({
        "status_breakdown": status_breakdown,
        "risk_breakdown": risk_breakdown,
        "amount_stats": stats,
    })


@api_bp.route("/analytics/vendor/<vendor_name>", methods=["GET"])
@api_login_required
def vendor_analytics(vendor_name: str):
    history = Invoice.vendor_history(vendor_name)
    serialised = []
    for doc in history:
        doc["_id"] = str(doc["_id"])
        for field in ("invoice_date", "due_date"):
            if doc.get(field) and hasattr(doc[field], "isoformat"):
                doc[field] = doc[field].isoformat()
        serialised.append(doc)
    return ok({"vendor": vendor_name, "history": serialised})


# -- Audit Trail --------------------------------------------------------------
@api_bp.route("/invoices/<invoice_id>/audit", methods=["GET"])
@api_login_required
def get_audit_trail(invoice_id: str):
    logs = AuditLog.get_for_invoice(invoice_id)
    return ok({"audit_trail": logs})
