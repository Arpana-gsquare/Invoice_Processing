"""
Proposals Blueprint
Routes:
  GET  /                                   - list proposals
  GET  /upload                             - upload form
  POST /upload                             - process proposal file
  GET  /<proposal_id>                      - proposal detail
  POST /invoice/<invoice_id>/match-proposal - manually trigger match
  GET  /invoice/<invoice_id>/insights      - AI comparison view
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import (Blueprint, render_template, request, redirect,
                   url_for, flash, jsonify)
from flask_login import login_required, current_user

from ...models.proposal import Proposal
from ...models.invoice import Invoice
from ...models.audit import AuditLog
from ...services.gemini_service import gemini_service
from ...services.proposal_matching_service import (
    match_invoice_to_proposal, get_comparison_data
)
from ...utils.validators import allowed_file
from ...utils.helpers import save_uploaded_file, paginate

logger = logging.getLogger(__name__)
proposals_bp = Blueprint("proposals", __name__)


# -- List ---------------------------------------------------------------------
@proposals_bp.route("/")
@login_required
def list_proposals():
    page     = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 20))
    proposals, total = Proposal.list_all(page=page, per_page=per_page)
    pagination = paginate(total, page, per_page)
    return render_template("proposals/list.html",
                           proposals=proposals, pagination=pagination)


# -- Upload -------------------------------------------------------------------
@proposals_bp.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "GET":
        return render_template("proposals/upload.html")

    files = request.files.getlist("proposals")
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
        results.append(_process_single_proposal(file))

    ok_list   = [r for r in results if r["success"]]
    fail_list = [r for r in results if not r["success"]]
    if ok_list:
        flash("%d proposal(s) uploaded and processed." % len(ok_list), "success")
    for r in fail_list:
        flash("Failed '%s': %s" % (r["filename"], r["error"]), "danger")

    return redirect(url_for("proposals.list_proposals"))


def _process_single_proposal(file) -> dict:
    try:
        file_path, original_name = save_uploaded_file(file)
        ext = original_name.rsplit(".", 1)[-1].lower()
        logger.info("Processing proposal upload: %s", original_name)

        extracted = gemini_service.extract_proposal(file_path, ext)

        # Parse date strings
        for field in ("proposal_date", "validity_date"):
            val = extracted.get(field)
            if val and isinstance(val, str):
                try:
                    extracted[field] = datetime.strptime(val, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc)
                except ValueError:
                    extracted[field] = None

        doc = {
            **extracted,
            "file_path":         file_path,
            "original_filename": original_name,
            "file_type":         ext,
            "upload_timestamp":  datetime.now(timezone.utc),
            "uploaded_by":       current_user.id,
        }

        proposal = Proposal.create(doc)
        AuditLog.log(
            invoice_id="proposal:%s" % proposal.id,
            action="proposal_upload",
            actor_id=current_user.id,
            actor_name=current_user.name,
            details={
                "filename":    original_name,
                "proposal_id": proposal.proposal_id,
                "vendor":      proposal.vendor_name,
                "confidence":  extracted.get("extraction_confidence"),
            },
        )
        return {"success": True, "filename": original_name, "proposal_id": proposal.id}

    except Exception as exc:
        logger.exception("Proposal upload failed for %s: %s", file.filename, exc)
        return {"success": False, "filename": file.filename, "error": str(exc)}


# -- Delete -------------------------------------------------------------------
@proposals_bp.route("/<proposal_id>/delete", methods=["POST"])
@login_required
def delete_proposal(proposal_id: str):
    """Hard delete a proposal — admin only, no recycle bin."""
    if not current_user.is_admin():
        flash("Only administrators can delete Proposals.", "danger")
        return redirect(url_for("proposals.list_proposals"))
    proposal = Proposal.get_by_id(proposal_id)
    if not proposal:
        flash("Proposal not found.", "danger")
        return redirect(url_for("proposals.list_proposals"))
    proposal_ref = proposal.proposal_id or proposal_id
    from ...extensions import get_db
    from bson import ObjectId
    get_db().proposals.delete_one({"_id": ObjectId(proposal_id)})
    AuditLog.log(
        invoice_id="proposal:%s" % proposal_id,
        action="proposal_deleted",
        actor_id=current_user.id,
        actor_name=current_user.name,
        details={"proposal_id": proposal_ref, "vendor": proposal.vendor_name},
    )
    flash("Proposal '%s' permanently deleted." % proposal_ref, "warning")
    return redirect(url_for("proposals.list_proposals"))


# -- Detail -------------------------------------------------------------------
@proposals_bp.route("/<proposal_id>")
@login_required
def detail(proposal_id: str):
    proposal = Proposal.get_by_id(proposal_id)
    if not proposal:
        flash("Proposal not found.", "danger")
        return redirect(url_for("proposals.list_proposals"))
    return render_template("proposals/detail.html",
                           proposal=proposal, proposal_dict=proposal.to_dict())


# -- Manual match trigger -----------------------------------------------------
@proposals_bp.route("/invoice/<invoice_id>/match-proposal", methods=["POST"])
@login_required
def match_proposal(invoice_id: str):
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))

    result = match_invoice_to_proposal(invoice.to_dict())
    invoice.update({
        "proposal_id":           result["proposal_id"],
        "proposal_match_status": result["proposal_match_status"],
        "proposal_match_score":  result["proposal_match_score"],
        "proposal_insights":     result["proposal_insights"],
    })
    AuditLog.log(
        invoice_id=invoice_id,
        action="proposal_match",
        actor_id=current_user.id,
        actor_name=current_user.name,
        details={
            "proposal_id": result["proposal_id"],
            "status":      result["proposal_match_status"],
            "score":       result["proposal_match_score"],
        },
    )
    flash("Proposal matching complete: %s (score %d/100)" % (
        result["proposal_match_status"], result["proposal_match_score"]), "info")
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


# -- AI Insights view ---------------------------------------------------------
@proposals_bp.route("/invoice/<invoice_id>/insights")
@login_required
def insights(invoice_id: str):
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))

    inv_dict    = invoice.to_dict()
    proposal_id = inv_dict.get("proposal_id")
    proposal    = Proposal.get_by_id(proposal_id) if proposal_id else None

    comparison  = None
    if proposal:
        comparison = get_comparison_data(inv_dict, proposal)

    stored_insights = inv_dict.get("proposal_insights") or {}

    return render_template(
        "proposals/insights.html",
        invoice=invoice,
        inv_dict=inv_dict,
        proposal=proposal,
        proposal_dict=proposal.to_dict() if proposal else None,
        comparison=comparison,
        insights=stored_insights,
    )


# -- Regenerate insights (AJAX / form POST) -----------------------------------
@proposals_bp.route("/invoice/<invoice_id>/insights/regenerate", methods=["POST"])
@login_required
def regenerate_insights(invoice_id: str):
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))

    inv_dict    = invoice.to_dict()
    proposal_id = inv_dict.get("proposal_id")
    proposal    = Proposal.get_by_id(proposal_id) if proposal_id else None

    if not proposal:
        flash("No proposal linked — run matching first.", "warning")
        return redirect(url_for("proposals.insights", invoice_id=invoice_id))

    from ..po.routes import _get_po_dict_for_invoice
    po_dict = _get_po_dict_for_invoice(inv_dict)

    try:
        new_insights = gemini_service.generate_proposal_insights(
            invoice=inv_dict, proposal=proposal.to_dict(), po=po_dict)
        invoice.update({"proposal_insights": new_insights})
        flash("AI insights regenerated successfully.", "success")
    except Exception as exc:
        flash("Insight generation failed: %s" % exc, "danger")

    return redirect(url_for("proposals.insights", invoice_id=invoice_id))
