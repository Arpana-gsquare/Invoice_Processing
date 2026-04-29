"""
Shared extensions (MongoDB client, login manager, etc.)
Initialised in create_app() to avoid circular imports.
"""
from pymongo import MongoClient, ASCENDING, DESCENDING
from flask_login import LoginManager

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message_category = "warning"

_mongo_client: MongoClient | None = None
_db = None


def init_mongo(app):
    """Create the MongoClient, attach the database, and ensure indexes."""
    global _mongo_client, _db
    _mongo_client = MongoClient(app.config["MONGODB_URI"])
    _db = _mongo_client[app.config["MONGODB_DB_NAME"]]
    _create_indexes(_db)
    app.db = _db
    return _db


def get_db():
    return _db


def _create_indexes(db):
    """Idempotent index creation – safe to call on every startup."""
    # Invoices – core
    db.invoices.create_index([("invoice_number", ASCENDING), ("vendor_name", ASCENDING)],
                              unique=False, name="inv_num_vendor")
    db.invoices.create_index([("upload_timestamp", DESCENDING)], name="upload_ts_desc")
    db.invoices.create_index([("risk_flag", ASCENDING)], name="risk_flag_asc")
    db.invoices.create_index([("status", ASCENDING)], name="status_asc")
    db.invoices.create_index([("vendor_name", ASCENDING)], name="vendor_name_asc")
    db.invoices.create_index([("invoice_date", DESCENDING)], name="inv_date_desc")

    # Soft-delete filter – speeds up all "active only" queries
    db.invoices.create_index([("is_deleted", ASCENDING)], name="is_deleted_asc")

    # TTL index: auto-purge soft-deleted invoices when permanent_delete_at is reached.
    # expireAfterSeconds=0 means "expire AT the stored datetime value".
    # partialFilterExpression restricts the TTL to deleted documents only,
    # so active invoices with no permanent_delete_at are never touched.
    db.invoices.create_index(
        [("permanent_delete_at", ASCENDING)],
        expireAfterSeconds=0,
        partialFilterExpression={"is_deleted": True},
        name="recycle_bin_ttl",
    )

    # Users
    db.users.create_index([("email", ASCENDING)], unique=True, name="email_unique")

    # Audit trail
    db.audit_logs.create_index([("invoice_id", ASCENDING), ("timestamp", DESCENDING)],
                                name="audit_inv_ts")
    db.audit_logs.create_index([("timestamp", DESCENDING)], name="audit_ts_desc")
