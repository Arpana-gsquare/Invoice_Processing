"""
Auth Blueprint – Login, Logout, User Management
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user
from ...models.user import User, ROLES

auth_bp = Blueprint("auth", __name__, template_folder="../../templates/auth")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember"))

        user = User.get_by_email(email)
        if user and user.verify_password(password):
            login_user(user, remember=remember)
            user.update_last_login()
            next_page = request.args.get("next")
            flash(f"Welcome back, {user.name}!", "success")
            return redirect(next_page or url_for("dashboard.index"))
        flash("Invalid email or password.", "danger")

    return render_template("auth/login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/users", methods=["GET"])
@login_required
def users():
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard.index"))
    all_users = User.list_all()
    return render_template("auth/users.html", users=all_users, roles=ROLES)


@auth_bp.route("/users/create", methods=["POST"])
@login_required
def create_user():
    if not current_user.is_admin():
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard.index"))

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    name = request.form.get("name", "").strip()
    role = request.form.get("role", "auditor")

    if not all([email, password, name, role]):
        flash("All fields are required.", "danger")
        return redirect(url_for("auth.users"))

    if User.get_by_email(email):
        flash(f"User {email} already exists.", "warning")
        return redirect(url_for("auth.users"))

    try:
        User.create(email=email, password=password, name=name, role=role)
        flash(f"User '{name}' created successfully.", "success")
    except ValueError as e:
        flash(str(e), "danger")

    return redirect(url_for("auth.users"))
