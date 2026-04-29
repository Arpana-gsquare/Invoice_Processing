"""
Centralised configuration for all environments.
"""
import os
from datetime import timedelta


class BaseConfig:
    # ── Flask ──────────────────────────────────────────────────────────────
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH_MB", 50)) * 1024 * 1024
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

    # ── MongoDB ────────────────────────────────────────────────────────────
    MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017/")
    MONGODB_DB_NAME = os.environ.get("MONGODB_DB_NAME", "invoice_processor")

    # ── Gemini AI ──────────────────────────────────────────────────────────
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-preview-04-17")

    # ── File Storage ───────────────────────────────────────────────────────
    STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local")   # 'local' | 's3'
    UPLOAD_FOLDER = os.path.join(os.getcwd(), os.environ.get("UPLOAD_FOLDER", "uploads"))
    ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png"}

    # ── AWS S3 (optional) ──────────────────────────────────────────────────
    AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    AWS_S3_BUCKET = os.environ.get("AWS_S3_BUCKET", "")
    AWS_S3_REGION = os.environ.get("AWS_S3_REGION", "us-east-1")

    # ── Risk Detection ─────────────────────────────────────────────────────
    ANOMALY_ZSCORE_THRESHOLD = float(os.environ.get("ANOMALY_ZSCORE_THRESHOLD", 2.5))
    HIGH_RISK_THRESHOLD = int(os.environ.get("HIGH_RISK_VENDOR_SCORE_THRESHOLD", 70))
    MODERATE_RISK_THRESHOLD = int(os.environ.get("MODERATE_RISK_VENDOR_SCORE_THRESHOLD", 40))

    # ── App Meta ───────────────────────────────────────────────────────────
    APP_NAME = os.environ.get("APP_NAME", "InvoiceIQ")
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@company.com")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Admin@123456")


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    TESTING = False


class ProductionConfig(BaseConfig):
    DEBUG = False
    TESTING = False


class TestingConfig(BaseConfig):
    DEBUG = True
    TESTING = True
    MONGODB_DB_NAME = "invoice_processor_test"


config_map = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}


def get_config():
    env = os.environ.get("FLASK_ENV", "development")
    return config_map.get(env, DevelopmentConfig)
