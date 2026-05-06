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

        # Mark as processed after AI extraction
        invoice.update({"workflow_status": "processed", "status": "pending"})

        # Auto PO matching — routes invoice to correct workflow state
        try:
            from ...models.approval_workflow import ApprovalWorkflow
            from ...services.workflow_service import advance_workflow

            po_result = match_invoice_to_po(invoice.to_dict())
            invoice.update({
                "po_id":           po_result["po_id"],
                "po_match_status": po_result["po_match_status"],
                "match_score":     po_result["match_score"],
                "match_details":   po_result["match_details"],
            })
            po_status = po_result["po_match_status"]
            logger.info("PO match for %s: %s (score %d)",
                        original_name, po_status, po_result["match_score"])

            if po_status == "full":
                # Full match → initiate 3-level workflow → pending_L1
                ApprovalWorkflow.initiate(
                    invoice_id=invoice.id,
                    initiated_by=current_user.id,
                    initiated_by_name=current_user.name,
                )
                advance_workflow(invoice, "pending_L1",
                                 current_user.id, current_user.name,
                                 "Full PO match — auto-initiated")
                invoice.update({"workflow_level": 1})

            elif po_status == "partial":
                # Partial match → manual_review
                advance_workflow(invoice, "manual_review",
                                 current_user.id, current_user.name,
                                 "Partial PO match — manual review required")

            else:
                # No match → missing_po
                advance_workflow(invoice, "missing_po",
                                 current_user.id, current_user.name,
                                 "No PO found")

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
                "po_match_status":       invoice._doc.get("po_match_status", "none"),
                "workflow_status":       invoice._doc.get("workflow_status", "processed"),
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


# -- Export -----------------------------------------------------------------------

@invoices_bp.route("/export")
@login_required
def export():
    """Export all (non-deleted) invoices as CSV or Excel."""
    fmt = request.args.get("fmt", "csv").lower()
    invoices, _ = Invoice.list_all(per_page=10_000)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if fmt == "excel":
        data, mimetype, filename = (
            export_excel(invoices),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "invoices_%s.xlsx" % timestamp,
        )
    else:
        data, mimetype, filename = (
            export_csv(invoices),
            "text/csv",
            "invoices_%s.csv" % timestamp,
        )

    from flask import Response
    return Response(
        data,
        mimetype=mimetype,
        headers={"Content-Disposition": "attachment; filename=%s" % filename},
    )


# -- Approval Workflow (3-level: L1 → L2 → L3) --------------------------------

@invoices_bp.route("/<invoice_id>/workflow")
@login_required
def workflow_status(invoice_id: str):
    """JSON: full workflow state for the stepper modal."""
    from ...models.approval_workflow import ApprovalWorkflow
    wf = ApprovalWorkflow.get_by_invoice(invoice_id)
    invoice = Invoice.get_by_id(invoice_id)
    inv_wf  = invoice.workflow_status if invoice else None
    return jsonify({
        "workflow":         wf.to_dict() if wf else None,
        "workflow_status":  inv_wf,
    })


def _get_invoice_and_wf(invoice_id: str):
    """Helper: load invoice + workflow, return (invoice, wf) or (None, None)."""
    from ...models.approval_workflow import ApprovalWorkflow
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        return None, None
    wf = ApprovalWorkflow.get_by_invoice(invoice_id)
    return invoice, wf


@invoices_bp.route("/<invoice_id>/approve/L1", methods=["POST"])
@login_required
def approve_l1(invoice_id: str):
    """L1 approval. Only L1 role (or admin) can act."""
    if not current_user.can_approve_level(1):
        flash("Only an L1 approver can perform this action.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    from ...models.approval_workflow import ApprovalWorkflow
    from ...services.workflow_service import advance_workflow

    invoice = Invoice.get_by_id(invoice_id)
    if not invoice or invoice.workflow_status != "pending_L1":
        flash("Invoice is not in pending_L1 state.", "warning")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    wf = ApprovalWorkflow.get_by_invoice(invoice_id)
    if not wf:
        flash("No approval workflow found.", "warning")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    comments = request.form.get("comments", "").strip()
    wf.approve_level(1, current_user.id, current_user.name, comments)
    invoice.push_approval_history("L1", current_user.id, current_user.name,
                                  "approved", comments)
    advance_workflow(invoice, "pending_L2", current_user.id, current_user.name,
                     "L1 approved")
    invoice.update({"workflow_level": 2})
    flash("L1 approved. Invoice is now awaiting L2 approval.", "success")
    AuditLog.log(invoice_id=invoice_id, action="approved_L1",
                 actor_id=current_user.id, actor_name=current_user.name,
                 details={"comments": comments})
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


@invoices_bp.route("/<invoice_id>/approve/L2", methods=["POST"])
@login_required
def approve_l2(invoice_id: str):
    """L2 approval. Only L2 role (or admin) can act."""
    if not current_user.can_approve_level(2):
        flash("Only an L2 approver can perform this action.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    from ...models.approval_workflow import ApprovalWorkflow
    from ...services.workflow_service import advance_workflow

    invoice = Invoice.get_by_id(invoice_id)
    if not invoice or invoice.workflow_status != "pending_L2":
        flash("Invoice is not in pending_L2 state.", "warning")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    wf = ApprovalWorkflow.get_by_invoice(invoice_id)
    if not wf:
        flash("No approval workflow found.", "warning")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    comments = request.form.get("comments", "").strip()
    wf.approve_level(2, current_user.id, current_user.name, comments)
    invoice.push_approval_history("L2", current_user.id, current_user.name,
                                  "approved", comments)
    advance_workflow(invoice, "pending_L3", current_user.id, current_user.name,
                     "L2 approved")
    invoice.update({"workflow_level": 3})
    flash("L2 approved. Invoice is now awaiting L3 (final) approval.", "success")
    AuditLog.log(invoice_id=invoice_id, action="approved_L2",
                 actor_id=current_user.id, actor_name=current_user.name,
                 details={"comments": comments})
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


@invoices_bp.route("/<invoice_id>/approve/L3", methods=["POST"])
@login_required
def approve_l3(invoice_id: str):
    """L3 final approval. Only L3 role (or admin) can act."""
    if not current_user.can_approve_level(3):
        flash("Only an L3 approver can perform this action.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    from ...models.approval_workflow import ApprovalWorkflow
    from ...services.workflow_service import advance_workflow

    invoice = Invoice.get_by_id(invoice_id)
    if not invoice or invoice.workflow_status != "pending_L3":
        flash("Invoice is not in pending_L3 state.", "warning")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    wf = ApprovalWorkflow.get_by_invoice(invoice_id)
    if not wf:
        flash("No approval workflow found.", "warning")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    comments = request.form.get("comments", "").strip()
    wf.approve_level(3, current_user.id, current_user.name, comments)
    invoice.push_approval_history("L3", current_user.id, current_user.name,
                                  "approved", comments)
    advance_workflow(invoice, "approved", current_user.id, current_user.name,
                     "L3 final approval")
    flash("Invoice fully approved by L3.", "success")
    AuditLog.log(invoice_id=invoice_id, action="approved_L3",
                 actor_id=current_user.id, actor_name=current_user.name,
                 details={"comments": comments})
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


@invoices_bp.route("/<invoice_id>/reject/<level>", methods=["POST"])
@login_required
def reject_level(invoice_id: str, level: str):
    """Reject at a given approval level (L1/L2/L3) -> sends back to manual_review."""
    level_map = {"L1": 1, "L2": 2, "L3": 3}
    lvl = level_map.get(level.upper())
    if not lvl or not current_user.can_approve_level(lvl):
        flash(f"You don't have permission to reject at level {level}.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    from ...models.approval_workflow import ApprovalWorkflow
    from ...services.workflow_service import advance_workflow

    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))

    reason = request.form.get("reason", "").strip()
    expected_state = f"pending_{level.upper()}"
    if invoice.workflow_status != expected_state:
        flash(f"Invoice is not in {expected_state} state.", "warning")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    wf = ApprovalWorkflow.get_by_invoice(invoice_id)
    if wf:
        wf.reject_level(lvl, current_user.id, current_user.name, reason)
    invoice.push_approval_history(level.upper(), current_user.id, current_user.name,
                                  "rejected", reason)
    advance_workflow(invoice, "manual_review", current_user.id, current_user.name,
                     reason or f"Rejected at {level}")
    flash(f"Invoice rejected at {level} and sent for manual review.", "warning")
    AuditLog.log(invoice_id=invoice_id, action=f"rejected_{level.upper()}",
                 actor_id=current_user.id, actor_name=current_user.name,
                 details={"reason": reason})
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


@invoices_bp.route("/<invoice_id>/send-to-l1", methods=["POST"])
@login_required
def send_to_l1(invoice_id: str):
    """Send a manual_review or missing_po invoice to pending_L1."""
    from ...models.approval_workflow import ApprovalWorkflow
    from ...services.workflow_service import advance_workflow

    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))

    if invoice.workflow_status not in ("manual_review", "missing_po", "processed"):
        flash("Invoice cannot be sent to L1 from its current state.", "warning")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    wf = ApprovalWorkflow.get_by_invoice(invoice_id)
    if not wf:
        ApprovalWorkflow.initiate(invoice_id, current_user.id, current_user.name)

    advance_workflow(invoice, "pending_L1", current_user.id, current_user.name,
                     "Manually sent to L1 review")
    invoice.update({"workflow_level": 1})
    flash("Invoice sent to L1 for review.", "success")
    AuditLog.log(invoice_id=invoice_id, action="sent_to_L1",
                 actor_id=current_user.id, actor_name=current_user.name)
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))




@invoices_bp.route("/<invoice_id>/mark-ready", methods=["POST"])
@login_required
def mark_ready_for_payment(invoice_id: str):
    """Mark an approved invoice as ready for payment. Admin only."""
    if not current_user.is_admin():
        flash("Only admins can mark invoices as ready for payment.", "danger")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    from ...services.workflow_service import advance_workflow

    invoice = Invoice.get_by_id(invoice_id)
    if not invoice or invoice.workflow_status != "approved":
        flash("Invoice must be in approved state first.", "warning")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    advance_workflow(invoice, "ready_for_payment", current_user.id, current_user.name,
                     "Marked ready for payment")
    flash("Invoice is now marked as Ready for Payment.", "success")
    AuditLog.log(invoice_id=invoice_id, action="marked_ready_for_payment",
                 actor_id=current_user.id, actor_name=current_user.name)
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))
