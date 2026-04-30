"""
Flask Application Factory
"""
import os
from flask import Flask
from .config import get_config
from .extensions import init_mongo, login_manager


def create_app(config_class=None):
    app = Flask(__name__, template_folder="templates", static_folder="static")

    cfg = config_class or get_config()
    app.config.from_object(cfg)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    init_mongo(app)
    login_manager.init_app(app)

    from .models.user import User

    @login_manager.user_loader
    def load_user(user_id):
        return User.get_by_id(user_id)

    from .blueprints.auth.routes import auth_bp
    from .blueprints.dashboard.routes import dashboard_bp
    from .blueprints.invoices.routes import invoices_bp
    from .blueprints.api.routes import api_bp
    from .blueprints.recycle.routes import recycle_bp
    from .blueprints.po.routes import po_bp
    from .blueprints.proposals.routes import proposals_bp

    app.register_blueprint(auth_bp,       url_prefix="/auth")
    app.register_blueprint(dashboard_bp,  url_prefix="/")
    app.register_blueprint(invoices_bp,   url_prefix="/invoices")
    app.register_blueprint(api_bp,        url_prefix="/api/v1")
    app.register_blueprint(recycle_bp,    url_prefix="/recycle-bin")
    app.register_blueprint(po_bp,         url_prefix="/purchase-orders")
    app.register_blueprint(proposals_bp,  url_prefix="/proposals")

    with app.app_context():
        _seed_admin(app)

    app.jinja_env.globals["APP_NAME"] = app.config["APP_NAME"]
    return app


def _seed_admin(app):
    from .models.user import User
    if not User.get_by_email(app.config["ADMIN_EMAIL"]):
        User.create(
            email=app.config["ADMIN_EMAIL"],
            password=app.config["ADMIN_PASSWORD"],
            name="System Admin",
            role="admin",
        )
