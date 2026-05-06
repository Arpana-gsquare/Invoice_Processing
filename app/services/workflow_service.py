"""
Workflow Service - Invoice State Transitions
=============================================
Manages the new 9-state workflow_status field while keeping the
legacy `status` field (pending / approved / rejected) in sync so
AI features and export code continue to work unchanged.

Valid workflow_status transitions
----------------------------------
  uploaded         -> processed
  processed        -> pending_L1 / manual_review / missing_po
  missing_po       -> pending_L1   (PO attached + sent)
  manual_review    -> pending_L1   (reviewer sends forward)
  pending_L1       -> pending_L2   (L1 approves)
  pending_L1       -> manual_review (L1 rejects / sends back)
  pending_L2       -> pending_L3
  pending_L2       -> manual_review
  pending_L3       -> approved
  pending_L3       -> manual_review
  approved         -> ready_for_payment (admin only)
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..models.invoice import Invoice, WF_TO_STATUS
from ..models.audit import AuditLog


class WorkflowError(Exception):
    """Raised when an invalid state transition is attempted."""


# -- Allowed transitions -------------------------------------------------------
ALLOWED_WF: dict[str, list[str]] = {
    "uploaded":          ["processed"],
    "processed":         ["pending_L1", "manual_review", "missing_po"],
    "missing_po":        ["pending_L1"],
    "manual_review":     ["pending_L1"],
    "pending_L1":        ["pending_L2",  "manual_review"],
    "pending_L2":        ["pending_L3",  "manual_review"],
    "pending_L3":        ["approved",    "manual_review"],
    "approved":          ["ready_for_payment"],
    "ready_for_payment": [],
}

# Legacy transitions kept for backward compat
ALLOWED_LEGACY: dict[str, list[str]] = {
    "pending":  ["approved", "rejected"],
    "approved": ["pending"],
    "rejected": ["pending"],
}

# Audit action labels
WF_LABELS: dict[tuple, str] = {
    ("processed",     "pending_L1"):        "matched_full_sent_L1",
    ("processed",     "manual_review"):     "matched_partial",
    ("processed",     "missing_po"):        "no_po_found",
    ("missing_po",    "pending_L1"):        "po_attached_sent_L1",
    ("manual_review", "pending_L1"):        "manual_review_sent_L1",
    ("pending_L1",    "pending_L2"):        "approved_L1",
    ("pending_L1",    "manual_review"):     "rejected_L1",
    ("pending_L2",    "pending_L3"):        "approved_L2",
    ("pending_L2",    "manual_review"):     "rejected_L2",
    ("pending_L3",    "approved"):          "approved_L3",
    ("pending_L3",    "manual_review"):     "rejected_L3",
    ("approved",      "ready_for_payment"): "marked_ready_for_payment",
}


def advance_workflow(
    invoice: Invoice,
    new_wf_status: str,
    actor_id: str,
    actor_name: str,
    reason: str = "",
) -> Invoice:
    """
    Move invoice to a new workflow_status.
    Updates the legacy `status` field for backward compat.
    Raises WorkflowError for invalid transitions.
    """
    current_wf = invoice.workflow_status

    if current_wf == new_wf_status:
        raise WorkflowError("Invoice is already in '%s' state." % new_wf_status)

    allowed = ALLOWED_WF.get(current_wf, [])
    if new_wf_status not in allowed:
        raise WorkflowError(
            "Cannot move from '%s' to '%s'. Allowed: %s"
            % (current_wf, new_wf_status, allowed or "none")
        )

    new_legacy = WF_TO_STATUS.get(new_wf_status, "pending")
    invoice.update({"workflow_status": new_wf_status, "status": new_legacy})

    invoice.push_status_history(
        from_status=current_wf,
        to_status=new_wf_status,
        changed_by=actor_id,
        changed_by_name=actor_name,
        reason=reason,
    )

    action = WF_LABELS.get((current_wf, new_wf_status), new_wf_status)
    AuditLog.log(
        invoice_id=invoice.id,
        action=action,
        actor_id=actor_id,
        actor_name=actor_name,
        details={"from": current_wf, "to": new_wf_status, "reason": reason},
    )
    return invoice


def transition_status(
    invoice: Invoice,
    new_status: str,
    actor_id: str,
    actor_name: str,
    reason: str = "",
) -> Invoice:
    """
    Backward-compat entry-point.
    Accepts workflow_status values (new) or legacy status values (old).
    """
    if new_status in ALLOWED_WF:
        return advance_workflow(invoice, new_status, actor_id, actor_name, reason)

    # Legacy path
    current = invoice.status
    if current == new_status:
        raise WorkflowError("Invoice is already in '%s' status." % new_status)
    allowed = ALLOWED_LEGACY.get(current, [])
    if new_status not in allowed:
        raise WorkflowError(
            "Cannot move from '%s' to '%s'. Allowed: %s"
            % (current, new_status, allowed or "none")
        )

    now = datetime.now(timezone.utc)
    updates: dict = {"status": new_status}
    if new_status == "approved":
        updates.update({
            "workflow_status": "approved",
            "approved_by": actor_id, "approved_at": now,
            "rejected_by": None, "rejected_at": None, "rejection_reason": "",
        })
    elif new_status == "rejected":
        updates.update({
            "rejected_by": actor_id, "rejected_at": now,
            "rejection_reason": reason,
            "approved_by": None, "approved_at": None,
        })
    elif new_status == "pending":
        updates.update({
            "workflow_status": "processed",
            "approved_by": None, "approved_at": None,
            "rejected_by": None, "rejected_at": None, "rejection_reason": "",
        })

    invoice.update(updates)
    invoice.push_status_history(
        from_status=current, to_status=new_status,
        changed_by=actor_id, changed_by_name=actor_name, reason=reason,
    )

    labels = {
        ("pending",  "approved"): "approved",
        ("pending",  "rejected"): "rejected",
        ("approved", "pending"):  "undo_approval",
        ("rejected", "pending"):  "undo_rejection",
    }
    AuditLog.log(
        invoice_id=invoice.id,
        action=labels.get((current, new_status), new_status),
        actor_id=actor_id, actor_name=actor_name,
        details={"from": current, "to": new_status, "reason": reason},
    )
    return invoice
