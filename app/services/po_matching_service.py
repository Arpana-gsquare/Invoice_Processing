"""
PO Matching Engine
------------------
Matches an invoice to the best available Purchase Order.

Match statuses:
  MATCHED        - strong match on header + line items (score >= 80)
  PARTIAL_MATCH  - header matches but line items diverge (score 50-79)
  MISMATCH       - PO found but key values conflict (score 20-49)
  NO_PO_FOUND    - no candidate PO located in the database

Score breakdown (0-100):
  po_number exact match    → +40
  vendor_name fuzzy match  → up to +25
  total_amount match       → up to +20
  line_items similarity    → up to +15
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Any

logger = logging.getLogger(__name__)

# ── Weights ────────────────────────────────────────────────────────────────
W_PO_NUMBER = 40
W_VENDOR    = 25
W_AMOUNT    = 20
W_LINE_ITEMS = 15

AMOUNT_TOLERANCE = 0.05   # 5% tolerance for total_amount
LINE_AMOUNT_TOLERANCE = 0.10   # 10% per line item


# ── Public API ──────────────────────────────────────────────────────────────

def match_invoice_to_po(invoice_data: dict) -> dict[str, Any]:
    """
    Find the best PO match for an invoice.

    Args:
        invoice_data: dict with keys invoice_number, vendor_name, total_amount,
                      po_number (optional), line_items (optional).

    Returns:
        {
            po_id:           str | None,
            po_match_status: str,
            match_score:     int (0-100),
            match_details:   dict,
        }
    """
    from ..models.purchase_order import PurchaseOrder

    vendor = (invoice_data.get("vendor_name") or "").strip()
    amount = float(invoice_data.get("total_amount") or 0)
    po_number = (invoice_data.get("po_number") or "").strip()
    inv_line_items = invoice_data.get("line_items") or []

    candidates = PurchaseOrder.find_candidates(vendor, amount, po_number)

    if not candidates:
        return _no_po_result()

    # Score each candidate and pick the best
    scored = [
        (po, _score_match(invoice_data, po))
        for po in candidates
    ]
    scored.sort(key=lambda x: x[1]["score"], reverse=True)
    best_po, best_result = scored[0]

    score = best_result["score"]
    details = best_result["details"]

    if score >= 80:
        status = "MATCHED"
    elif score >= 50:
        status = "PARTIAL_MATCH"
    elif score >= 20:
        status = "MISMATCH"
    else:
        return _no_po_result()

    return {
        "po_id": best_po.id,
        "po_match_status": status,
        "match_score": score,
        "match_details": details,
    }


def compare_invoice_to_po(invoice_data: dict, po) -> dict[str, Any]:
    """
    Detailed field-by-field comparison for the UI comparison view.
    `po` is a PurchaseOrder instance.
    """
    result = _score_match(invoice_data, po)
    details = result["details"]

    comparison = {
        "score": result["score"],
        "status": _status_from_score(result["score"]),
        "header": {
            "po_number": {
                "invoice": invoice_data.get("po_number"),
                "po": po.po_number,
                "match": details["po_number_match"],
            },
            "vendor_name": {
                "invoice": invoice_data.get("vendor_name"),
                "po": po.vendor_name,
                "similarity": details["vendor_similarity"],
                "match": details["vendor_similarity"] >= 0.75,
            },
            "total_amount": {
                "invoice": float(invoice_data.get("total_amount") or 0),
                "po": po.total_amount,
                "difference": details["amount_difference"],
                "difference_pct": details["amount_difference_pct"],
                "match": details["amount_match"],
            },
        },
        "line_items": details.get("line_item_comparison", []),
        "unmatched_invoice_lines": details.get("unmatched_invoice_lines", []),
        "unmatched_po_lines": details.get("unmatched_po_lines", []),
        "flags": details.get("flags", []),
    }
    return comparison


# ── Internal Scoring ────────────────────────────────────────────────────────

def _score_match(invoice_data: dict, po) -> dict:
    """Score one invoice-vs-PO pair. Returns {score, details}."""
    score = 0
    details: dict[str, Any] = {"flags": []}

    inv_amount = float(invoice_data.get("total_amount") or 0)
    inv_vendor = (invoice_data.get("vendor_name") or "").strip()
    inv_po_num = (invoice_data.get("po_number") or "").strip()
    inv_lines = invoice_data.get("line_items") or []

    # ── PO Number ───────────────────────────────────────────────────────
    po_num_match = bool(
        inv_po_num and po.po_number and
        inv_po_num.strip().upper() == po.po_number.strip().upper()
    )
    details["po_number_match"] = po_num_match
    if po_num_match:
        score += W_PO_NUMBER
    elif inv_po_num and po.po_number:
        details["flags"].append(
            f"PO number mismatch: invoice has '{inv_po_num}', PO has '{po.po_number}'"
        )

    # ── Vendor Name ─────────────────────────────────────────────────────
    vendor_sim = _fuzzy_ratio(inv_vendor, po.vendor_name)
    details["vendor_similarity"] = round(vendor_sim, 3)
    vendor_score = int(vendor_sim * W_VENDOR)
    score += vendor_score
    if vendor_sim < 0.6:
        details["flags"].append(
            f"Vendor name low similarity ({vendor_sim:.0%}): "
            f"'{inv_vendor}' vs '{po.vendor_name}'"
        )

    # ── Total Amount ────────────────────────────────────────────────────
    amount_diff = abs(inv_amount - po.total_amount)
    amount_diff_pct = (amount_diff / po.total_amount) if po.total_amount else 0
    amount_match = amount_diff_pct <= AMOUNT_TOLERANCE
    details["amount_difference"] = round(amount_diff, 2)
    details["amount_difference_pct"] = round(amount_diff_pct * 100, 2)
    details["amount_match"] = amount_match

    if amount_match:
        score += W_AMOUNT
    else:
        # Partial credit for within 15%
        partial = max(0.0, 1.0 - (amount_diff_pct / 0.15))
        score += int(partial * W_AMOUNT)
        direction = "exceeds" if inv_amount > po.total_amount else "under"
        details["flags"].append(
            f"Invoice amount {direction} PO by "
            f"{amount_diff_pct:.1%} (inv={inv_amount:,.2f}, po={po.total_amount:,.2f})"
        )
        if inv_amount > po.total_amount:
            details["flags"].append("AMOUNT_EXCEEDS_PO")

    # ── Line Items ──────────────────────────────────────────────────────
    li_result = _match_line_items(inv_lines, po.line_items)
    score += int(li_result["similarity"] * W_LINE_ITEMS)
    details.update(li_result)

    return {"score": min(score, 100), "details": details}


def _match_line_items(inv_lines: list, po_lines: list) -> dict:
    """
    Greedy line-item matcher.
    Each invoice line is matched to the closest PO line by description similarity.
    Returns a similarity score (0-1) and a detailed comparison list.
    """
    if not inv_lines and not po_lines:
        return {"similarity": 1.0, "line_item_comparison": [],
                "unmatched_invoice_lines": [], "unmatched_po_lines": []}
    if not po_lines:
        return {"similarity": 0.0, "line_item_comparison": [],
                "unmatched_invoice_lines": inv_lines, "unmatched_po_lines": []}
    if not inv_lines:
        return {"similarity": 0.0, "line_item_comparison": [],
                "unmatched_invoice_lines": [], "unmatched_po_lines": po_lines}

    used_po_indices: set[int] = set()
    matched: list[dict] = []
    unmatched_inv: list = []

    for inv_line in inv_lines:
        best_idx, best_sim = _best_line_match(inv_line, po_lines, used_po_indices)
        if best_sim >= 0.40 and best_idx is not None:
            po_line = po_lines[best_idx]
            used_po_indices.add(best_idx)

            inv_qty   = float(inv_line.get("quantity") or 0)
            po_qty    = float(po_line.get("quantity") or 0)
            inv_price = float(inv_line.get("unit_price") or 0)
            po_price  = float(po_line.get("unit_price") or 0)
            inv_amt   = float(inv_line.get("amount") or 0)
            po_amt    = float(po_line.get("amount") or 0)

            qty_match   = (inv_qty == po_qty) or (po_qty == 0)
            price_ok    = _within_pct(inv_price, po_price, LINE_AMOUNT_TOLERANCE)
            amount_ok   = _within_pct(inv_amt, po_amt, LINE_AMOUNT_TOLERANCE)

            row = {
                "invoice_description": inv_line.get("description"),
                "po_description":      po_line.get("description"),
                "description_similarity": round(best_sim, 3),
                "invoice_qty":   inv_qty,  "po_qty":   po_qty,  "qty_match":   qty_match,
                "invoice_price": inv_price,"po_price": po_price, "price_match": price_ok,
                "invoice_amount":inv_amt,  "po_amount": po_amt, "amount_match": amount_ok,
                "status": "OK" if (qty_match and price_ok and amount_ok) else "MISMATCH",
            }
            matched.append(row)
        else:
            unmatched_inv.append(inv_line)

    unmatched_po = [po_lines[i] for i in range(len(po_lines)) if i not in used_po_indices]

    # Similarity = fraction of invoice lines successfully matched with OK status
    if not matched and not unmatched_inv:
        sim = 1.0
    else:
        ok_count = sum(1 for r in matched if r["status"] == "OK")
        total = len(inv_lines)
        sim = ok_count / total if total else 0.0

    return {
        "similarity": sim,
        "line_item_comparison": matched,
        "unmatched_invoice_lines": unmatched_inv,
        "unmatched_po_lines": unmatched_po,
    }


def _best_line_match(inv_line: dict, po_lines: list, used: set) -> tuple[int | None, float]:
    inv_desc = (inv_line.get("description") or "").lower()
    best_sim = 0.0
    best_idx = None
    for i, po_line in enumerate(po_lines):
        if i in used:
            continue
        po_desc = (po_line.get("description") or "").lower()
        sim = _fuzzy_ratio(inv_desc, po_desc)
        if sim > best_sim:
            best_sim = sim
            best_idx = i
    return best_idx, best_sim


def _fuzzy_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _within_pct(a: float, b: float, pct: float) -> bool:
    if b == 0:
        return a == 0
    return abs(a - b) / b <= pct


def _no_po_result() -> dict:
    return {
        "po_id": None,
        "po_match_status": "NO_PO_FOUND",
        "match_score": 0,
        "match_details": {"flags": ["No matching PO found in the system"]},
    }


def _status_from_score(score: int) -> str:
    if score >= 80:
        return "MATCHED"
    if score >= 50:
        return "PARTIAL_MATCH"
    if score >= 20:
        return "MISMATCH"
    return "NO_PO_FOUND"
