"""
MongoDB extension - single MongoClient shared across the app.
"""
from flask_login import LoginManager
from pymongo import MongoClient, ASCENDING, DESCENDING

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message_category = "warning"

_mongo_client = None
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
    """Idempotent index creation - safe to call on every startup."""
    # Invoices - core
    db.invoices.create_index(
        [("invoice_number", ASCENDING), ("vendor_name", ASCENDING)],
        unique=False, name="inv_num_vendor",
    )
    db.invoices.create_index([("upload_timestamp", DESCENDING)], name="upload_ts_desc")
    db.invoices.create_index([("risk_flag", ASCENDING)],         name="risk_flag_asc")
    db.invoices.create_index([("status", ASCENDING)],            name="status_asc")
    db.invoices.create_index([("vendor_name", ASCENDING)],       name="vendor_name_asc")
    db.invoices.create_index([("invoice_date", DESCENDING)],     name="inv_date_desc")
    db.invoices.create_index([("po_match_status", ASCENDING)],   name="po_match_status_asc")

    # Soft-delete: speeds up all "active only" queries
    db.invoices.create_index([("is_deleted", ASCENDING)], name="is_deleted_asc")

    # TTL index: auto-purge soft-deleted invoices at permanent_delete_at datetime.
    # expireAfterSeconds=0 means "expire AT the stored datetime value".
    db.invoices.create_index(
        [("permanent_delete_at", ASCENDING)],
        expireAfterSeconds=0,
        partialFilterExpression={"is_deleted": True},
        name="recycle_bin_ttl",
    )

    # Purchase Orders
    db.purchase_orders.create_index([("po_number", ASCENDING)],       name="po_number_asc")
    db.purchase_orders.create_index([("vendor_name", ASCENDING)],     name="po_vendor_asc")
    db.purchase_orders.create_index([("upload_timestamp", DESCENDING)], name="po_upload_ts_desc")
    db.purchase_orders.create_index(
        [("vendor_name", ASCENDING), ("total_amount", ASCENDING)],
        name="po_vendor_amount",
    )
