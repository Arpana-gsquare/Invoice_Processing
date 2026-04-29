"""
Fraud & Risk Detection Engine
Evaluates each invoice across five dimensions:
  1. Duplicate Detection   - same invoice_number + vendor + amount
  2. Anomaly Detection     - amount > mean + N*std vs historical data
  3. Vendor Risk Scoring   - derived from payment history, inconsistencies
  4. PO Compliance Check   - invoice vs PO amount / match status
  5. Final Classification  - SAFE | MODERATE | HIGH RISK | DUPLICATE
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from ..extensions import get_db

logger = logging.getLogger(__name__)

# -- Risk Score Weights -------------------------------------------------------
WEIGHT_DUPLICATE      = 100   # instant HIGH RISK / DUPLICATE
WEIGHT_ANOMALY        = 40    # high amount anomaly
WEIGHT_VENDOR_HISTORY = 30    # vendor has prior issues
WEIGHT_MISSING_FIELDS = 15    # critical fields absent
WEIGHT_INCONSISTENCY  = 10    # math doesn't add up
WEIGHT_PO_EXCEED      = 35    # invoice amount exceeds PO authorised value
WEIGHT_NO_PO          = 10    # no matching PO on file (MODERATE signal)
WEIGHT_LINE_MISMATCH  = 15    # PO line items don't match invoice lines


def analyse_invoice(invoice_data: dict, existing_id: str | None = None) -> dict[str, Any]:
    """
    Run all risk checks on extracted invoice data.

    Args:
        invoice_data:  The raw extracted dict (before DB insert).
        existing_id:   MongoDB _id string of a previously inserted invoice
                       (used to exclude self from duplicate checks after re-analysis).

    Returns a dict with keys:
        risk_flag     - SAFE | MODERATE | HIGH RISK | DUPLICATE
        risk_score    - 0-100
        risk_reasons  - list of human-readable strings
        duplicate_of  - invoice_id string if duplicate, else None
    """
    score = 0
    reasons: list[str] = []
    duplicate_of: str | None = None

    # 1. Duplicate Detection
    dup_result = _check_duplicate(invoice_data, existing_id)
    if dup_result["is_duplicate"]:
        score += WEIGHT_DUPLICATE
        reasons.append("Duplicate invoice detected (matches invoice %s)" % dup_result["match_id"])
        duplicate_of = dup_result["match_id"]

    # 2. Anomaly Detection
    anomaly_result = _check_amount_anomaly(invoice_data)
    if anomaly_result["is_anomaly"]:
        score += WEIGHT_ANOMALY
        reasons.append(
            "Unusually high amount: %s%s (z-score: %.2f, avg: %s)" % (
                invoice_data.get("currency_symbol", "$"),
                "{:,.2f}".format(float(invoice_data.get("total_amount", 0))),
                anomaly_result["z_score"],
                "{:,.2f}".format(anomaly_result["mean"]),
            )
        )

    # 3. Vendor Risk Scoring
    vendor_result = _score_vendor(invoice_data.get("vendor_name", ""))
    if vendor_result["risk_score"] > 0:
        vendor_contribution = int(vendor_result["risk_score"] * WEIGHT_VENDOR_HISTORY / 100)
        score += vendor_contribution
        reasons.extend(vendor_result["reasons"])

    # 4. Data Quality / Consistency Checks
    quality_result = _check_data_quality(invoice_data)
    score += quality_result["penalty"]
    reasons.extend(quality_result["reasons"])

    # 5. PO Compliance
    po_result = _check_po_compliance(invoice_data)
    score += po_result["penalty"]
    reasons.extend(po_result["reasons"])

    # Clamp & Classify
    score = min(score, 100)
    flag = _classify(score, duplicate_of)

    return {
        "risk_flag": flag,
        "risk_score": score,
        "risk_reasons": reasons,
        "duplicate_of": duplicate_of,
    }


# ---------------------------------------------------------------------------
# Internal Check Functions
# ---------------------------------------------------------------------------

def _check_duplicate(invoice_data: dict, existing_id: str | None) -> dict:
    """
    Duplicate heuristics (ordered by strictness):
      A) Exact invoice_number + vendor_name
      B) Same invoice_number + total_amount (vendor may be slightly different)
      C) Same vendor_name + amount within 1% (within same month)
    """
    db = get_db()
    inv_num = (invoice_data.get("invoice_number") or "").strip()
    vendor  = (invoice_data.get("vendor_name") or "").strip().lower()
    amount  = float(invoice_data.get("total_amount") or 0)

    excl = {}
    if existing_id:
        from bson import ObjectId
        try:
            excl = {"_id": {"$ne": ObjectId(existing_id)}}
        except Exception:
            pass

    # Check A - exact match
    if inv_num and vendor:
        q = {**excl, "invoice_number": inv_num,
             "vendor_name": {"$regex": "^%s$" % re.escape(vendor), "$options": "i"}}
        match = db.invoices.find_one(q, {"_id": 1})
        if match:
            return {"is_duplicate": True, "match_id": str(match["_id"])}

    # Check B - same invoice_number + amount
    if inv_num and amount:
        q = {**excl, "invoice_number": inv_num,
             "total_amount": {"$gte": amount * 0.99, "$lte": amount * 1.01}}
        match = db.invoices.find_one(q, {"_id": 1})
        if match:
            return {"is_duplicate": True, "match_id": str(match["_id"])}

    # Check C - same vendor + amount in same month
    inv_date = invoice_data.get("invoice_date")
    if vendor and amount and inv_date:
        if isinstance(inv_date, str):
            try:
                inv_date = datetime.strptime(inv_date, "%Y-%m-%d")
            except ValueError:
                inv_date = None
        if inv_date:
            month_start = inv_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if month_start.month == 12:
                month_end = month_start.replace(year=month_start.year + 1, month=1)
            else:
                month_end = month_start.replace(month=month_start.month + 1)
            q = {
                **excl,
                "vendor_name": {"$regex": "^%s$" % re.escape(vendor), "$options": "i"},
                "total_amount": {"$gte": amount * 0.99, "$lte": amount * 1.01},
                "invoice_date": {"$gte": month_start, "$lt": month_end},
            }
            match = db.invoices.find_one(q, {"_id": 1})
            if match:
                return {"is_duplicate": True, "match_id": str(match["_id"])}

    return {"is_duplicate": False, "match_id": None}


def _check_amount_anomaly(invoice_data: dict, z_threshold: float = 2.5) -> dict:
    """Z-score anomaly detection against all historical invoices."""
    amount = float(invoice_data.get("total_amount") or 0)
    vendor = (invoice_data.get("vendor_name") or "").strip()
    db = get_db()

    def _stats(amounts: list) -> tuple:
        if not amounts:
            return 0.0, 0.0
        mean = sum(amounts) / len(amounts)
        if len(amounts) < 2:
            return mean, 0.0
        variance = sum((x - mean) ** 2 for x in amounts) / len(amounts)
        return mean, variance ** 0.5

    # Per-vendor baseline (prefer this if >= 5 invoices)
    if vendor:
        vendor_docs = list(db.invoices.find(
            {"vendor_name": {"$regex": vendor, "$options": "i"}},
            {"total_amount": 1},
        ))
        vendor_amounts = [float(d["total_amount"]) for d in vendor_docs
                          if d.get("total_amount") is not None]
        if len(vendor_amounts) >= 5:
            mean, std = _stats(vendor_amounts)
            if std > 0:
                z = (amount - mean) / std
                if z > z_threshold:
                    return {"is_anomaly": True, "z_score": round(z, 2), "mean": mean}

    # Global baseline
    all_docs = list(db.invoices.find({}, {"total_amount": 1}))
    all_amounts = [float(d["total_amount"]) for d in all_docs
                   if d.get("total_amount") is not None]
    if len(all_amounts) >= 5:
        mean, std = _stats(all_amounts)
        if std > 0:
            z = (amount - mean) / std
            if z > z_threshold:
                return {"is_anomaly": True, "z_score": round(z, 2), "mean": mean}

    return {"is_anomaly": False, "z_score": 0.0, "mean": 0.0}


def _score_vendor(vendor_name: str) -> dict:
    """Vendor risk score (0-100) based on rejection rate, overdue, and prior flags."""
    if not vendor_name:
        return {"risk_score": 0, "reasons": []}

    db = get_db()
    docs = list(db.invoices.find(
        {"vendor_name": {"$regex": vendor_name, "$options": "i"}},
        {"status": 1, "due_date": 1, "risk_flag": 1, "upload_timestamp": 1},
    ))
    if not docs:
        return {"risk_score": 0, "reasons": []}

    total = len(docs)
    reasons: list[str] = []
    score = 0

    # Rejection rate
    rejected = sum(1 for d in docs if d.get("status") == "rejected")
    rejection_rate = rejected / total
    if rejection_rate > 0.3:
        score += 40
        reasons.append("Vendor has high rejection rate: %.0f%% (%d/%d invoices)" % (
            rejection_rate * 100, rejected, total))
    elif rejection_rate > 0.1:
        score += 15
        reasons.append("Vendor has moderate rejection rate: %.0f%%" % (rejection_rate * 100))

    # Overdue rate
    now = datetime.now(timezone.utc)
    overdue = 0
    for d in docs:
        due = d.get("due_date")
        if due and d.get("status") == "pending":
            due_aware = due.replace(tzinfo=timezone.utc) if due.tzinfo is None else due
            if due_aware < now:
                overdue += 1
    overdue_rate = overdue / total
    if overdue_rate > 0.25:
        score += 30
        reasons.append("Vendor has %d overdue unpaid invoices (%.0f%%)" % (
            overdue, overdue_rate * 100))
    elif overdue_rate > 0.1:
        score += 10
        reasons.append("Vendor has %d overdue invoices" % overdue)

    # Prior risk flags
    high_risk_count = sum(1 for d in docs if d.get("risk_flag") == "HIGH RISK")
    if high_risk_count > 0:
        score += min(30, high_risk_count * 10)
        reasons.append("Vendor has %d previously flagged HIGH RISK invoices" % high_risk_count)

    return {"risk_score": min(score, 100), "reasons": reasons}


def _check_data_quality(invoice_data: dict) -> dict:
    """Check for missing critical fields and mathematical inconsistencies."""
    penalty = 0
    reasons: list[str] = []

    critical = ["invoice_number", "vendor_name", "invoice_date", "total_amount"]
    missing = [f for f in critical if not invoice_data.get(f)]
    if missing:
        penalty += len(missing) * 5
        reasons.append("Missing critical field(s): %s" % ", ".join(missing))

    total    = float(invoice_data.get("total_amount") or 0)
    subtotal = float(invoice_data.get("subtotal") or 0)
    tax      = float(invoice_data.get("tax_amount") or 0)
    if subtotal and tax and total:
        calculated = subtotal + tax
        if abs(calculated - total) > (total * 0.02):
            penalty += WEIGHT_INCONSISTENCY
            reasons.append(
                "Amount mismatch: subtotal(%.2f) + tax(%.2f) = %.2f != total(%.2f)" % (
                    subtotal, tax, calculated, total)
            )

    if invoice_data.get("line_items") and total:
        li_total = sum(float(li.get("amount") or 0) for li in invoice_data["line_items"])
        if li_total and abs(li_total - total) > (total * 0.05):
            penalty += 5
            reasons.append(
                "Line items sum (%.2f) differs from invoice total (%.2f)" % (li_total, total)
            )

    return {"penalty": penalty, "reasons": reasons}


def _check_po_compliance(invoice_data: dict) -> dict:
    """
    PO-aware risk rules:
      - Invoice amount > PO amount                 => HIGH RISK (+35)
      - MISMATCH status (PO found but conflicts)   => HIGH RISK (+25)
      - PARTIAL_MATCH with line mismatches         => FLAG (+15)
      - NO_PO_FOUND                                => MODERATE (+10)
    """
    penalty = 0
    reasons: list[str] = []

    po_match_status = invoice_data.get("po_match_status")
    match_score     = int(invoice_data.get("match_score") or 0)
    match_details   = invoice_data.get("match_details") or {}
    flags           = match_details.get("flags", [])

    if not po_match_status:
        # PO check hasn't run yet (e.g., re-analysis before matching)
        return {"penalty": 0, "reasons": []}

    if po_match_status == "NO_PO_FOUND":
        penalty += WEIGHT_NO_PO
        reasons.append("No matching Purchase Order found in the system")

    elif po_match_status == "MISMATCH":
        penalty += 25
        reasons.append("Invoice conflicts with linked PO (match score: %d/100)" % match_score)
        # Check for amount-exceeds flag
        if any("AMOUNT_EXCEEDS_PO" in f for f in flags):
            penalty += WEIGHT_PO_EXCEED - 25   # top up to 35 total
            reasons.append("Invoice amount exceeds authorised PO value - HIGH RISK")

    elif po_match_status == "PARTIAL_MATCH":
        if any("AMOUNT_EXCEEDS_PO" in f for f in flags):
            penalty += WEIGHT_PO_EXCEED
            reasons.append("Invoice amount exceeds PO authorised value")
        if match_details.get("unmatched_invoice_lines"):
            penalty += WEIGHT_LINE_MISMATCH
            n = len(match_details["unmatched_invoice_lines"])
            reasons.append("%d invoice line item(s) have no matching PO line" % n)

    elif po_match_status == "MATCHED":
        # Fully matched - no penalty; sanity-check amount anyway
        if any("AMOUNT_EXCEEDS_PO" in f for f in flags):
            penalty += 10
            reasons.append("Invoice amount slightly over PO despite match")

    return {"penalty": penalty, "reasons": reasons}


def _classify(score: int, duplicate_of: str | None) -> str:
    if duplicate_of:
        return "DUPLICATE"
    if score >= 70:
        return "HIGH RISK"
    if score >= 30:
        return "MODERATE"
    return "SAFE"
