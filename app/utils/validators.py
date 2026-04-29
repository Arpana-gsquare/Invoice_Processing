"""
Validation utilities for invoice data and file uploads.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pycountry
from flask import current_app

ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png"}

# ISO 4217 currency codes (subset + full pycountry lookup)
_KNOWN_CURRENCIES = {c.alpha_3 for c in pycountry.currencies}

CURRENCY_SYMBOL_MAP = {
    "$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR",
    "₩": "KRW", "₺": "TRY", "₽": "RUB", "฿": "THB", "₴": "UAH",
    "₪": "ILS", "₦": "NGN", "₱": "PHP", "R$": "BRL", "kr": "SEK",
    "zł": "PLN", "Kč": "CZK", "Ft": "HUF", "CHF": "CHF", "AED": "AED",
    "AUD": "AUD", "CAD": "CAD", "SGD": "SGD", "HKD": "HKD", "NZD": "NZD",
}


def allowed_file(filename: str) -> bool:
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


def get_file_extension(filename: str) -> str:
    return filename.rsplit(".", 1)[1].lower() if "." in filename else ""


def normalise_currency(currency_str: str | None, symbol: str | None = None) -> str:
    """
    Return a valid ISO 4217 currency code.
    Falls back to symbol lookup, then "USD".
    """
    if currency_str:
        code = currency_str.strip().upper()
        if code in _KNOWN_CURRENCIES:
            return code
    if symbol:
        sym = symbol.strip()
        if sym in CURRENCY_SYMBOL_MAP:
            return CURRENCY_SYMBOL_MAP[sym]
    return "USD"


def parse_date(date_str: str | None) -> datetime | None:
    """Try multiple date formats; return a datetime or None."""
    if not date_str:
        return None
    formats = [
        "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y",
        "%d-%m-%Y", "%m-%d-%Y", "%d.%m.%Y",
        "%B %d, %Y", "%b %d, %Y", "%d %B %Y",
        "%Y%m%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


def validate_invoice_data(data: dict) -> tuple[bool, list[str]]:
    """
    Validate and normalise extracted invoice data in-place.
    Returns (is_valid, list_of_errors).
    """
    errors: list[str] = []

    # Normalise currency
    data["currency"] = normalise_currency(
        data.get("currency"), data.get("currency_symbol")
    )

    # Parse and validate dates
    for field in ("invoice_date", "due_date"):
        raw = data.get(field)
        if isinstance(raw, str):
            parsed = parse_date(raw)
            if parsed:
                data[field] = parsed
            else:
                data[field] = None
                if field == "invoice_date":
                    errors.append(f"Could not parse {field}: '{raw}'")

    # total_amount must be a positive number
    try:
        amount = float(data.get("total_amount") or 0)
        if amount <= 0:
            errors.append("total_amount must be greater than zero")
        data["total_amount"] = round(amount, 2)
    except (TypeError, ValueError):
        errors.append("total_amount is not a valid number")

    # Sanitise numeric fields
    for field in ("subtotal", "tax_amount"):
        try:
            val = data.get(field)
            data[field] = round(float(val), 2) if val is not None else None
        except (TypeError, ValueError):
            data[field] = None

    # Validate line items
    clean_items = []
    for item in (data.get("line_items") or []):
        clean = {
            "description": str(item.get("description") or ""),
            "quantity": _safe_float(item.get("quantity")),
            "unit_price": _safe_float(item.get("unit_price")),
            "amount": _safe_float(item.get("amount")),
            "category": item.get("category", "other"),
        }
        clean_items.append(clean)
    data["line_items"] = clean_items

    is_valid = len([e for e in errors if "parse" not in e]) == 0
    return is_valid, errors


def _safe_float(val) -> float | None:
    try:
        return round(float(val), 4) if val is not None else None
    except (TypeError, ValueError):
        return None
