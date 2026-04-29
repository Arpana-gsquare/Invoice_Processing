"""
Audit Trail Model – immutable log of every action taken on an invoice.
"""
from __future__ import annotations
from datetime import datetime, timezone
from bson import ObjectId
from ..extensions import get_db


class AuditLog:
    ACTIONS = (
        "upload", "extract", "approve", "reject", "edit",
        "flag_risk", "add_attachment", "export", "delete",
    )

    @classmethod
    def log(
        cls,
        invoice_id: str,
        action: str,
        actor_id: str,
        actor_name: str,
        details: dict | None = None,
    ) -> None:
        db = get_db()
        entry = {
            "invoice_id": invoice_id,
            "action": action,
            "actor_id": actor_id,
            "actor_name": actor_name,
            "details": details or {},
            "timestamp": datetime.now(timezone.utc),
        }
        db.audit_logs.insert_one(entry)

    @classmethod
    def get_for_invoice(cls, invoice_id: str) -> list[dict]:
        docs = get_db().audit_logs.find(
            {"invoice_id": invoice_id},
            sort=[("timestamp", -1)],
        )
        result = []
        for d in docs:
            d["_id"] = str(d["_id"])
            if hasattr(d.get("timestamp"), "isoformat"):
                d["timestamp"] = d["timestamp"].isoformat()
            result.append(d)
        return result

    @classmethod
    def get_recent(cls, limit: int = 50) -> list[dict]:
        docs = get_db().audit_logs.find(
            {}, sort=[("timestamp", -1)], limit=limit
        )
        result = []
        for d in docs:
            d["_id"] = str(d["_id"])
            if hasattr(d.get("timestamp"), "isoformat"):
                d["timestamp"] = d["timestamp"].isoformat()
            result.append(d)
        return result
