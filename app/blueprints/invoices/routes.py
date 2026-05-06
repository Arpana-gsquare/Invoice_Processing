"""
Invoices Blueprint - Upload, List, Detail, Approve/Reject, Export, Download, Attachments.
"""
from __future__ import annotations

import os
import logging
from datetime import datetime, timezone

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, send_file, current_app, jsonify, abort)
from flask_login import login_required, current_user

from ...models.invoice import Invoice
from ...models.audit import AuditLog
from ...services.gemini_service import gemini_service
from ...services.fraud_detection import analyse_invoice
from ...services.export_service import export_csv, export_excel
from ...services.workflow_service import transition_status, WorkflowError
from ...services.recycle_service import soft_delete, RETENTION_OPTIONS, DEFAULT_RETENTION
from ...services.po_matching_service import match_invoice_to_po
from ...services.proposal_matching_service import match_invoice_to_proposal
from ...utils.validators import allowed_file, validate_invoice_data
from ...utils.helpers import save_uploaded_file, paginate, build_filters

logger = logging.getLogger(__name__)
invoices_bp = Blueprint("invoices", __name__)


# -- List & Filter ------------------------------------------------------------
@invoices_bp.route("/")
@login_required
def list_invoices():
    page     = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 20))
    filters  = build_filters(request.args)
    invoices, total = Invoice.list_all(filters=filters, page=page, per_page=per_page)
    pagination = paginate(total, page, per_page)
    return render_template(
        "invoices/list.html",
        invoices=invoices,
        pagination=pagination,
        filters=request.args,
    )


# -- Upload -------------------------------------------------------------------
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
            flash("'%s' - unsupported format. Use PDF, JPG, or PNG." % file.filename, "warning")
            continue
        result = _process_single_upload(file)
        results.append(result)

    ok   = [r for r in results if r["success"]]
    fail = [r for r in results if not r["success"]]
    if ok:
        flash("%d invoice(s) uploaded and processed successfully." % len(ok), "success")
    for r in fail:
        flash("Failed '%s': %s" % (r["filename"], r["error"]), "danger")

    return redirect(url_for("invoices.list_invoices"))


def _process_single_upload(file) -> dict:
    """Upload, extract, validate, risk-score, PO-match, proposal-match, and save one invoice."""
    try:
        file_path, original_name = save_uploaded_file(file)
        ext = original_name.rsplit(".", 1)[-1].lower()
        logger.info("Processing upload: %s", original_name)

        extracted = gemini_service.extract_invoice(file_path, ext)
        validate_invoice_data(extracted)

        doc = {
            **extracted,
            "file_path":         file_path,
            "original_filename": original_name,
            "file_type":         ext,
            "upload_timestamp":  datetime.now(timezone.utc),
            "uploaded_by":       current_user.id,
        }

        risk = analyse_invoice(doc)
        doc.update(risk)

        invoice = Invoice.create(doc)

        # Auto PO matching (non-fatal) — score >= 50 → MATCHED → auto-initiate L1 workflow
        try:
            po_result = match_invoice_to_po(invoice.to_dict())
            invoice.update({
                "po_id":           po_result["po_id"],
                "po_match_status": po_result["po_match_status"],
                "match_score":     po_result["match_score"],
                "match_details":   po_result["match_details"],
            })
            logger.info("PO match for %s: %s (score %d)",
                        original_name, po_result["po_match_status"], po_result["match_score"])

            if po_result["po_match_status"] == "MATCHED":
                from ...models.approval_workflow import ApprovalWorkflow
                ApprovalWorkflow.initiate(
                    invoice_id=invoice.id,
                    initiated_by=current_user.id,
                    initiated_by_name=current_user.name,
                )
                invoice.update({"workflow_level": 1})
                logger.info("Approval workflow initiated for %s (PO matched)", original_name)
        except Exception as po_exc:
            logger.warning("PO matching failed for %s: %s", original_name, po_exc)

        # Auto Proposal matching (non-fatal)
        try:
            prop_result = match_invoice_to_proposal(invoice.to_dict())
            invoice.update({
                "proposal_id":           prop_result["proposal_id"],
                "proposal_match_status": prop_result["proposal_match_status"],
                "proposal_match_score":  prop_result["proposal_match_score"],
                "proposal_insights":     prop_result["proposal_insights"],
            })
            logger.info("Proposal match for %s: %s (score %d)",
                        original_name, prop_result["proposal_match_status"],
                        prop_result["proposal_match_score"])
        except Exception as prop_exc:
            logger.warning("Proposal matching failed for %s: %s", original_name, prop_exc)

        AuditLog.log(
            invoice_id=invoice.id,
            action="upload",
            actor_id=current_user.id,
            actor_name=current_user.name,
            details={
                "filename":              original_name,
                "confidence":            extracted.get("extraction_confidence"),
                "risk_flag":             risk["risk_flag"],
                "po_match_status":       invoice._doc.get("po_match_status", "NO_PO_FOUND"),
                "proposal_match_status": invoice._doc.get("proposal_match_status", "NO_PROPOSAL"),
            },
        )

        return {"success": True, "filename": original_name, "invoice_id": invoice.id}

    except Exception as exc:
        logger.exception("Upload failed for %s: %s", file.filename, exc)
        return {"success": False, "filename": file.filename, "error": str(exc)}


# -- AJAX Single-File Upload (used by batch/folder uploader) ------------------
@invoices_bp.route("/upload/single", methods=["POST"])
@login_required
def upload_single():
    """
    Process one file and return a JSON result.
    Called sequentially by the front-end batch uploader so the user sees
    live per-file progress.
    """
    file = request.files.get("invoice")
    if not file or file.filename == "":
        return jsonify({"success": False, "error": "No file provided"}), 400
    if not allowed_file(file.filename):
        return jsonify({
            "success": False,
            "filename": file.filename,
            "error": "Unsupported format. Use PDF, JPG, or PNG.",
        }), 400
    result = _process_single_upload(file)
    status_code = 200 if result["success"] else 500
    return jsonify(result), status_code


# -- Detail View --------------------------------------------------------------
@invoices_bp.route("/<invoice_id>")
@login_required
def detail(invoice_id: str):
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))
    audit_trail = AuditLog.get_for_invoice(invoice_id)

    # Preserve the filtered list URL so "Invoices" breadcrumb navigates back
    # to whatever filter the user came from (e.g. PROCEED tile, No PO tile, etc.)
    referrer = request.referrer or ""
    if referrer and "/invoices" in referrer and ("?" in referrer or referrer.rstrip("/").endswith("/invoices")):
        back_url = referrer
    else:
        back_url = url_for("invoices.list_invoices")

    return render_template(
        "invoices/detail.html",
        invoice=invoice,
        invoice_dict=invoice.to_dict(),
        audit_trail=audit_trail,
        back_url=back_url,
    )


# -- Download Original File ---------------------------------------------------
@invoices_bp.route("/<invoice_id>/download")
@login_required
def download(invoice_id: str):
    """Serve the original uploaded invoice file as a download."""
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        abort(404)

    inv_dict  = invoice.to_dict()
    file_path = inv_dict.get("file_path")
    if not file_path or not os.path.isfile(file_path):
        flash("Original file not found on disk.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    original_name = inv_dict.get("original_filename") or os.path.basename(file_path)
    ext = (original_name.rsplit(".", 1)[-1].lower()) if "." in original_name else "pdf"
    mimetype_map = {
        "pdf":  "application/pdf",
        "jpg":  "image/jpeg",
        "jpeg": "image/jpeg",
        "png":  "image/png",
    }
    mimetype = mimetype_map.get(ext, "application/octet-stream")

    AuditLog.log(
        invoice_id=invoice_id,
        action="download",
        actor_id=current_user.id,
        actor_name=current_user.name,
        details={"filename": original_name},
    )
    return send_file(
        file_path,
        mimetype=mimetype,
        as_attachment=True,
        download_name=original_name,
    )


# -- Unified Status Transition ------------------------------------------------
@invoices_bp.route("/<invoice_id>/transition", methods=["POST"])
@login_required
def transition(invoice_id: str):
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))

    new_status = request.form.get("new_status", "").strip()
    reason     = request.form.get("reason", "").strip()

    if new_status in ("approved", "rejected") and not current_user.can_approve():
        flash("You do not have permission to approve or reject invoices.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    try:
        transition_status(
            invoice=invoice,
            new_status=new_status,
            actor_id=current_user.id,
            actor_name=current_user.name,
            reason=reason,
        )
        labels = {
            "approved": ("Invoice approved.", "success"),
            "rejected": ("Invoice rejected.", "warning"),
            "pending":  ("Invoice reopened and set back to pending.", "info"),
        }
        msg, cat = labels.get(new_status, ("Status updated.", "info"))
        flash(msg, cat)
    except WorkflowError as e:
        flash(str(e), "danger")

    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


# -- Legacy aliases -----------------------------------------------------------
@invoices_bp.route("/<invoice_id>/approve", methods=["POST"])
@login_required
def approve(invoice_id: str):
    return _quick_transition(invoice_id, "approved")


@invoices_bp.route("/<invoice_id>/reject", methods=["POST"])
@login_required
def reject(invoice_id: str):
    return _quick_transition(invoice_id, "rejected", reason=request.form.get("reason", ""))


def _quick_transition(invoice_id, new_status, reason=""):
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))
    if not current_user.can_approve():
        flash("Permission denied.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))
    try:
        transition_status(invoice, new_status, current_user.id, current_user.name, reason)
        flash("Status updated to %s." % new_status,
              "success" if new_status == "approved" else "warning")
    except WorkflowError as e:
        flash(str(e), "danger")
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


# -- Re-analyse ---------------------------------------------------------------
@invoices_bp.route("/<invoice_id>/reanalyse", methods=["POST"])
@login_required
def reanalyse(invoice_id: str):
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))

    risk = analyse_invoice(invoice.to_dict(), existing_id=invoice_id)
    invoice.update(risk)
    AuditLog.log(invoice_id=invoice_id, action="flag_risk",
                 actor_id=current_user.id, actor_name=current_user.name,
                 details={"new_flag": risk["risk_flag"]})
    flash("Risk re-analysed: %s" % risk["risk_flag"], "info")
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


# -- Attachment Upload --------------------------------------------------------
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
    flash("Attachment '%s' added." % original_name, "success")
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


# -- Soft Delete -> Recycle Bin -----------------------------------------------
@invoices_bp.route("/<invoice_id>/delete", methods=["POST"])
@login_required
def delete(invoice_id: str):
    if not current_user.is_admin():
        flash("Only admins can move invoices to the recycle bin.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))

    try:
        retention = int(request.form.get("retention_days", DEFAULT_RETENTION))
    except (TypeError, ValueError):
        retention = DEFAULT_RETENTION

    soft_delete(
        invoice=invoice,
        actor_id=current_user.id,
        actor_name=current_user.name,
        retention_days=retention,
    )
    flash(
        "Invoice moved to Recycle Bin. "
        "It will be permanently deleted after %d days." % retention,
        "warning",
    )
    return redirect(url_for("invoices.list_invoices"))


# -- Approval Workflow --------------------------------------------------------

@invoices_bp.route("/<invoice_id>/workflow")
@login_required
def workflow_status(invoice_id: str):
    """
    JSON endpoint — returns the full workflow state for the stepper modal.
    GET /invoices/<id>/workflow
    """
    from ...models.approval_workflow import ApprovalWorkflow
    wf = ApprovalWorkflow.get_by_invoice(invoice_id)
    if not wf:
        return jsonify({"workflow": None})
    return jsonify({"workflow": wf.to_dict()})


@invoices_bp.route("/<invoice_id>/workflow/approve", methods=["POST"])
@login_required
def workflow_approve(invoice_id: str):
    """
    L1 (accountant) or L2 (admin) approval.
    Determines level from current workflow state + user role.
    """
    from ...models.approval_workflow import ApprovalWorkflow
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))

    wf = ApprovalWorkflow.get_by_invoice(invoice_id)
    if not wf:
        flash("No approval workflow exists for this invoice.", "warning")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    comments = request.form.get("comments", "").strip()
    level    = wf.current_level

    # Permission check
    if level == 1 and current_user.role not in ("accountant", "admin"):
        flash("Only an accountant or admin can approve at Level 1.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))
    if level == 2 and not current_user.is_admin():
        flash("Only an admin can approve at Level 2.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))
    if level is None:
        flash("This workflow has already been completed.", "info")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    completed = wf.approve_level(level, current_user.id, current_user.name, comments)

    if completed:
        # Both levels approved → mark invoice approved
        invoice.update({"workflow_level": None})
        try:
            transition_status(invoice, "approved", current_user.id, current_user.name,
                              "Approved via L1→L2 workflow")
        except WorkflowError:
            pass
        flash("Invoice fully approved through L1 & L2 workflow.", "success")
    else:
        # Advance to next level
        invoice.update({"workflow_level": 2})
        flash("L1 approved — invoice is now awaiting L2 (admin) approval.", "info")

    AuditLog.log(
        invoice_id=invoice_id,
        action="workflow_approve_l%d" % level,
        actor_id=current_user.id,
        actor_name=current_user.name,
        details={"level": level, "comments": comments, "completed": completed},
    )
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


@invoices_bp.route("/<invoice_id>/workflow/reject", methods=["POST"])
@login_required
def workflow_reject(invoice_id: str):
    """
    Reject at L1 or L2 — ends workflow, marks invoice rejected.
    """
    from ...models.approval_workflow import ApprovalWorkflow
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))

    wf = ApprovalWorkflow.get_by_invoice(invoice_id)
    if not wf:
        flash("No approval workflow exists for this invoice.", "warning")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    comments = request.form.get("comments", "").strip()
    level    = wf.current_level

    if level is None:
        flash("This workflow has already been completed.", "info")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    wf.reject_level(level, current_user.id, current_user.name, comments)
    invoice.update({"workflow_level": None})
    try:
        transition_status(invoice, "rejected", current_user.id, current_user.name,
                          comments or "Rejected via L%d workflow" % level)
    except WorkflowError:
        pass

    AuditLog.log(
        invoice_id=invoice_id,
        action="workflow_reject_l%d" % level,
        actor_id=current_user.id,
        actor_name=current_user.name,
        details={"level": level, "comments": comments},
    )
    flash("Invoice rejected at Level %d." % level, "warning")
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


# -- Export -------------------------------------------------------------------
@invoices_bp.route("/export/<fmt>")
@login_required
def export(fmt: str):
    filters  = build_filters(request.args)
    invoices, _ = Invoice.list_all(filters=filters, page=1, per_page=10_000)

    AuditLog.log(invoice_id="bulk", action="export",
                 actor_id=current_user.id, actor_name=current_user.name,
                 details={"format": fmt, "count": len(invoices)})

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if fmt == "csv":
        buf = export_csv(invoices)
        return send_file(
            buf, mimetype="text/csv",
            as_attachment=True, download_name="invoices_%s.csv" % timestamp,
        )
    elif fmt == "excel":
        buf = export_excel(invoices)
        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True, download_name="invoices_%s.xlsx" % timestamp,
        )
    else:
        flash("Unsupported export format.", "danger")
        return redirect(url_for("invoices.list_invoices"))
