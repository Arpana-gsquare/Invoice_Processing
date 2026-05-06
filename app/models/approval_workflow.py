"""
Three-level Invoice Approval Workflow
======================================
Stored in ``approval_workflows`` MongoDB collection.

  L1 → first-level reviewer   (role: L1)
  L2 → second-level reviewer  (role: L2)
  L3 → final approver         (role: L3 / finance head)

Lifecycle
---------
  initiate()         → current_level=1, l1_status='pending'
  approve_level(1)   → l1_status='approved', current_level=2
  approve_level(2)   → l2_status='approved', current_level=3
  approve_level(3)   → l3_status='approved', final_status='approved'
  reject_level(n)    → lN_status='rejected', final_status='rejected'
"""
from __future__ import annotations

from datetime import datetime, timezone
from bson import ObjectId

from ..extensions import get_db


class ApprovalWorkflow:
    def __init__(self, doc: dict):
        self._doc = doc

    # ── Properties ─────────────────────────────────────────────────────────
    @property
    def id(self) -> str:
        return str(self._doc["_id"])

    @property
    def invoice_id(self) -> str:
        return str(self._doc["invoice_id"])

    @property
    def current_level(self):
        """1, 2, 3, or None (completed / not started)."""
        return self._doc.get("current_level")

    @property
    def l1_status(self) -> str:
        return self._doc.get("l1_status", "pending")

    @property
    def l2_status(self) -> str:
        return self._doc.get("l2_status", "waiting")

    @property
    def l3_status(self) -> str:
        return self._doc.get("l3_status", "waiting")

    @property
    def final_status(self) -> str:
        return self._doc.get("final_status", "pending")

    # ── Factory / Queries ──────────────────────────────────────────────────
    @classmethod
    def get_by_invoice(cls, invoice_id: str) -> "ApprovalWorkflow | None":
        doc = get_db().approval_workflows.find_one({"invoice_id": invoice_id})
        return cls(doc) if doc else None

    @classmethod
    def initiate(cls, invoice_id: str,
                 initiated_by: str, initiated_by_name: str) -> "ApprovalWorkflow":
        """
        Start a fresh L1->L2->L3 workflow.
        Idempotent: returns existing workflow if one already exists.
        """
        existing = cls.get_by_invoice(invoice_id)
        if existing:
            return existing

        now = datetime.now(timezone.utc)
        doc = {
            "invoice_id":        invoice_id,
            "initiated_at":      now,
            "initiated_by":      initiated_by,
            "initiated_by_name": initiated_by_name,
            "current_level":     1,
            "final_status":      "pending",
            "completed_at":      None,
            # L1
            "l1_status":         "pending",
            "l1_actor_id":       None,
            "l1_actor_name":     None,
            "l1_acted_at":       None,
            "l1_comments":       "",
            # L2
            "l2_status":         "waiting",
            "l2_actor_id":       None,
            "l2_actor_name":     None,
            "l2_acted_at":       None,
            "l2_comments":       "",
            # L3
            "l3_status":         "waiting",
            "l3_actor_id":       None,
            "l3_actor_name":     None,
            "l3_acted_at":       None,
            "l3_comments":       "",
        }
        result = get_db().approval_workflows.insert_one(doc)
        doc["_id"] = result.inserted_id
        return cls(doc)

    # ── Actions ────────────────────────────────────────────────────────────
    def approve_level(self, level: int, actor_id: str,
                      actor_name: str, comments: str = "") -> bool:
        """
        Approve a specific level.
        Returns True when all three levels are complete (workflow done).
        """
        now = datetime.now(timezone.utc)
        db  = get_db()

        if level == 1:
            updates = {
                "l1_status":     "approved",
                "l1_actor_id":   actor_id,
                "l1_actor_name": actor_name,
                "l1_acted_at":   now,
                "l1_comments":   comments,
                "current_level": 2,
                "l2_status":     "pending",
            }
            db.approval_workflows.update_one({"_id": self._doc["_id"]}, {"$set": updates})
            self._doc.update(updates)
            return False

        elif level == 2:
            updates = {
                "l2_status":     "approved",
                "l2_actor_id":   actor_id,
                "l2_actor_name": actor_name,
                "l2_acted_at":   now,
                "l2_comments":   comments,
                "current_level": 3,
                "l3_status":     "pending",
            }
            db.approval_workflows.update_one({"_id": self._doc["_id"]}, {"$set": updates})
            self._doc.update(updates)
            return False

        elif level == 3:
            updates = {
                "l3_status":     "approved",
                "l3_actor_id":   actor_id,
                "l3_actor_name": actor_name,
                "l3_acted_at":   now,
                "l3_comments":   comments,
                "current_level": None,
                "final_status":  "approved",
                "completed_at":  now,
            }
            db.approval_workflows.update_one({"_id": self._doc["_id"]}, {"$set": updates})
            self._doc.update(updates)
            return True   # workflow fully complete

        return False

    def reject_level(self, level: int, actor_id: str,
                     actor_name: str, comments: str = ""):
        """Reject at any level — workflow ends immediately as rejected."""
        now = datetime.now(timezone.utc)
        lk  = f"l{level}"
        updates = {
            f"{lk}_status":     "rejected",
            f"{lk}_actor_id":   actor_id,
            f"{lk}_actor_name": actor_name,
            f"{lk}_acted_at":   now,
            f"{lk}_comments":   comments,
            "current_level":    None,
            "final_status":     "rejected",
            "completed_at":     now,
        }
        get_db().approval_workflows.update_one(
            {"_id": self._doc["_id"]}, {"$set": updates}
        )
        self._doc.update(updates)

    # ── Serialisation ──────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        doc = self._doc.copy()
        doc["_id"] = str(doc["_id"])
        ts_fields = (
            "initiated_at", "completed_at",
            "l1_acted_at", "l2_acted_at", "l3_acted_at",
        )
        for f in ts_fields:
            if doc.get(f) and hasattr(doc[f], "isoformat"):
                doc[f] = doc[f].isoformat()
        return doc
