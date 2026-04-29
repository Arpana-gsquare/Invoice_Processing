"""
Invoice Model – MongoDB schema design with nested line items.

Schema (stored as BSON document):
{
  _id: ObjectId,
  invoice_number: str,
  vendor_name: str,
  vendor_address: str,
  vendor_email: str,
  bill_to: str,
  invoice_date: datetime,
  due_date: datetime,
  total_amount: float,
  subtotal: float,
  tax_amount: float,
  currency: str,           # ISO 4217 (e.g. "USD")
  currency_symbol: str,
  line_items: [
    {
      description: str,
      quantity: float,
      unit_price: float,
      amount: float,
      category: str,       # utilities | travel | office_supplies | ...
    }
  ],
  extraction_confidence: float,   # 0-1
  raw_text: str,
  file_path: str,
  original_filename: str,
  file_type: str,          # pdf | jpg | png
  upload_timestamp: datetime,
  uploaded_by: str,        # user_id
  status: str,             # pending | approved | rejected
  risk_flag: str,          # SAFE | MODERATE | HIGH RISK | DUPLICATE
  risk_score: int,         # 0-100
  risk_reasons: [str],
  duplicate_of: str | None,
  category: str,           # inferred from line items
  attachments: [str],      # list of file paths
  notes: str,
  payment_terms: str,
  po_number: str,
  approved_by: str | None,
  approved_at: datetime | None,
  rejected_by: str | None,
  rejected_at: datetime | None,
  rejection_reason: str,
}
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from bson import ObjectId
from ..extensions import get_db


# ── Invoice Categories ─────────────────────────────────────────────────────
CATEGORIES = (
    "utilities", "travel", "office_supplies", "software",
    "hardware", "professional_services", "marketing", "logistics",
    "maintenance", "other",
)

# ── Workflow Statuses ──────────────────────────────────────────────────────
STATUSES = ("pending", "approved", "rejected")

# ── Risk Flags ─────────────────────────────────────────────────────────────
RISK_FLAGS = ("SAFE", "MODERATE", "HIGH RISK", "DUPLICATE")


class Invoice:
    """CRUD operations for invoices collection."""

    def __init__(self, doc: dict):
        self._doc = doc

    # ── Properties ─────────────────────────────────────────────────────────
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
        return self._doc.get("risk_flag", "SAFE")

    @property
    def status(self) -> str:
        return self._doc.get("status", "pending")

    @property
    def days_since_invoice(self) -> int | None:
        inv_date = self._doc.get("invoice_date")
        if inv_date:
            delta = datetime.now(timezone.utc) - inv_date.replace(tzinfo=timezone.utc) \
                if inv_date.tzinfo is None else datetime.now(timezone.utc) - inv_date
            return delta.days
        return None

    @property
    def is_overdue(self) -> bool:
        due = self._doc.get("due_date")
        if due:
            due_aware = due.replace(tzinfo=timezone.utc) if due.tzinfo is None else due
            return due_aware < datetime.now(timezone.utc) and self.status == "pending"
        return False

    # ── CRUD ───────────────────────────────────────────────────────────────
    @classmethod
    def create(cls, data: dict) -> "Invoice":
        db = get_db()
        data.setdefault("upload_timestamp", datetime.now(timezone.utc))
        data.setdefault("status", "pending")
        data.setdefault("risk_flag", "SAFE")
        data.setdefault("risk_score", 0)
        data.setdefault("risk_reasons", [])
        data.setdefault("attachments", [])
        result = db.invoices.insert_one(data)
        data["_id"] = result.inserted_id
        return cls(data)

    @classmethod
    def get_by_id(cls, invoice_id: str) -> "Invoice | None":
        try:
            doc = get_db().invoices.find_one({"_id": ObjectId(invoice_id)})
        except Exception:
            return None
        return cls(doc) if doc else None

    @classmethod
    def get_by_number(cls, invoice_number: str) -> list["Invoice"]:
        docs = get_db().invoices.find({"invoice_number": invoice_number})
        return [cls(d) for d in docs]

    @classmethod
    def list_all(
        cls,
        filters: dict | None = None,
        sort_by: str = "upload_timestamp",
        sort_dir: int = -1,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[list["Invoice"], int]:
        db = get_db()
        query = filters or {}
        total = db.invoices.count_documents(query)
        skip = (page - 1) * per_page
        docs = db.invoices.find(query).sort(sort_by, sort_dir).skip(skip).limit(per_page)
        return [cls(d) for d in docs], total

    @classmethod
    def vendor_history(cls, vendor_name: str) -> list[dict]:
        """Return all invoices for a vendor (for risk scoring)."""
        docs = get_db().invoices.find(
            {"vendor_name": {"$regex": vendor_name, "$options": "i"}},
            {"total_amount": 1, "invoice_date": 1, "status": 1, "due_date": 1},
        )
        return list(docs)

    @classmethod
    def amount_statistics(cls) -> dict:
        """Return global mean/std of invoice amounts for anomaly detection."""
        pipeline = [
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

    def delete(self):
        get_db().invoices.delete_one({"_id": self._doc["_id"]})

    def add_attachment(self, file_path: str):
        get_db().invoices.update_one(
            {"_id": self._doc["_id"]},
            {"$push": {"attachments": file_path}},
        )

    # ── Serialisation ──────────────────────────────────────────────────────
    def to_dict(self, full: bool = True) -> dict[str, Any]:
        doc = self._doc.copy()
        doc["_id"] = str(doc["_id"])
        # Serialise datetimes
        for field in ("invoice_date", "due_date", "upload_timestamp",
                      "approved_at", "rejected_at"):
            if doc.get(field) and hasattr(doc[field], "isoformat"):
                doc[field] = doc[field].isoformat()
        if not full:
            # Strip heavy fields for list views
            doc.pop("raw_text", None)
            doc.pop("line_items", None)
        doc["days_since_invoice"] = self.days_since_invoice
        doc["is_overdue"] = self.is_overdue
        return doc
