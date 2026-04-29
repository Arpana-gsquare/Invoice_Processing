"""
Invoices Blueprint – Upload, List, Detail, Approve/Reject, Export, Attachments.
"""
from __future__ import annotations

import os
import logging
from datetime import datetime, timezone

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, send_file, current_app, jsonify)
from flask_login import login_required, current_user

from ...models.invoice import Invoice
from ...models.audit import AuditLog
from ...services.gemini_service import gemini_service
from ...services.fraud_detection import analyse_invoice
from ...services.export_service import export_csv, export_excel
from ...utils.validators import allowed_file, validate_invoice_data
from ...utils.helpers import save_uploaded_file, paginate, build_filters

logger = logging.getLogger(__name__)
invoices_bp = Blueprint("invoices", __name__)


# ── List & Filter ──────────────────────────────────────────────────────────
@invoices_bp.route("/")
@login_required
def list_invoices():
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 20))
    filters = build_filters(request.args)
    invoices, total = Invoice.list_all(filters=filters, page=page, per_page=per_page)
    pagination = paginate(total, page, per_page)
    return render_template(
        "invoices/list.html",
        invoices=invoices,
        pagination=pagination,
        filters=request.args,
    )


# ── Upload ─────────────────────────────────────────────────────────────────
@invoices_bp.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "GET":
        return render_template("invoices/upload.html")

    files = request.files.getlist("invoices")
    if not files or all(f.filename == "" for f in files):
        flash("No files selected.", "warning")
        return redirect(request.url)

    results = []
    for file in files:
        if file.filename == "":
            continue
        if not allowed_file(file.filename):
            flash(f"'{file.filename}' – unsupported format. Use PDF, JPG, or PNG.", "warning")
            continue
        result = _process_single_upload(file)
        results.append(result)

    ok = [r for r in results if r["success"]]
    fail = [r for r in results if not r["success"]]
    if ok:
        flash(f"{len(ok)} invoice(s) uploaded and processed successfully.", "success")
    if fail:
        for r in fail:
            flash(f"Failed '{r['filename']}': {r['error']}", "danger")

    return redirect(url_for("invoices.list_invoices"))


def _process_single_upload(file) -> dict:
    """Upload, extract, validate, risk-score, and save one invoice."""
    try:
        file_path, original_name = save_uploaded_file(file)
        ext = original_name.rsplit(".", 1)[-1].lower()
        logger.info("Processing upload: %s", original_name)

        # AI Extraction
        extracted = gemini_service.extract_invoice(file_path, ext)

        # Validation & Normalisation
        validate_invoice_data(extracted)

        # Assemble the document
        doc = {
            **extracted,
            "file_path": file_path,
            "original_filename": original_name,
            "file_type": ext,
            "upload_timestamp": datetime.now(timezone.utc),
            "uploaded_by": current_user.id,
        }

        # Risk Analysis
        risk = analyse_invoice(doc)
        doc.update(risk)

        # Persist
        invoice = Invoice.create(doc)

        # Audit
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

        return {"success": True, "filename": original_name, "invoice_id": invoice.id}

    except Exception as exc:
        logger.exception("Upload failed for %s: %s", file.filename, exc)
        return {"success": False, "filename": file.filename, "error": str(exc)}


# ── Detail View ────────────────────────────────────────────────────────────
@invoices_bp.route("/<invoice_id>")
@login_required
def detail(invoice_id: str):
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))
    audit_trail = AuditLog.get_for_invoice(invoice_id)
    return render_template(
        "invoices/detail.html",
        invoice=invoice,
        invoice_dict=invoice.to_dict(),
        audit_trail=audit_trail,
    )


# ── Approve / Reject Workflow ──────────────────────────────────────────────
@invoices_bp.route("/<invoice_id>/approve", methods=["POST"])
@login_required
def approve(invoice_id: str):
    if not current_user.can_approve():
        flash("You do not have permission to approve invoices.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))

    invoice.update({
        "status": "approved",
        "approved_by": current_user.id,
        "approved_at": datetime.now(timezone.utc),
    })
    AuditLog.log(invoice_id=invoice_id, action="approve",
                 actor_id=current_user.id, actor_name=current_user.name)
    flash("Invoice approved.", "success")
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


@invoices_bp.route("/<invoice_id>/reject", methods=["POST"])
@login_required
def reject(invoice_id: str):
    if not current_user.can_approve():
        flash("You do not have permission to reject invoices.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))

    reason = request.form.get("reason", "")
    invoice.update({
        "status": "rejected",
        "rejected_by": current_user.id,
        "rejected_at": datetime.now(timezone.utc),
        "rejection_reason": reason,
    })
    AuditLog.log(
        invoice_id=invoice_id, action="reject",
        actor_id=current_user.id, actor_name=current_user.name,
        details={"reason": reason},
    )
    flash("Invoice rejected.", "warning")
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


# ── Re-analyse ─────────────────────────────────────────────────────────────
@invoices_bp.route("/<invoice_id>/reanalyse", methods=["POST"])
@login_required
def reanalyse(invoice_id: str):
    """Re-run fraud detection on an existing invoice."""
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))

    risk = analyse_invoice(invoice.to_dict(), existing_id=invoice_id)
    invoice.update(risk)
    AuditLog.log(invoice_id=invoice_id, action="flag_risk",
                 actor_id=current_user.id, actor_name=current_user.name,
                 details={"new_flag": risk["risk_flag"]})
    flash(f"Risk re-analysed: {risk['risk_flag']}", "info")
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


# ── Attachment Upload ──────────────────────────────────────────────────────
@invoices_bp.route("/<invoice_id>/attach", methods=["POST"])
@login_required
def attach(invoice_id: str):
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))

    file = request.files.get("attachment")
    if not file or file.filename == "":
        flash("No file selected.", "warning")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    file_path, original_name = save_uploaded_file(file)
    invoice.add_attachment(file_path)
    AuditLog.log(invoice_id=invoice_id, action="add_attachment",
                 actor_id=current_user.id, actor_name=current_user.name,
                 details={"filename": original_name})
    flash(f"Attachment '{original_name}' added.", "success")
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


# ── Delete ─────────────────────────────────────────────────────────────────
@invoices_bp.route("/<invoice_id>/delete", methods=["POST"])
@login_required
def delete(invoice_id: str):
    if not current_user.is_admin():
        flash("Only admins can delete invoices.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))

    AuditLog.log(invoice_id=invoice_id, action="delete",
                 actor_id=current_user.id, actor_name=current_user.name,
                 details={"invoice_number": invoice.invoice_number})
    invoice.delete()
    flash("Invoice deleted.", "warning")
    return redirect(url_for("invoices.list_invoices"))


# ── Export ─────────────────────────────────────────────────────────────────
@invoices_bp.route("/export/<fmt>")
@login_required
def export(fmt: str):
    filters = build_filters(request.args)
    invoices, _ = Invoice.list_all(filters=filters, page=1, per_page=10_000)

    AuditLog.log(invoice_id="bulk", action="export",
                 actor_id=current_user.id, actor_name=current_user.name,
                 details={"format": fmt, "count": len(invoices)})

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if fmt == "csv":
        buf = export_csv(invoices)
        return send_file(
            buf, mimetype="text/csv",
            as_attachment=True, download_name=f"invoices_{timestamp}.csv",
        )
    elif fmt == "excel":
        buf = export_excel(invoices)
        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True, download_name=f"invoices_{timestamp}.xlsx",
        )
    else:
        flash("Unsupported export format.", "danger")
        return redirect(url_for("invoices.list_invoices"))
