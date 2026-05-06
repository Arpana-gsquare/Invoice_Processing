"""
Admin Blueprint - User Management
===================================
Only users with role 'admin' can access these routes.

Routes:
  GET  /admin/users              - list all users (including inactive)
  POST /admin/users/create       - create a new user
  POST /admin/users/<id>/role    - change a user's role
  POST /admin/users/<id>/toggle  - activate / deactivate
  POST /admin/users/<id>/delete  - soft-delete (deactivate) a user
"""
from flask import (Blueprint, render_template, request,
                   redirect, url_for, flash, jsonify)
from flask_login import login_required, current_user

from ...models.user import User, ROLES
from ...models.audit import AuditLog

admin_bp = Blueprint("admin", __name__)


def _require_admin():
    if not current_user.is_admin():
        flash("Access denied. Admin only.", "danger")
        return redirect(url_for("dashboard.index"))
    return None


# -- User List ----------------------------------------------------------------
@admin_bp.route("/users")
@login_required
def users():
    gate = _require_admin()
    if gate:
        return gate
    all_users = User.list_all(include_inactive=True)
    return render_template(
        "admin/users.html",
        users=all_users,
        roles=ROLES,
        current_user=current_user,
    )


# -- Create User --------------------------------------------------------------
@admin_bp.route("/users/create", methods=["POST"])
@login_required
def create_user():
    gate = _require_admin()
    if gate:
        return gate

    name     = request.form.get("name", "").strip()
    email    = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()
    role     = request.form.get("role", "L1").strip()

    if not all([name, email, password, role]):
        flash("All fields (name, email, password, role) are required.", "danger")
        return redirect(url_for("admin.users"))

    try:
        user = User.create(email=email, password=password, name=name, role=role)
        AuditLog.log(
            invoice_id="system",
            action="user_created",
            actor_id=current_user.id,
            actor_name=current_user.name,
            details={"new_user": email, "role": role},
        )
        flash("User '%s' (%s) created successfully." % (name, role), "success")
    except ValueError as exc:
        flash(str(exc), "danger")

    return redirect(url_for("admin.users"))


# -- Change Role --------------------------------------------------------------
@admin_bp.route("/users/<user_id>/role", methods=["POST"])
@login_required
def change_role(user_id: str):
    gate = _require_admin()
    if gate:
        return gate

    user = User.get_by_id(user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("admin.users"))

    new_role = request.form.get("role", "").strip()
    try:
        old_role = user.role
        user.update_role(new_role)
        AuditLog.log(
            invoice_id="system",
            action="user_updated",
            actor_id=current_user.id,
            actor_name=current_user.name,
            details={"user": user.email, "old_role": old_role, "new_role": new_role},
        )
        flash("Role updated to %s for %s." % (new_role, user.name), "success")
    except ValueError as exc:
        flash(str(exc), "danger")

    return redirect(url_for("admin.users"))


# -- Toggle Active / Inactive -------------------------------------------------
@admin_bp.route("/users/<user_id>/toggle", methods=["POST"])
@login_required
def toggle_active(user_id: str):
    gate = _require_admin()
    if gate:
        return gate

    user = User.get_by_id(user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("admin.users"))

    if user.id == current_user.id:
        flash("You cannot deactivate your own account.", "warning")
        return redirect(url_for("admin.users"))

    new_state = not user.is_active
    user.set_active(new_state)
    label = "activated" if new_state else "deactivated"
    AuditLog.log(
        invoice_id="system",
        action="user_updated",
        actor_id=current_user.id,
        actor_name=current_user.name,
        details={"user": user.email, "action": label},
    )
    flash("User %s has been %s." % (user.name, label), "info")
    return redirect(url_for("admin.users"))


# -- Soft Delete --------------------------------------------------------------
@admin_bp.route("/users/<user_id>/delete", methods=["POST"])
@login_required
def delete_user(user_id: str):
    gate = _require_admin()
    if gate:
        return gate

    user = User.get_by_id(user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("admin.users"))

    if user.id == current_user.id:
        flash("You cannot delete your own account.", "warning")
        return redirect(url_for("admin.users"))

    user.soft_delete()
    AuditLog.log(
        invoice_id="system",
        action="user_deleted",
        actor_id=current_user.id,
        actor_name=current_user.name,
        details={"user": user.email},
    )
    flash("User %s has been deactivated." % user.name, "warning")
    return redirect(url_for("admin.users"))
