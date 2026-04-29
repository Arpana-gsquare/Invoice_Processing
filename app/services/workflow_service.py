"""
Workflow Service – Reversible Invoice Status Transitions
=========================================================
Valid transitions:
  pending  -> approved   (approve)
  pending  -> rejected   (reject)
  approved -> pending    (undo_approval / reopen)
  rejected -> pending    (undo_rejection / reopen)

Any other transition raises WorkflowError.
"""
from __future__ import annotations
from datetime import datetime, timezone
from ..models.invoice import Invoice
from ..models.audit import AuditLog


class WorkflowError(Exception):
    """Raised when an invalid status transition is attempted."""


# Allowed transitions: {from_status: [allowed_to_statuses]}
ALLOWED_TRANSITIONS: dict[str, list[str]] = {
    "pending":  ["approved", "rejected"],
    "approved": ["pending"],
    "rejected": ["pending"],
}

ACTION_LABELS = {
    ("pending",  "approved"): "approved",
    ("pending",  "rejected"): "rejected",
    ("approved", "pending"):  "undo_approval",
    ("rejected", "pending"):  "undo_rejection",
}


def transition_status(
    invoice: Invoice,
    new_status: str,
    actor_id: str,
    actor_name: str,
    reason: str = "",
) -> Invoice:
    """
    Transition an invoice to new_status with full validation,
    embedded status_history update, and audit log entry.

    Returns the updated Invoice.
    Raises WorkflowError if the transition is not permitted.
    """
    current = invoice.status

    if current == new_status:
        raise WorkflowError(
            "Invoice is already in '%s' status." % new_status
        )

    allowed = ALLOWED_TRANSITIONS.get(current, [])
    if new_status not in allowed:
        raise WorkflowError(
            "Cannot move from '%s' to '%s'. "
            "Allowed transitions from '%s': %s"
            % (current, new_status, current, allowed or "none")
        )

    now = datetime.now(timezone.utc)
    updates: dict = {"status": new_status}

    # Clear or set approval/rejection metadata
    if new_status == "approved":
        updates.update({"approved_by": actor_id, "approved_at": now,
                        "rejected_by": None, "rejected_at": None, "rejection_reason": ""})
    elif new_status == "rejected":
        updates.update({"rejected_by": actor_id, "rejected_at": now,
                        "rejection_reason": reason,
                        "approved_by": None, "approved_at": None})
    elif new_status == "pending":
        # Reopen — clear both sides
        updates.update({"approved_by": None, "approved_at": None,
                        "rejected_by": None, "rejected_at": None,
                        "rejection_reason": ""})

    invoice.update(updates)

    # Embedded status history
    invoice.push_status_history(
        from_status=current,
        to_status=new_status,
        changed_by=actor_id,
        changed_by_name=actor_name,
        reason=reason,
    )

    # Audit log
    action = ACTION_LABELS.get((current, new_status), new_status)
    AuditLog.log(
        invoice_id=invoice.id,
        action=action,
        actor_id=actor_id,
        actor_name=actor_name,
        details={"from": current, "to": new_status, "reason": reason},
    )

    return invoice
