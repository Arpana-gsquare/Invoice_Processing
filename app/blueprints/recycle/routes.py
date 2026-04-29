"""
Recycle Bin Blueprint – List, Restore, Permanent Delete
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user

from ...models.invoice import Invoice
from ...services.recycle_service import restore, permanent_delete, RETENTION_OPTIONS
from ...utils.helpers import paginate

recycle_bp = Blueprint("recycle", __name__)


@recycle_bp.route("/")
@login_required
def index():
    if not current_user.is_admin():
        flash("Only admins can access the Recycle Bin.", "danger")
        return redirect(url_for("dashboard.index"))

    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 20))
    invoices, total = Invoice.list_deleted(page=page, per_page=per_page)
    pagination = paginate(total, page, per_page)
    return render_template(
        "recycle_bin.html",
        invoices=invoices,
        pagination=pagination,
        retention_options=RETENTION_OPTIONS,
    )


@recycle_bp.route("/<invoice_id>/restore", methods=["POST"])
@login_required
def restore_invoice(invoice_id: str):
    if not current_user.is_admin():
        flash("Only admins can restore invoices.", "danger")
        return redirect(url_for("recycle.index"))

    invoice = Invoice.get_by_id(invoice_id, include_deleted=True)
    if not invoice or not invoice.is_deleted:
        flash("Invoice not found in Recycle Bin.", "danger")
        return redirect(url_for("recycle.index"))

    restore(invoice, actor_id=current_user.id, actor_name=current_user.name)
    flash(
        "Invoice '%s' has been restored successfully." % (invoice.invoice_number or invoice.id),
        "success",
    )
    return redirect(url_for("recycle.index"))


@recycle_bp.route("/<invoice_id>/permanent-delete", methods=["POST"])
@login_required
def purge(invoice_id: str):
    if not current_user.is_admin():
        flash("Only admins can permanently delete invoices.", "danger")
        return redirect(url_for("recycle.index"))

    invoice = Invoice.get_by_id(invoice_id, include_deleted=True)
    if not invoice or not invoice.is_deleted:
        flash("Invoice not found in Recycle Bin.", "danger")
        return redirect(url_for("recycle.index"))

    permanent_delete(invoice, actor_id=current_user.id, actor_name=current_user.name)
    flash("Invoice permanently deleted.", "warning")
    return redirect(url_for("recycle.index"))


@recycle_bp.route("/purge-all", methods=["POST"])
@login_required
def purge_all():
    """Permanently delete ALL items currently in the recycle bin."""
    if not current_user.is_admin():
        flash("Only admins can purge the recycle bin.", "danger")
        return redirect(url_for("recycle.index"))

    invoices, _ = Invoice.list_deleted(page=1, per_page=10_000)
    count = 0
    for inv in invoices:
        permanent_delete(inv, actor_id=current_user.id, actor_name=current_user.name)
        count += 1

    flash("Recycle Bin cleared. %d invoice(s) permanently deleted." % count, "warning")
    return redirect(url_for("recycle.index"))
