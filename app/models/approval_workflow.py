"""
Two-level Invoice Approval Workflow
=====================================
Stored in ``approval_workflows`` MongoDB collection.

L1 → accountant role (first-level reviewer)
L2 → admin role     (final approver / finance head)

Lifecycle:
  initiate()        → current_level=1, l1_status='pending', l2_status='waiting'
  approve_level(1)  → l1_status='approved', current_level=2, l2_status='pending'
  approve_level(2)  → l2_status='approved', current_level=None, final_status='approved'
  reject_level(n)   → lN_status='rejected', current_level=None, final_status='rejected'
"""
from __future__ import annotations

from datetime import datetime, timezone
from bson import ObjectId

from ..extensions import get_db


class ApprovalWorkflow:
    def __init__(self, doc: dict):
        self._doc = doc

    # ── Properties ────────────────────────────────────────────────────────
    @property
    def id(self) -> str:
        return str(self._doc["_id"])

    @property
    def invoice_id(self) -> str:
        return str(self._doc["invoice_id"])

    @property
    def current_level(self):
        """1, 2, or None (completed / not started)."""
        return self._doc.get("current_level")

    @property
    def l1_status(self) -> str:
        return self._doc.get("l1_status", "pending")

    @property
    def l2_status(self) -> str:
        return self._doc.get("l2_status", "waiting")

    @property
    def final_status(self) -> str:
        return self._doc.get("final_status", "pending")

    # ── Factory / Queries ─────────────────────────────────────────────────
    @classmethod
    def get_by_invoice(cls, invoice_id: str) -> "ApprovalWorkflow | None":
        doc = get_db().approval_workflows.find_one({"invoice_id": invoice_id})
        return cls(doc) if doc else None

    @classmethod
    def initiate(cls, invoice_id: str, initiated_by: str,
                 initiated_by_name: str) -> "ApprovalWorkflow":
        """
        Start a fresh L1→L2 workflow for an invoice.
        Idempotent — returns existing workflow if already started.
        """
        existing = cls.get_by_invoice(invoice_id)
        if existing:
            return existing

        now = datetime.now(timezone.utc)
        doc = {
            "invoice_id":         invoice_id,
            "initiated_at":       now,
            "initiated_by":       initiated_by,
            "initiated_by_name":  initiated_by_name,
            # Level tracker
            "current_level":      1,
            "final_status":       "pending",
            "completed_at":       None,
            # L1
            "l1_status":          "pending",
            "l1_actor_id":        None,
            "l1_actor_name":      None,
            "l1_acted_at":        None,
            "l1_comments":        "",
            # L2
            "l2_status":          "waiting",   # not yet reached
            "l2_actor_id":        None,
            "l2_actor_name":      None,
            "l2_acted_at":        None,
            "l2_comments":        "",
        }
        result = get_db().approval_workflows.insert_one(doc)
        doc["_id"] = result.inserted_id
        return cls(doc)

    # ── Actions ───────────────────────────────────────────────────────────
    def approve_level(self, level: int, actor_id: str,
                      actor_name: str, comments: str = "") -> bool:
        """
        Approve the given level.
        Returns True when the whole workflow is now complete (both levels approved).
        """
        now = datetime.now(timezone.utc)
        db  = get_db()

        if level == 1:
            db.approval_workflows.update_one(
                {"_id": self._doc["_id"]},
                {"$set": {
                    "l1_status":     "approved",
                    "l1_actor_id":   actor_id,
                    "l1_actor_name": actor_name,
                    "l1_acted_at":   now,
                    "l1_comments":   comments,
                    "current_level": 2,
                    "l2_status":     "pending",
                }},
            )
            self._doc.update({
                "l1_status": "approved", "l1_actor_id": actor_id,
                "l1_actor_name": actor_name, "l1_acted_at": now,
                "current_level": 2, "l2_status": "pending",
            })
            return False   # workflow continues

        elif level == 2:
            db.approval_workflows.update_one(
                {"_id": self._doc["_id"]},
                {"$set": {
                    "l2_status":     "approved",
                    "l2_actor_id":   actor_id,
                    "l2_actor_name": actor_name,
                    "l2_acted_at":   now,
                    "l2_comments":   comments,
                    "current_level": None,
                    "final_status":  "approved",
                    "completed_at":  now,
                }},
            )
            self._doc.update({
                "l2_status": "approved", "final_status": "approved",
                "current_level": None, "completed_at": now,
            })
            return True    # workflow complete

        return False

    def reject_level(self, level: int, actor_id: str,
                     actor_name: str, comments: str = ""):
        """Reject at any level — workflow ends as rejected."""
        now = datetime.now(timezone.utc)
        lk  = f"l{level}"
        get_db().approval_workflows.update_one(
            {"_id": self._doc["_id"]},
            {"$set": {
                f"{lk}_status":     "rejected",
                f"{lk}_actor_id":   actor_id,
                f"{lk}_actor_name": actor_name,
                f"{lk}_acted_at":   now,
                f"{lk}_comments":   comments,
                "current_level":    None,
                "final_status":     "rejected",
                "completed_at":     now,
            }},
        )
        self._doc.update({
            f"{lk}_status": "rejected", "current_level": None,
            "final_status": "rejected", "completed_at": now,
        })

    # ── Serialisation ─────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        doc = self._doc.copy()
        doc["_id"] = str(doc["_id"])
        for field in ("initiated_at", "l1_acted_at", "l2_acted_at", "completed_at"):
            if doc.get(field) and hasattr(doc[field], "isoformat"):
                doc[field] = doc[field].isoformat()
        return doc
