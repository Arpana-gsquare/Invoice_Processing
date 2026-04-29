"""
Flask Application Factory
"""
import os
from flask import Flask
from .config import get_config
from .extensions import init_mongo, login_manager


def create_app(config_class=None):
    app = Flask(__name__, template_folder="templates", static_folder="static")

    # ── Config ─────────────────────────────────────────────────────────────
    cfg = config_class or get_config()
    app.config.from_object(cfg)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    # ── Extensions ─────────────────────────────────────────────────────────
    init_mongo(app)
    login_manager.init_app(app)

    # ── User Loader ────────────────────────────────────────────────────────
    from .models.user import User

    @login_manager.user_loader
    def load_user(user_id):
        return User.get_by_id(user_id)

    # ── Blueprints ─────────────────────────────────────────────────────────
    from .blueprints.auth.routes import auth_bp
    from .blueprints.dashboard.routes import dashboard_bp
    from .blueprints.invoices.routes import invoices_bp
    from .blueprints.api.routes import api_bp

    app.register_blueprint(auth_bp,      url_prefix="/auth")
    app.register_blueprint(dashboard_bp, url_prefix="/")
    app.register_blueprint(invoices_bp,  url_prefix="/invoices")
    app.register_blueprint(api_bp,       url_prefix="/api/v1")

    # ── Seed Admin User ────────────────────────────────────────────────────
    with app.app_context():
        _seed_admin(app)

    # ── Jinja Globals ──────────────────────────────────────────────────────
    app.jinja_env.globals["APP_NAME"] = app.config["APP_NAME"]

    return app


def _seed_admin(app):
    """Create the default admin account if it doesn't exist."""
    from .models.user import User
    if not User.get_by_email(app.config["ADMIN_EMAIL"]):
        User.create(
            email=app.config["ADMIN_EMAIL"],
            password=app.config["ADMIN_PASSWORD"],
            name="System Admin",
            role="admin",
        )
