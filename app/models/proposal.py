"""
Proposal Model - MongoDB collection: proposals

Fields:
  proposal_id       - unique proposal reference (extracted or auto-generated)
  vendor_name       - proposing vendor / supplier
  proposal_date     - date proposal was issued
  validity_date     - date the proposal expires
  total_amount      - proposed value
  currency          - ISO 4217
  line_items        - nested array of quoted items
  terms_conditions  - key contractual terms text
  file_path         - uploaded document path
  original_filename
  file_type         - pdf | jpg | png
  upload_timestamp
  uploaded_by       - user_id
  extraction_confidence - 0.0-1.0
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from bson import ObjectId

from ..extensions import get_db


class Proposal:
    def __init__(self, doc: dict):
        self._doc = doc

    # ── Properties ──────────────────────────────────────────────────────────
    @property
    def id(self) -> str:
        return str(self._doc["_id"])

    @property
    def proposal_id(self) -> str:
        return self._doc.get("proposal_id", "")

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

    @property
    def validity_date(self):
        return self._doc.get("validity_date")

    @property
    def is_expired(self) -> bool:
        vd = self.validity_date
        if not vd:
            return False
        vd_aware = vd.replace(tzinfo=timezone.utc) if vd.tzinfo is None else vd
        return vd_aware < datetime.now(timezone.utc)

    # ── CRUD ────────────────────────────────────────────────────────────────
    @classmethod
    def create(cls, data: dict) -> "Proposal":
        db = get_db()
        data.setdefault("upload_timestamp", datetime.now(timezone.utc))
        data.setdefault("line_items", [])
        data.setdefault("currency", "USD")
        data.setdefault("extraction_confidence", 0.0)
        data.setdefault("terms_conditions", "")
        result = db.proposals.insert_one(data)
        data["_id"] = result.inserted_id
        return cls(data)

    @classmethod
    def get_by_id(cls, proposal_id: str) -> "Proposal | None":
        try:
            doc = get_db().proposals.find_one({"_id": ObjectId(proposal_id)})
        except Exception:
            return None
        return cls(doc) if doc else None

    @classmethod
    def find_candidates(cls, vendor_name: str, amount: float,
                        proposal_ref: str = "") -> list["Proposal"]:
        """
        Return proposals that could match an invoice:
          - Exact proposal_id/ref match, OR
          - Vendor fuzzy match + amount within 25% tolerance
        """
        db = get_db()
        conditions = []
        if proposal_ref:
            conditions.append({"proposal_id": proposal_ref})
        if vendor_name:
            safe = re.escape(vendor_name)
            conditions.append({
                "vendor_name": {"$regex": safe, "$options": "i"},
                "total_amount": {
                    "$gte": amount * 0.75,
                    "$lte": amount * 1.25,
                },
            })
        if not conditions:
            return []
        query = {"$or": conditions} if len(conditions) > 1 else conditions[0]
        docs = list(db.proposals.find(query).sort("upload_timestamp", -1).limit(10))
        return [cls(d) for d in docs]

    @classmethod
    def list_all(cls, page: int = 1, per_page: int = 20):
        db = get_db()
        total = db.proposals.count_documents({})
        skip = (page - 1) * per_page
        docs = (
            db.proposals.find({})
            .sort("upload_timestamp", -1)
            .skip(skip)
            .limit(per_page)
        )
        return [cls(d) for d in docs], total

    # ── Serialisation ────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        doc = self._doc.copy()
        doc["_id"] = str(doc["_id"])
        for field in ("proposal_date", "validity_date", "upload_timestamp"):
            if doc.get(field) and hasattr(doc[field], "isoformat"):
                doc[field] = doc[field].isoformat()
        doc["is_expired"] = self.is_expired
        return doc
