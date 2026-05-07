"""
Purchase Order Blueprint
Routes:
  GET  /                          - list all POs
  GET  /upload                    - upload form
  POST /upload                    - process PO file
  GET  /<po_id>                   - PO detail
  POST /<po_id>/delete            - hard delete (admin only)
  POST /invoice/<invoice_id>/match-po  - manually trigger match
  GET  /invoice/<invoice_id>/comparison - comparison view
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, jsonify)
from flask_login import login_required, current_user

from ...models.purchase_order import PurchaseOrder
from ...models.invoice import Invoice
from ...models.audit import AuditLog
from ...services.gemini_service import gemini_service
from ...services.po_matching_service import match_invoice_to_po, compare_invoice_to_po
from ...utils.validators import allowed_file
from ...utils.helpers import save_uploaded_file, paginate

logger = logging.getLogger(__name__)
po_bp = Blueprint("po", __name__)


# -- PO List ------------------------------------------------------------------
@po_bp.route("/")
@login_required
def list_pos():
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 20))
    pos, total = PurchaseOrder.list_all(page=page, per_page=per_page)
    pagination = paginate(total, page, per_page)
    return render_template("po/list.html", pos=pos, pagination=pagination)


# -- PO Upload ----------------------------------------------------------------
@po_bp.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "GET":
        return render_template("po/upload.html")

    files = request.files.getlist("pos")
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
        result = _process_single_po_upload(file)
        results.append(result)

    ok_list   = [r for r in results if r["success"]]
    fail_list = [r for r in results if not r["success"]]
    if ok_list:
        flash("%d PO(s) uploaded and processed successfully." % len(ok_list), "success")
    for r in fail_list:
        flash("Failed '%s': %s" % (r["filename"], r["error"]), "danger")

    return redirect(url_for("po.list_pos"))


def _process_single_po_upload(file) -> dict:
    try:
        file_path, original_name = save_uploaded_file(file)
        ext = original_name.rsplit(".", 1)[-1].lower()
        logger.info("Processing PO upload: %s", original_name)

        extracted = gemini_service.extract_po(file_path, ext)

        doc = {
            **extracted,
            "file_path":         file_path,
            "original_filename": original_name,
            "file_type":         ext,
            "upload_timestamp":  datetime.now(timezone.utc),
            "uploaded_by":       current_user.id,
        }

        # Parse po_date string -> datetime
        po_date_str = doc.get("po_date")
        if po_date_str and isinstance(po_date_str, str):
            try:
                doc["po_date"] = datetime.strptime(po_date_str, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc)
            except ValueError:
                doc["po_date"] = None

        po = PurchaseOrder.create(doc)
        AuditLog.log(
            invoice_id="po:%s" % po.id,
            action="po_upload",
            actor_id=current_user.id,
            actor_name=current_user.name,
            details={
                "filename":   original_name,
                "po_number":  po.po_number,
                "vendor":     po.vendor_name,
                "confidence": extracted.get("extraction_confidence"),
            },
        )
        return {"success": True, "filename": original_name, "po_id": po.id}

    except Exception as exc:
        logger.exception("PO upload failed for %s: %s", file.filename, exc)
        return {"success": False, "filename": file.filename, "error": str(exc)}


# -- PO Delete ----------------------------------------------------------------
@po_bp.route("/<po_id>/delete", methods=["POST"])
@login_required
def delete_po(po_id: str):
    """Hard delete a PO — admin only, no recycle bin."""
    if not current_user.is_admin():
        flash("Only administrators can delete Purchase Orders.", "danger")
        return redirect(url_for("po.list_pos"))
    po = PurchaseOrder.get_by_id(po_id)
    if not po:
        flash("Purchase Order not found.", "danger")
        return redirect(url_for("po.list_pos"))
    po_number = po.po_number or po_id
    from ...extensions import get_db
    from bson import ObjectId
    get_db().purchase_orders.delete_one({"_id": ObjectId(po_id)})
    AuditLog.log(
        invoice_id="po:%s" % po_id,
        action="po_deleted",
        actor_id=current_user.id,
        actor_name=current_user.name,
        details={"po_number": po_number, "vendor": po.vendor_name},
    )
    flash("Purchase Order '%s' permanently deleted." % po_number, "warning")
    return redirect(url_for("po.list_pos"))


# -- PO Detail ----------------------------------------------------------------
@po_bp.route("/<po_id>")
@login_required
def detail(po_id: str):
    po = PurchaseOrder.get_by_id(po_id)
    if not po:
        flash("Purchase Order not found.", "danger")
        return redirect(url_for("po.list_pos"))
    return render_template("po/detail.html", po=po, po_dict=po.to_dict())


# -- Manual Match Trigger -----------------------------------------------------
@po_bp.route("/invoice/<invoice_id>/match-po", methods=["POST"])
@login_required
def match_po(invoice_id: str):
    """Re-run PO matching for an existing invoice.
    After updating PO data, regenerates proposal insights so that the
    po_alignment section reflects the newly matched PO.
    """
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))

    match_result = match_invoice_to_po(invoice.to_dict())
    invoice.update({
        "po_id":           match_result["po_id"],
        "po_match_status": match_result["po_match_status"],
        "match_score":     match_result["match_score"],
        "match_details":   match_result["match_details"],
    })
    AuditLog.log(
        invoice_id=invoice_id,
        action="po_match",
        actor_id=current_user.id,
        actor_name=current_user.name,
        details={
            "po_id":  match_result["po_id"],
            "status": match_result["po_match_status"],
            "score":  match_result["match_score"],
        },
    )

    # Regenerate proposal insights with updated PO context so that
    # po_alignment section reflects the newly matched (or cleared) PO.
    inv_dict    = invoice.to_dict()   # already has updated po_id in _doc
    proposal_id = inv_dict.get("proposal_id")
    if proposal_id:
        try:
            from ...services.proposal_matching_service import match_invoice_to_proposal
            prop_result = match_invoice_to_proposal(inv_dict)
            invoice.update({
                "proposal_id":           prop_result["proposal_id"],
                "proposal_match_status": prop_result["proposal_match_status"],
                "proposal_match_score":  prop_result["proposal_match_score"],
                "proposal_insights":     prop_result["proposal_insights"],
            })
            logger.info("Proposal insights regenerated after PO re-match for %s", invoice_id)
        except Exception as prop_exc:
            logger.warning("Proposal insight regen failed after PO re-match: %s", prop_exc)

    flash("PO matching complete: %s (score %d/100)" % (
        match_result["po_match_status"], match_result["match_score"]), "info")
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


# -- Comparison View ----------------------------------------------------------
@po_bp.route("/invoice/<invoice_id>/comparison")
@login_required
def comparison(invoice_id: str):
    invoice = Invoice.get_by_id(invoice_id)
    if not invoice:
        flash("Invoice not found.", "danger")
        return redirect(url_for("invoices.list_invoices"))

    inv_dict = invoice.to_dict()
    po_id    = inv_dict.get("po_id")
    po       = PurchaseOrder.get_by_id(po_id) if po_id else None

    comparison_data = None
    if po:
        comparison_data = compare_invoice_to_po(inv_dict, po)

    return render_template(
        "po/comparison.html",
        invoice=invoice,
        inv_dict=inv_dict,
        po=po,
        po_dict=po.to_dict() if po else None,
        comparison=comparison_data,
    )


# -- Helper used by proposals blueprint ---------------------------------------
def _get_po_dict_for_invoice(inv_dict: dict):
    """
    Return the matched PO as a plain dict, or None.
    Used by the proposals blueprint when regenerating Gemini insights
    so PO context can be passed into the three-way comparison.
    """
    po_id = inv_dict.get("po_id")
    if not po_id:
        return None
    po = PurchaseOrder.get_by_id(po_id)
    return po.to_dict() if po else None
