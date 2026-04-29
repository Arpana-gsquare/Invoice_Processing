"""
PurchaseOrder Model - MongoDB collection: purchase_orders

Fields:
  po_number       - PO reference number
  vendor_name     - supplier / vendor
  po_date         - date PO was issued
  total_amount    - authorised value
  currency        - ISO 4217
  line_items      - nested array of ordered items
  file_path       - uploaded document path
  original_filename
  file_type       - pdf | jpg | png
  upload_timestamp
  uploaded_by     - user_id
  extraction_confidence - 0.0-1.0
"""
from __future__ import annotations

from datetime import datetime, timezone
from bson import ObjectId

from ..extensions import get_db


class PurchaseOrder:
    def __init__(self, doc: dict):
        self._doc = doc

    # ── Properties ──────────────────────────────────────────────────────────
    @property
    def id(self) -> str:
        return str(self._doc["_id"])

    @property
    def po_number(self) -> str:
        return self._doc.get("po_number", "")

    @property
    def vendor_name(self) -> str:
        return self._doc.get("vendor_name", "")

    @property
    def total_amount(self) -> float:
        return float(self._doc.get("total_amount", 0))

    @property
    def currency(self) -> str:
        return self._doc.get("currency", "USD")

    @property
    def line_items(self) -> list:
        return self._doc.get("line_items", [])

    # ── CRUD ────────────────────────────────────────────────────────────────
    @classmethod
    def create(cls, data: dict) -> "PurchaseOrder":
        db = get_db()
        data.setdefault("upload_timestamp", datetime.now(timezone.utc))
        data.setdefault("line_items", [])
        data.setdefault("currency", "USD")
        data.setdefault("extraction_confidence", 0.0)
        result = db.purchase_orders.insert_one(data)
        data["_id"] = result.inserted_id
        return cls(data)

    @classmethod
    def get_by_id(cls, po_id: str) -> "PurchaseOrder | None":
        try:
            doc = get_db().purchase_orders.find_one({"_id": ObjectId(po_id)})
        except Exception:
            return None
        return cls(doc) if doc else None

    @classmethod
    def get_by_number(cls, po_number: str) -> list["PurchaseOrder"]:
        docs = get_db().purchase_orders.find({"po_number": po_number})
        return [cls(d) for d in docs]

    @classmethod
    def find_candidates(cls, vendor_name: str, amount: float,
                        po_number: str = "") -> list["PurchaseOrder"]:
        """
        Return POs that could match an invoice:
          - Exact po_number match, OR
          - vendor fuzzy match with amount within 20% tolerance
        """
        db = get_db()
        conditions = []
        if po_number:
            conditions.append({"po_number": po_number})
        if vendor_name:
            import re as _re
            # escape for regex safety
            safe = _re.escape(vendor_name)
            conditions.append({
                "vendor_name": {"$regex": safe, "$options": "i"},
                "total_amount": {
                    "$gte": amount * 0.80,
                    "$lte": amount * 1.20,
                },
            })
        if not conditions:
            return []
        query = {"$or": conditions} if len(conditions) > 1 else conditions[0]
        docs = list(db.purchase_orders.find(query).sort("upload_timestamp", -1).limit(10))
        return [cls(d) for d in docs]

    @classmethod
    def list_all(cls, page: int = 1, per_page: int = 20):
        db = get_db()
        total = db.purchase_orders.count_documents({})
        skip = (page - 1) * per_page
        docs = (
            db.purchase_orders.find({})
            .sort("upload_timestamp", -1)
            .skip(skip)
            .limit(per_page)
        )
        return [cls(d) for d in docs], total

    # ── Serialisation ────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        doc = self._doc.copy()
        doc["_id"] = str(doc["_id"])
        for field in ("po_date", "upload_timestamp"):
            if doc.get(field) and hasattr(doc[field], "isoformat"):
                doc[field] = doc[field].isoformat()
        return doc
