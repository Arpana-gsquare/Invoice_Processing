"""
Miscellaneous helper utilities.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from werkzeug.utils import secure_filename
from flask import current_app


def save_uploaded_file(file_storage) -> tuple[str, str]:
    """
    Save a Werkzeug FileStorage object to the configured UPLOAD_FOLDER.
    Returns (saved_path, original_filename).
    """
    original_name = secure_filename(file_storage.filename)
    ext = original_name.rsplit(".", 1)[-1].lower()
    unique_name = f"{uuid.uuid4().hex}.{ext}"
    upload_dir = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_dir, exist_ok=True)
    save_path = os.path.join(upload_dir, unique_name)
    file_storage.save(save_path)
    return save_path, original_name


def paginate(total: int, page: int, per_page: int) -> dict:
    """Return pagination metadata."""
    total_pages = max(1, (total + per_page - 1) // per_page)
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_page": page - 1 if page > 1 else None,
        "next_page": page + 1 if page < total_pages else None,
    }


def build_filters(args: dict) -> dict:
    """
    Convert URL query parameters into a MongoDB filter dict.
    Supported params: vendor, risk_flag, exclude_risk_flag, status,
                      date_from, date_to, search, overdue, po_match_status
    """
    from datetime import datetime, timezone
    query: dict = {}

    if args.get("vendor"):
        query["vendor_name"] = {"$regex": args["vendor"], "$options": "i"}

    if args.get("risk_flag"):
        query["risk_flag"] = args["risk_flag"]

    # Exclude a specific risk flag (e.g. exclude_risk_flag=DUPLICATE)
    if args.get("exclude_risk_flag"):
        query["risk_flag"] = {"$ne": args["exclude_risk_flag"]}

    if args.get("status"):
        query["status"] = args["status"]

    if args.get("date_from") or args.get("date_to"):
        date_filter: dict = {}
        if args.get("date_from"):
            try:
                date_filter["$gte"] = datetime.strptime(args["date_from"], "%Y-%m-%d")
            except ValueError:
                pass
        if args.get("date_to"):
            try:
                date_filter["$lte"] = datetime.strptime(args["date_to"], "%Y-%m-%d")
            except ValueError:
                pass
        if date_filter:
            query["invoice_date"] = date_filter

    if args.get("search"):
        term = args["search"]
        query["$or"] = [
            {"invoice_number": {"$regex": term, "$options": "i"}},
            {"vendor_name": {"$regex": term, "$options": "i"}},
        ]

    # Overdue: pending invoices where due_date exists, is not null, and is in the past
    if args.get("overdue") == "1":
        now = datetime.now(timezone.utc)
        query["due_date"] = {"$exists": True, "$ne": None, "$lt": now}
        query["status"]   = "pending"

    # PO match status filter (e.g. po_match_status=NO_PO_FOUND or po_match_status=none)
    if args.get("po_match_status"):
        query["po_match_status"] = args["po_match_status"]

    # New 9-state workflow filter
    if args.get("workflow_status"):
        query["workflow_status"] = args["workflow_status"]

    return query
