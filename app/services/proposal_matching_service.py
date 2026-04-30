"""
Proposal Matching Service
--------------------------
Matches an invoice to the best available Proposal, then calls Gemini
to generate structured business insights.

Match statuses:
  MATCHED        - strong match (score >= 75)
  PARTIAL_MATCH  - acceptable match (score 45-74)
  MISMATCH       - proposal found but conflicts (score 20-44)
  NO_PROPOSAL    - no candidate proposal found

Score breakdown (0-100):
  proposal_id / ref exact match  -> +35
  vendor_name fuzzy match        -> up to +30
  total_amount similarity        -> up to +20
  line_items similarity          -> up to +15
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

logger = logging.getLogger(__name__)

# Weights
W_REF     = 35
W_VENDOR  = 30
W_AMOUNT  = 20
W_LINES   = 15

AMOUNT_TOLERANCE     = 0.10   # 10% for header total
LINE_AMOUNT_TOL      = 0.15   # 15% per line item


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def match_invoice_to_proposal(invoice_data: dict) -> dict[str, Any]:
    """
    Find the best proposal match for an invoice and generate AI insights.

    Returns:
        proposal_id          - MongoDB _id string | None
        proposal_match_status
        proposal_match_score - int 0-100
        proposal_insights    - dict from Gemini (or basic fallback)
    """
    from ..models.proposal import Proposal
    from ..models.purchase_order import PurchaseOrder
    from .gemini_service import gemini_service

    vendor  = (invoice_data.get("vendor_name") or "").strip()
    amount  = float(invoice_data.get("total_amount") or 0)
    ref     = (invoice_data.get("proposal_id") or "").strip()

    candidates = Proposal.find_candidates(vendor, amount, ref)

    if not candidates:
        return _no_proposal_result()

    # Score each candidate
    scored = [(p, _score_match(invoice_data, p)) for p in candidates]
    scored.sort(key=lambda x: x[1]["score"], reverse=True)
    best_proposal, best_result = scored[0]

    score  = best_result["score"]
    status = _status_from_score(score)

    if score < 20:
        return _no_proposal_result()

    # ── Gemini AI Insights ──────────────────────────────────────────────
    insights = {}
    try:
        # Load PO if invoice has one linked
        po_dict = None
        po_id = invoice_data.get("po_id")
        if po_id:
            po_obj = PurchaseOrder.get_by_id(po_id)
            if po_obj:
                po_dict = po_obj.to_dict()

        insights = gemini_service.generate_proposal_insights(
            invoice  = invoice_data,
            proposal = best_proposal.to_dict(),
            po       = po_dict,
        )
    except Exception as exc:
        logger.warning("Gemini insight generation failed: %s", exc)
        insights = _basic_insights(invoice_data, best_proposal, score, status)

    return {
        "proposal_id":           best_proposal.id,
        "proposal_match_status": status,
        "proposal_match_score":  score,
        "proposal_insights":     insights,
    }


def get_comparison_data(invoice_data: dict, proposal) -> dict[str, Any]:
    """
    Return structured comparison for the UI — without calling Gemini again
    (insights are already stored on the invoice).
    """
    result = _score_match(invoice_data, proposal)
    details = result["details"]

    return {
        "score":  result["score"],
        "status": _status_from_score(result["score"]),
        "header": {
            "proposal_id": {
                "invoice_ref": invoice_data.get("proposal_id"),
                "proposal":    proposal.proposal_id,
                "match":       details["ref_match"],
            },
            "vendor_name": {
                "invoice":    invoice_data.get("vendor_name"),
                "proposal":   proposal.vendor_name,
                "similarity": details["vendor_similarity"],
                "match":      details["vendor_similarity"] >= 0.70,
            },
            "total_amount": {
                "invoice":        float(invoice_data.get("total_amount") or 0),
                "proposal":       proposal.total_amount,
                "difference":     details["amount_difference"],
                "difference_pct": details["amount_difference_pct"],
                "match":          details["amount_match"],
                "exceeds":        float(invoice_data.get("total_amount") or 0) > proposal.total_amount,
            },
            "validity": {
                "validity_date": str(proposal.validity_date)[:10] if proposal.validity_date else None,
                "is_expired":    proposal.is_expired,
            },
        },
        "line_items":              details.get("line_item_comparison", []),
        "unmatched_invoice_lines": details.get("unmatched_invoice_lines", []),
        "unmatched_proposal_lines":details.get("unmatched_proposal_lines", []),
        "flags":                   details.get("flags", []),
    }


# ---------------------------------------------------------------------------
# Internal Scoring
# ---------------------------------------------------------------------------

def _score_match(invoice_data: dict, proposal) -> dict:
    score = 0
    details: dict[str, Any] = {"flags": []}

    inv_amount = float(invoice_data.get("total_amount") or 0)
    inv_vendor = (invoice_data.get("vendor_name") or "").strip()
    inv_ref    = (invoice_data.get("proposal_id") or "").strip()
    inv_lines  = invoice_data.get("line_items") or []

    # Reference / proposal ID
    ref_match = bool(
        inv_ref and proposal.proposal_id and
        inv_ref.strip().upper() == proposal.proposal_id.strip().upper()
    )
    details["ref_match"] = ref_match
    if ref_match:
        score += W_REF

    # Vendor
    vsim = _fuzzy(inv_vendor, proposal.vendor_name)
    details["vendor_similarity"] = round(vsim, 3)
    score += int(vsim * W_VENDOR)
    if vsim < 0.55:
        details["flags"].append(
            "Vendor name mismatch (%.0f%% similar): '%s' vs '%s'" % (
                vsim * 100, inv_vendor, proposal.vendor_name)
        )

    # Amount
    diff     = abs(inv_amount - proposal.total_amount)
    diff_pct = diff / proposal.total_amount if proposal.total_amount else 0
    amt_ok   = diff_pct <= AMOUNT_TOLERANCE
    details["amount_difference"]     = round(diff, 2)
    details["amount_difference_pct"] = round(diff_pct * 100, 2)
    details["amount_match"]          = amt_ok

    if amt_ok:
        score += W_AMOUNT
    else:
        partial = max(0.0, 1.0 - (diff_pct / 0.25))
        score += int(partial * W_AMOUNT)
        direction = "exceeds" if inv_amount > proposal.total_amount else "below"
        details["flags"].append(
            "Invoice amount %s proposal by %.1f%% (inv=%.2f, proposal=%.2f)" % (
                direction, diff_pct * 100, inv_amount, proposal.total_amount)
        )
        if inv_amount > proposal.total_amount:
            details["flags"].append("AMOUNT_EXCEEDS_PROPOSAL")

    # Proposal validity
    if proposal.is_expired:
        details["flags"].append("Proposal has expired (validity: %s)" % (
            str(proposal.validity_date)[:10] if proposal.validity_date else "unknown"))

    # Line items
    li_result = _match_line_items(inv_lines, proposal.line_items)
    score += int(li_result["similarity"] * W_LINES)
    details.update(li_result)

    return {"score": min(score, 100), "details": details}


def _match_line_items(inv_lines: list, prop_lines: list) -> dict:
    if not inv_lines and not prop_lines:
        return {"similarity": 1.0, "line_item_comparison": [],
                "unmatched_invoice_lines": [], "unmatched_proposal_lines": []}
    if not prop_lines:
        return {"similarity": 0.0, "line_item_comparison": [],
                "unmatched_invoice_lines": inv_lines, "unmatched_proposal_lines": []}
    if not inv_lines:
        return {"similarity": 0.0, "line_item_comparison": [],
                "unmatched_invoice_lines": [], "unmatched_proposal_lines": prop_lines}

    used: set[int] = set()
    matched, unmatched_inv = [], []

    for inv_line in inv_lines:
        best_idx, best_sim = _best_line(inv_line, prop_lines, used)
        if best_sim >= 0.38 and best_idx is not None:
            prop_line = prop_lines[best_idx]
            used.add(best_idx)

            inv_price  = float(inv_line.get("unit_price") or 0)
            prop_price = float(prop_line.get("unit_price") or 0)
            inv_amt    = float(inv_line.get("amount") or 0)
            prop_amt   = float(prop_line.get("amount") or 0)
            inv_qty    = float(inv_line.get("quantity") or 0)
            prop_qty   = float(prop_line.get("quantity") or 0)

            price_ok  = _within_pct(inv_price, prop_price, LINE_AMOUNT_TOL)
            amount_ok = _within_pct(inv_amt,   prop_amt,   LINE_AMOUNT_TOL)
            qty_match = inv_qty == prop_qty or prop_qty == 0

            matched.append({
                "invoice_description":  inv_line.get("description"),
                "proposal_description": prop_line.get("description"),
                "description_similarity": round(best_sim, 3),
                "invoice_qty":    inv_qty,  "proposal_qty":    prop_qty,  "qty_match":    qty_match,
                "invoice_price":  inv_price,"proposal_price":  prop_price,"price_match":  price_ok,
                "invoice_amount": inv_amt,  "proposal_amount": prop_amt,  "amount_match": amount_ok,
                "status": "OK" if (qty_match and price_ok and amount_ok) else "MISMATCH",
            })
        else:
            unmatched_inv.append(inv_line)

    unmatched_prop = [prop_lines[i] for i in range(len(prop_lines)) if i not in used]
    ok_count = sum(1 for r in matched if r["status"] == "OK")
    sim = ok_count / len(inv_lines) if inv_lines else 0.0

    return {
        "similarity": sim,
        "line_item_comparison":    matched,
        "unmatched_invoice_lines": unmatched_inv,
        "unmatched_proposal_lines":unmatched_prop,
    }


def _best_line(inv_line: dict, prop_lines: list, used: set) -> tuple:
    inv_desc = (inv_line.get("description") or "").lower()
    best_sim, best_idx = 0.0, None
    for i, pl in enumerate(prop_lines):
        if i in used:
            continue
        sim = _fuzzy(inv_desc, (pl.get("description") or "").lower())
        if sim > best_sim:
            best_sim, best_idx = sim, i
    return best_idx, best_sim


def _fuzzy(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.strip(), b.strip()).ratio()


def _within_pct(a: float, b: float, pct: float) -> bool:
    if b == 0:
        return a == 0
    return abs(a - b) / b <= pct


def _status_from_score(score: int) -> str:
    if score >= 75:
        return "MATCHED"
    if score >= 45:
        return "PARTIAL_MATCH"
    if score >= 20:
        return "MISMATCH"
    return "NO_PROPOSAL"


def _no_proposal_result() -> dict:
    return {
        "proposal_id":           None,
        "proposal_match_status": "NO_PROPOSAL",
        "proposal_match_score":  0,
        "proposal_insights": {
            "summary": "No matching proposal found in the system.",
            "overall_verdict": "ATTENTION_NEEDED",
            "amount_analysis": {},
            "pricing_differences": [],
            "missing_items": [],
            "extra_items": [],
            "validity_check": {},
            "terms_flags": [],
            "po_alignment": {"has_po": False, "finding": "N/A"},
            "recommendations": ["Upload the relevant proposal document to enable comparison."],
            "risk_level": "MEDIUM",
        },
    }


def _basic_insights(invoice_data: dict, proposal, score: int, status: str) -> dict:
    """Fallback insights when Gemini call fails."""
    inv_amt  = float(invoice_data.get("total_amount") or 0)
    prop_amt = proposal.total_amount
    diff     = inv_amt - prop_amt
    diff_pct = (abs(diff) / prop_amt * 100) if prop_amt else 0

    flags = []
    if diff > 0:
        flags.append("Invoice amount exceeds proposal by %.2f (%.1f%%)" % (diff, diff_pct))
    if proposal.is_expired:
        flags.append("Proposal has expired")

    return {
        "summary": "Invoice matched to proposal '%s' with score %d/100 (%s)." % (
            proposal.proposal_id or proposal.id, score, status),
        "overall_verdict": "CLEAN" if score >= 75 else "ATTENTION_NEEDED",
        "amount_analysis": {
            "proposal_amount": prop_amt,
            "invoice_amount":  inv_amt,
            "difference":      round(diff, 2),
            "difference_pct":  round(diff_pct, 2),
            "exceeds_proposal":diff > 0,
            "finding": "Invoice is %.1f%% %s the proposal amount." % (
                diff_pct, "above" if diff > 0 else "below"),
        },
        "pricing_differences": [],
        "missing_items": [],
        "extra_items": [],
        "validity_check": {
            "proposal_valid": not proposal.is_expired,
            "validity_date":  str(proposal.validity_date)[:10] if proposal.validity_date else None,
            "finding": "Proposal is %s." % ("EXPIRED" if proposal.is_expired else "valid"),
        },
        "terms_flags": flags,
        "po_alignment": {"has_po": bool(invoice_data.get("po_id")), "finding": "N/A"},
        "recommendations": ["Review pricing differences before approving invoice."] if diff > 0 else [],
        "risk_level": "HIGH" if diff_pct > 15 else ("MEDIUM" if diff_pct > 5 else "LOW"),
    }
