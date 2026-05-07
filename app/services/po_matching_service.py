"""
PO Matching Engine (v2)
-----------------------
Matches an invoice to the best available Purchase Order using:
  vendor name, item descriptions, quantities, amounts, and tax figures.

Match outcomes:
  full     (score >= 85) -> workflow moves to pending_L1
  partial  (score 50-84) -> workflow moves to manual_review
  none     (score <  50) -> workflow moves to missing_po

Score breakdown (0-100 pts):
  vendor_name similarity  -> up to 30 pts
  line item names         -> up to 25 pts
  quantities match        -> up to 20 pts
  amounts match           -> up to 15 pts
  tax match               -> up to 10 pts
"""
from __future__ import annotations

import logging
from difflib import SequenceMatcher
from typing import Any

logger = logging.getLogger(__name__)

# -- Weights -------------------------------------------------------------------
W_VENDOR      = 30
W_ITEM_NAMES  = 25
W_QTY         = 20
W_AMOUNT      = 15
W_TAX         = 10

# Tolerances
AMOUNT_TOLERANCE = 0.05   # 5% on total / line amounts
TAX_TOLERANCE    = 0.05   # 5% on tax values
LINE_AMOUNT_TOL  = 0.10   # 10% per line item

# Score thresholds
FULL_THRESHOLD    = 85
PARTIAL_THRESHOLD = 50


# -- Public API ----------------------------------------------------------------

def match_invoice_to_po(invoice_data: dict) -> dict[str, Any]:
    """
    Find the best PO match for an invoice.

    Returns:
        {
            po_id:           str | None,
            po_match_status: "full" | "partial" | "none",
            match_score:     int (0-100),
            match_details:   dict,
        }
    """
    from ..models.purchase_order import PurchaseOrder

    vendor    = (invoice_data.get("vendor_name") or "").strip()
    amount    = float(invoice_data.get("total_amount") or 0)
    po_number = (invoice_data.get("po_number") or "").strip()

    candidates = PurchaseOrder.find_candidates(vendor, amount, po_number)

    if not candidates:
        return _build_result(None, "none", 0,
                             {"flags": ["No matching PO found in the system"]})

    # Score every candidate, pick the best
    scored = sorted(
        [(_score_match(invoice_data, po), po) for po in candidates],
        key=lambda x: x[0]["score"],
        reverse=True,
    )
    best_result, best_po = scored[0]

    score  = best_result["score"]
    status = _status_from_score(score)

    return _build_result(best_po.id, status, score, best_result["details"])


def compare_invoice_to_po(invoice_data: dict, po) -> dict[str, Any]:
    """
    Detailed field-by-field comparison for the UI comparison view.
    `po` is a PurchaseOrder instance.
    """
    result  = _score_match(invoice_data, po)
    details = result["details"]
    score   = result["score"]

    return {
        "score":  score,
        "status": _display_status_from_score(score),   # UI-friendly: MATCHED / PARTIAL_MATCH / MISMATCH
        "header": {
            "po_number": {                              # FIX: was missing — caused UndefinedError in template
                "invoice": invoice_data.get("po_number") or "",
                "po":      getattr(po, "po_number", "") or "",
                "match":   bool(
                    invoice_data.get("po_number")
                    and getattr(po, "po_number", "")
                    and (invoice_data.get("po_number") or "").strip().lower()
                       == (getattr(po, "po_number", "") or "").strip().lower()
                ),
            },
            "vendor_name": {
                "invoice":    invoice_data.get("vendor_name"),
                "po":         po.vendor_name,
                "similarity": details["vendor_similarity"],
                "match":      details["vendor_similarity"] >= 0.75,
            },
            "total_amount": {
                "invoice":        float(invoice_data.get("total_amount") or 0),
                "po":             po.total_amount,
                "difference":     details["amount_difference"],
                "difference_pct": details["amount_difference_pct"],
                "match":          details["amount_match"],
            },
            "tax": {
                "invoice_tax": details.get("invoice_tax"),
                "po_tax":      details.get("po_tax"),
                "match":       details.get("tax_match", False),
            },
        },
        "line_items":              details.get("line_item_comparison", []),
        "unmatched_invoice_lines": details.get("unmatched_invoice_lines", []),
        "unmatched_po_lines":      details.get("unmatched_po_lines", []),
        "flags":                   details.get("flags", []),
    }


# -- Internal Scoring ----------------------------------------------------------

def _score_match(invoice_data: dict, po) -> dict:
    """Score one invoice-vs-PO pair. Returns {score, details}."""
    score   = 0
    details: dict[str, Any] = {"flags": []}

    inv_amount = float(invoice_data.get("total_amount") or 0)
    inv_vendor = (invoice_data.get("vendor_name") or "").strip()
    inv_lines  = invoice_data.get("line_items") or []
    inv_tax    = _extract_tax(invoice_data)

    # -- Vendor Name (30 pts) --------------------------------------------------
    vendor_sim = _fuzzy_ratio(inv_vendor, getattr(po, "vendor_name", ""))
    details["vendor_similarity"] = round(vendor_sim, 3)
    score += int(vendor_sim * W_VENDOR)
    if vendor_sim < 0.6:
        details["flags"].append(
            "Vendor mismatch (%.0f%%): '%s' vs '%s'" % (
                vendor_sim * 100, inv_vendor, po.vendor_name)
        )

    # -- Item Names via line items (25 pts) ------------------------------------
    po_lines  = getattr(po, "line_items", []) or []
    li_result = _match_line_items(inv_lines, po_lines)
    item_sim  = li_result["item_similarity"]
    score    += int(item_sim * W_ITEM_NAMES)
    details.update(li_result)

    # -- Quantities (20 pts) ---------------------------------------------------
    qty_sim = li_result["qty_similarity"]
    score  += int(qty_sim * W_QTY)
    if qty_sim < 0.8 and inv_lines:
        details["flags"].append("Quantity mismatch (similarity %.0f%%)" % (qty_sim * 100))

    # -- Total Amount (15 pts) -------------------------------------------------
    po_amount    = float(getattr(po, "total_amount", 0) or 0)
    amt_diff     = abs(inv_amount - po_amount)
    amt_diff_pct = (amt_diff / po_amount) if po_amount else 1.0
    amount_match = amt_diff_pct <= AMOUNT_TOLERANCE
    details["amount_difference"]     = round(amt_diff, 2)
    details["amount_difference_pct"] = round(amt_diff_pct * 100, 2)
    details["amount_match"]          = amount_match

    if amount_match:
        score += W_AMOUNT
    else:
        partial = max(0.0, 1.0 - amt_diff_pct / 0.20)
        score  += int(partial * W_AMOUNT)
        direction = "exceeds" if inv_amount > po_amount else "under"
        details["flags"].append(
            "Amount %s PO by %.1f%% (inv=%.2f, po=%.2f)" % (
                direction, amt_diff_pct * 100, inv_amount, po_amount)
        )

    # -- Tax (10 pts) ----------------------------------------------------------
    po_tax    = _extract_po_tax(po)
    tax_match = False
    details["invoice_tax"] = inv_tax
    details["po_tax"]      = po_tax

    if inv_tax is not None and po_tax is not None and po_tax > 0:
        tax_diff_pct = abs(inv_tax - po_tax) / po_tax
        tax_match    = tax_diff_pct <= TAX_TOLERANCE
        details["tax_difference_pct"] = round(tax_diff_pct * 100, 2)
        if tax_match:
            score += W_TAX
        else:
            partial = max(0.0, 1.0 - tax_diff_pct / 0.20)
            score  += int(partial * W_TAX)
            details["flags"].append(
                "Tax mismatch: invoice=%.2f, po=%.2f" % (inv_tax, po_tax)
            )
    else:
        score += W_TAX // 2   # half credit when tax info unavailable

    details["tax_match"] = tax_match

    return {"score": min(score, 100), "details": details}


# -- Line Item Matching --------------------------------------------------------

def _match_line_items(inv_lines: list, po_lines: list) -> dict:
    """
    Greedy line-item matcher.
    Returns item_similarity (descriptions), qty_similarity, and comparison list.
    """
    if not inv_lines and not po_lines:
        return {
            "item_similarity": 1.0, "qty_similarity": 1.0,
            "line_item_comparison": [],
            "unmatched_invoice_lines": [], "unmatched_po_lines": [],
        }
    if not po_lines:
        return {
            "item_similarity": 0.0, "qty_similarity": 0.0,
            "line_item_comparison": [],
            "unmatched_invoice_lines": inv_lines, "unmatched_po_lines": [],
        }
    if not inv_lines:
        return {
            "item_similarity": 0.0, "qty_similarity": 0.0,
            "line_item_comparison": [],
            "unmatched_invoice_lines": [], "unmatched_po_lines": po_lines,
        }

    used_po: set[int] = set()
    matched: list[dict] = []
    unmatched_inv: list = []

    for inv_line in inv_lines:
        best_idx, best_sim = _best_line_match(inv_line, po_lines, used_po)
        if best_sim >= 0.40 and best_idx is not None:
            po_line = po_lines[best_idx]
            used_po.add(best_idx)

            inv_qty   = float(inv_line.get("quantity") or 0)
            po_qty    = float(po_line.get("quantity") or 0)
            inv_price = float(inv_line.get("unit_price") or 0)
            po_price  = float(po_line.get("unit_price") or 0)
            inv_amt   = float(inv_line.get("amount") or 0)
            po_amt    = float(po_line.get("amount") or 0)

            qty_match  = (inv_qty == po_qty) or (po_qty == 0)
            price_ok   = _within_pct(inv_price, po_price, LINE_AMOUNT_TOL)
            amount_ok  = _within_pct(inv_amt, po_amt, LINE_AMOUNT_TOL)

            matched.append({
                "invoice_description":    inv_line.get("description"),
                "po_description":         po_line.get("description"),
                "description_similarity": round(best_sim, 3),
                "invoice_qty":    inv_qty,   "po_qty":    po_qty,   "qty_match":   qty_match,
                "invoice_price":  inv_price, "po_price":  po_price, "price_match": price_ok,
                "invoice_amount": inv_amt,   "po_amount": po_amt,   "amount_match": amount_ok,
                "status": "OK" if (qty_match and price_ok and amount_ok) else "MISMATCH",
            })
        else:
            unmatched_inv.append(inv_line)

    unmatched_po = [po_lines[i] for i in range(len(po_lines)) if i not in used_po]
    total = len(inv_lines)
    item_sim = len(matched) / total if total else 0.0
    qty_ok   = sum(1 for r in matched if r["qty_match"])
    qty_sim  = qty_ok / total if total else 0.0

    return {
        "item_similarity":         item_sim,
        "qty_similarity":          qty_sim,
        "line_item_comparison":    matched,
        "unmatched_invoice_lines": unmatched_inv,
        "unmatched_po_lines":      unmatched_po,
    }


def _best_line_match(inv_line: dict, po_lines: list,
                     used: set) -> tuple[int | None, float]:
    inv_desc = (inv_line.get("description") or "").lower()
    best_sim, best_idx = 0.0, None
    for i, po_line in enumerate(po_lines):
        if i in used:
            continue
        po_desc = (po_line.get("description") or "").lower()
        sim = _fuzzy_ratio(inv_desc, po_desc)
        if sim > best_sim:
            best_sim, best_idx = sim, i
    return best_idx, best_sim


# -- Tax helpers ---------------------------------------------------------------

def _extract_tax(invoice_data: dict):
    """Try several common field names for tax on the invoice dict."""
    for key in ("tax_amount", "gst_amount", "sgst_amount", "cgst_amount",
                "igst_amount", "vat_amount", "tax"):
        val = invoice_data.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    # Derive from line items if present
    items = invoice_data.get("line_items") or []
    total = sum(float(it.get("tax", 0) or 0) for it in items)
    return total if total else None


def _extract_po_tax(po):
    """Try to get tax value from a PurchaseOrder object."""
    for attr in ("tax_amount", "gst_amount", "tax"):
        val = getattr(po, attr, None)
        if val is None and hasattr(po, "_doc"):
            val = (po._doc or {}).get(attr)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return None


# -- Utilities -----------------------------------------------------------------

def _fuzzy_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _within_pct(a: float, b: float, pct: float) -> bool:
    if b == 0:
        return a == 0
    return abs(a - b) / b <= pct


def _status_from_score(score: int) -> str:
    """Workflow-routing status (stored in invoice doc)."""
    if score >= FULL_THRESHOLD:
        return "full"
    if score >= PARTIAL_THRESHOLD:
        return "partial"
    return "none"


def _display_status_from_score(score: int) -> str:
    """UI-display status (used in comparison template badges)."""
    if score >= FULL_THRESHOLD:
        return "MATCHED"
    if score >= PARTIAL_THRESHOLD:
        return "PARTIAL_MATCH"
    return "MISMATCH"


def _build_result(po_id, status: str, score: int, details: dict) -> dict:
    return {
        "po_id":           po_id,
        "po_match_status": status,
        "match_score":     score,
        "match_details":   details,
    }
