"""
Invoice Model - MongoDB schema with nested line items, status history, and soft delete.

New fields vs original:
  status_history  - embedded array of every status change
  is_deleted      - soft delete flag
  deleted_at      - when moved to recycle bin
  deleted_by      - user_id who deleted
  deleted_by_name - display name
  retention_days  - how long to keep in bin
  permanent_delete_at - TTL index fires on this field
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Any
from bson import ObjectId
from ..extensions import get_db

CATEGORIES = (
    "utilities", "travel", "office_supplies", "software",
    "hardware", "professional_services", "marketing", "logistics",
    "maintenance", "other",
)
STATUSES = ("pending", "approved", "rejected")
RISK_FLAGS = ("LOW RISK", "MODERATE", "HIGH RISK", "DUPLICATE")


class Invoice:
    def __init__(self, doc: dict):
        self._doc = doc

    @property
    def id(self) -> str:
        return str(self._doc["_id"])

    @property
    def invoice_number(self) -> str:
        return self._doc.get("invoice_number", "")

    @property
    def vendor_name(self) -> str:
        return self._doc.get("vendor_name", "")

    @property
    def total_amount(self) -> float:
        return float(self._doc.get("total_amount", 0))

    @property
    def risk_flag(self) -> str:
        return self._doc.get("risk_flag", "LOW RISK")

    @property
    def status(self) -> str:
        return self._doc.get("status", "pending")

    @property
    def is_deleted(self) -> bool:
        return bool(self._doc.get("is_deleted", False))

    @property
    def status_history(self) -> list:
        return self._doc.get("status_history", [])

    @property
    def days_since_invoice(self):
        inv_date = self._doc.get("invoice_date")
        if inv_date:
            if inv_date.tzinfo is None:
                inv_date = inv_date.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - inv_date
            return delta.days
        return None

    @property
    def is_overdue(self) -> bool:
        due = self._doc.get("due_date")
        if due:
            due_aware = due.replace(tzinfo=timezone.utc) if due.tzinfo is None else due
            return due_aware < datetime.now(timezone.utc) and self.status == "pending"
        return False

    # ── CRUD ──────────────────────────────────────────────────────────────
    @classmethod
    def create(cls, data: dict) -> "Invoice":
        db = get_db()
        data.setdefault("upload_timestamp", datetime.now(timezone.utc))
        data.setdefault("status", "pending")
        data.setdefault("status_history", [])
        data.setdefault("risk_flag", "LOW RISK")
        data.setdefault("risk_score", 0)
        data.setdefault("risk_reasons", [])
        data.setdefault("attachments", [])
        data.setdefault("is_deleted", False)
        data.setdefault("deleted_at", None)
        data.setdefault("deleted_by", None)
        data.setdefault("deleted_by_name", None)
        data.setdefault("retention_days", None)
        data.setdefault("permanent_delete_at", None)
        result = db.invoices.insert_one(data)
        data["_id"] = result.inserted_id
        return cls(data)

    @classmethod
    def get_by_id(cls, invoice_id: str, include_deleted: bool = False):
        try:
            query = {"_id": ObjectId(invoice_id)}
            if not include_deleted:
                query["is_deleted"] = {"$ne": True}
            doc = get_db().invoices.find_one(query)
        except Exception:
            return None
        return cls(doc) if doc else None

    @classmethod
    def get_by_number(cls, invoice_number: str) -> list:
        docs = get_db().invoices.find(
            {"invoice_number": invoice_number, "is_deleted": {"$ne": True}}
        )
        return [cls(d) for d in docs]

    @classmethod
    def list_all(cls, filters=None, sort_by="upload_timestamp",
                 sort_dir=-1, page=1, per_page=20):
        db = get_db()
        query = {**(filters or {}), "is_deleted": {"$ne": True}}
        total = db.invoices.count_documents(query)
        skip = (page - 1) * per_page
        docs = db.invoices.find(query).sort(sort_by, sort_dir).skip(skip).limit(per_page)
        return [cls(d) for d in docs], total

    @classmethod
    def list_deleted(cls, page=1, per_page=20):
        db = get_db()
        query = {"is_deleted": True}
        total = db.invoices.count_documents(query)
        skip = (page - 1) * per_page
        docs = db.invoices.find(query).sort("deleted_at", -1).skip(skip).limit(per_page)
        return [cls(d) for d in docs], total

    @classmethod
    def vendor_history(cls, vendor_name: str) -> list:
        docs = get_db().invoices.find(
            {
                "vendor_name": {"$regex": vendor_name, "$options": "i"},
                "is_deleted": {"$ne": True},
            },
            {"total_amount": 1, "invoice_date": 1, "status": 1, "due_date": 1},
        )
        return list(docs)

    @classmethod
    def amount_statistics(cls) -> dict:
        pipeline = [
            {"$match": {"is_deleted": {"$ne": True}}},
            {"$group": {
                "_id": None,
                "mean": {"$avg": "$total_amount"},
                "count": {"$sum": 1},
                "amounts": {"$push": "$total_amount"},
            }}
        ]
        result = list(get_db().invoices.aggregate(pipeline))
        if not result:
            return {"mean": 0, "std": 0, "count": 0}
        data = result[0]
        amounts = data["amounts"]
        mean = data["mean"]
        if len(amounts) > 1:
            variance = sum((x - mean) ** 2 for x in amounts) / len(amounts)
            std = variance ** 0.5
        else:
            std = 0
        return {"mean": mean, "std": std, "count": data["count"]}

    def update(self, updates: dict) -> "Invoice":
        get_db().invoices.update_one({"_id": self._doc["_id"]}, {"$set": updates})
        self._doc.update(updates)
        return self

    def add_attachment(self, file_path: str):
        get_db().invoices.update_one(
            {"_id": self._doc["_id"]},
            {"$push": {"attachments": file_path}},
        )

    # ── Status History ─────────────────────────────────────────────────────
    def push_status_history(self, from_status, to_status,
                            changed_by, changed_by_name, reason=""):
        entry = {
            "from_status": from_status,
            "to_status": to_status,
            "changed_by": changed_by,
            "changed_by_name": changed_by_name,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc),
        }
        get_db().invoices.update_one(
            {"_id": self._doc["_id"]},
            {"$push": {"status_history": entry}},
        )
        self._doc.setdefault("status_history", []).append(entry)

    # ── Soft Delete / Restore / Hard Delete ────────────────────────────────
    def soft_delete(self, deleted_by: str, deleted_by_name: str, retention_days: int = 30):
        now = datetime.now(timezone.utc)
        updates = {
            "is_deleted": True,
            "deleted_at": now,
            "deleted_by": deleted_by,
            "deleted_by_name": deleted_by_name,
            "retention_days": retention_days,
            "permanent_delete_at": now + timedelta(days=retention_days),
        }
        get_db().invoices.update_one({"_id": self._doc["_id"]}, {"$set": updates})
        self._doc.update(updates)

    def restore(self):
        updates = {
            "is_deleted": False,
            "deleted_at": None,
            "deleted_by": None,
            "deleted_by_name": None,
            "retention_days": None,
            "permanent_delete_at": None,
        }
        get_db().invoices.update_one({"_id": self._doc["_id"]}, {"$set": updates})
        self._doc.update(updates)

    def hard_delete(self):
        get_db().invoices.delete_one({"_id": self._doc["_id"]})

    def delete(self):
        self.hard_delete()

    # ── Serialisation ──────────────────────────────────────────────────────
    def to_dict(self, full: bool = True) -> dict:
        doc = self._doc.copy()
        doc["_id"] = str(doc["_id"])
        for field in ("invoice_date", "due_date", "upload_timestamp",
                      "approved_at", "rejected_at", "deleted_at", "permanent_delete_at"):
            if doc.get(field) and hasattr(doc[field], "isoformat"):
                doc[field] = doc[field].isoformat()
        # Serialise timestamps inside status_history
        for entry in doc.get("status_history", []):
            if entry.get("timestamp") and hasattr(entry["timestamp"], "isoformat"):
                entry["timestamp"] = entry["timestamp"].isoformat()
        if not full:
            doc.pop("raw_text", None)
            doc.pop("line_items", None)
        doc["days_since_invoice"] = self.days_since_invoice
        doc["is_overdue"] = self.is_overdue
        return doc
