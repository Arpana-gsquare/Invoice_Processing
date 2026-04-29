"""
Recycle Bin Service – Soft Delete, Restore, and Permanent Deletion
===================================================================
Design: single-collection approach.

  Pros vs separate recycle_bin collection:
    + Restore is a flag flip — no document movement
    + TTL index fires directly on permanent_delete_at
    + No risk of ID conflicts or data drift between collections
    + Simpler queries (just add is_deleted filter)

  Cons:
    - Active queries must always include {"is_deleted": {"$ne": True}}
      (handled centrally in Invoice.list_all / Invoice.get_by_id)
    - Index size grows slightly (mitigated by partial TTL index)

Retention options (days): 7, 15, 30
Auto-purge: MongoDB TTL index on permanent_delete_at (expireAfterSeconds=0)
Manual purge: hard_delete() available from UI
"""
from __future__ import annotations
from ..models.invoice import Invoice
from ..models.audit import AuditLog

RETENTION_OPTIONS = [7, 15, 30]
DEFAULT_RETENTION = 30


def soft_delete(
    invoice: Invoice,
    actor_id: str,
    actor_name: str,
    retention_days: int = DEFAULT_RETENTION,
) -> None:
    """Move invoice to recycle bin with a retention period."""
    if invoice.is_deleted:
        raise ValueError("Invoice is already in the recycle bin.")

    if retention_days not in RETENTION_OPTIONS:
        retention_days = DEFAULT_RETENTION

    invoice.soft_delete(
        deleted_by=actor_id,
        deleted_by_name=actor_name,
        retention_days=retention_days,
    )

    AuditLog.log(
        invoice_id=invoice.id,
        action="soft_delete",
        actor_id=actor_id,
        actor_name=actor_name,
        details={
            "retention_days": retention_days,
            "invoice_number": invoice.invoice_number,
        },
    )


def restore(invoice: Invoice, actor_id: str, actor_name: str) -> None:
    """Restore an invoice from the recycle bin back to active."""
    if not invoice.is_deleted:
        raise ValueError("Invoice is not in the recycle bin.")

    invoice.restore()

    AuditLog.log(
        invoice_id=invoice.id,
        action="restore",
        actor_id=actor_id,
        actor_name=actor_name,
        details={"invoice_number": invoice.invoice_number},
    )


def permanent_delete(invoice: Invoice, actor_id: str, actor_name: str) -> None:
    """Permanently destroy an invoice (only callable from recycle bin)."""
    if not invoice.is_deleted:
        raise ValueError("Can only permanently delete invoices in the recycle bin.")

    AuditLog.log(
        invoice_id=invoice.id,
        action="permanent_delete",
        actor_id=actor_id,
        actor_name=actor_name,
        details={"invoice_number": invoice.invoice_number},
    )
    invoice.hard_delete()
