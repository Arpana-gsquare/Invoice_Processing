"""
Gemini AI Service
Handles:
  - Invoice data extraction
  - Purchase Order extraction
  - Proposal extraction
  - AI-powered proposal insight generation (Proposal vs Invoice vs PO)
"""
from __future__ import annotations

import io
import json
import logging
import re
import time
from typing import Any

import fitz  # PyMuPDF
import google.generativeai as genai
from PIL import Image
from flask import current_app

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extraction Prompts
# ---------------------------------------------------------------------------

INVOICE_PROMPT = """You are an expert invoice parser. Analyze the invoice image(s) and extract ALL available information.
Return ONLY a valid JSON object. No markdown, no explanation, no code fences.
Schema:
{
  "invoice_number": "<string or null>",
  "vendor_name": "<string or null>",
  "vendor_address": "<string or null>",
  "vendor_email": "<string or null>",
  "vendor_phone": "<string or null>",
  "bill_to": "<string or null>",
  "invoice_date": "<YYYY-MM-DD or null>",
  "due_date": "<YYYY-MM-DD or null>",
  "payment_terms": "<e.g. Net 30 or null>",
  "po_number": "<string or null>",
  "currency": "<ISO 4217 e.g. USD>",
  "currency_symbol": "<e.g. $>",
  "subtotal": null,
  "tax_amount": null,
  "tax_rate": null,
  "total_amount": 0.0,
  "line_items": [{"description":"","quantity":null,"unit_price":null,"amount":null,"category":"other"}],
  "notes": "<string or null>",
  "category": "<dominant category>",
  "raw_text": "<full text>"
}
Rules: Numbers are plain floats. Return ONLY JSON."""

PO_PROMPT = """You are an expert procurement document parser. Analyze the Purchase Order image(s).
Return ONLY a valid JSON object. No markdown, no explanation, no code fences.
Schema:
{
  "po_number": "<string or null>",
  "vendor_name": "<string or null>",
  "vendor_address": "<string or null>",
  "vendor_email": "<string or null>",
  "bill_to": "<string or null>",
  "po_date": "<YYYY-MM-DD or null>",
  "delivery_date": "<YYYY-MM-DD or null>",
  "payment_terms": "<string or null>",
  "currency": "<ISO 4217>",
  "currency_symbol": "<e.g. $>",
  "subtotal": null,
  "tax_amount": null,
  "total_amount": 0.0,
  "line_items": [{"description":"","quantity":null,"unit_price":null,"amount":null,"item_code":""}],
  "notes": "<string or null>",
  "raw_text": "<full text>"
}
Rules: Numbers are plain floats. Return ONLY JSON."""

PROPOSAL_PROMPT = """You are an expert business proposal parser. Analyze the proposal/quotation document image(s).
Return ONLY a valid JSON object. No markdown, no explanation, no code fences.
Schema:
{
  "proposal_id": "<proposal or quotation reference number, or null>",
  "vendor_name": "<proposing company name or null>",
  "vendor_address": "<string or null>",
  "vendor_email": "<string or null>",
  "vendor_phone": "<string or null>",
  "client_name": "<name of client this proposal is addressed to, or null>",
  "proposal_date": "<YYYY-MM-DD or null>",
  "validity_date": "<date proposal expires YYYY-MM-DD, or null>",
  "payment_terms": "<e.g. 50% upfront, Net 30, or null>",
  "currency": "<ISO 4217 e.g. USD>",
  "currency_symbol": "<e.g. $>",
  "subtotal": null,
  "tax_amount": null,
  "tax_rate": null,
  "total_amount": 0.0,
  "line_items": [
    {
      "description": "<item or service description>",
      "quantity": null,
      "unit_price": null,
      "amount": null,
      "category": "<category if identifiable>"
    }
  ],
  "terms_conditions": "<key terms, delivery scope, warranties, SLA, penalties - summarised as text>",
  "notes": "<any other relevant notes>",
  "raw_text": "<full text of the proposal>"
}
Rules:
- Numbers must be plain floats without currency symbols.
- Extract ALL line items across ALL pages.
- Capture terms and conditions in plain text summary.
- Return ONLY the JSON object."""


def _build_insight_prompt(invoice: dict, proposal: dict, po: dict | None) -> str:
    """Build the Gemini insight-generation prompt from structured data."""

    # Resolve currency symbols from the actual documents — never default to "$"
    inv_currency  = (invoice.get("currency_symbol")  or invoice.get("currency")  or "").strip()
    prop_currency = (proposal.get("currency_symbol") or proposal.get("currency") or inv_currency).strip()

    if po:
        po_currency  = (po.get("currency_symbol") or po.get("currency") or inv_currency).strip()
        po_section = """
PURCHASE ORDER (authorised by buyer):
  PO Number:    %s
  Vendor:       %s
  PO Date:      %s
  Total Amount: %s%s
  Line Items:
%s
""" % (
            po.get("po_number", "N/A"),
            po.get("vendor_name", "N/A"),
            po.get("po_date", "N/A"),
            po_currency,
            "{:,.2f}".format(float(po.get("total_amount") or 0)),
            _format_line_items(po.get("line_items") or []),
        )
        po_alignment_instruction = (
            'For "po_alignment": set has_po=true and compare the invoice amounts/items '
            'against the PO data provided above.'
        )
    else:
        po_section = "\nNO PURCHASE ORDER LINKED TO THIS INVOICE.\n"
        po_alignment_instruction = (
            'For "po_alignment": set has_po=false and finding MUST be exactly: '
            '"No PO linked to this invoice — PO alignment cannot be assessed." '
            'Do NOT invent any PO data or alignment conclusion.'
        )

    return """You are a senior procurement analyst reviewing financial documents.

CRITICAL ACCURACY RULES — follow without exception:
1. Base every finding ONLY on the document data provided below. Never invent or assume figures.
2. Currency symbols must be taken exactly from the documents. Do not substitute or guess.
3. %s

Analyze the following documents and generate structured business insights.

INVOICE (what vendor is charging):
  Invoice #:    %s
  Vendor:       %s
  Invoice Date: %s
  Due Date:     %s
  Total Amount: %s%s
  Line Items:
%s

PROPOSAL (what vendor originally quoted):
  Proposal ID:    %s
  Vendor:         %s
  Proposal Date:  %s
  Valid Until:    %s
  Quoted Amount:  %s%s
  Terms:          %s
  Line Items:
%s
%s
Return ONLY a valid JSON object (no markdown, no fences) with this exact structure:
{
  "summary": "<2-3 sentence plain-English summary of the overall situation>",
  "overall_verdict": "<CLEAN | ATTENTION_NEEDED | ESCALATE>",
  "amount_analysis": {
    "proposal_amount": <float>,
    "invoice_amount": <float>,
    "difference": <float>,
    "difference_pct": <float>,
    "exceeds_proposal": <bool>,
    "finding": "<one sentence describing the amount situation>"
  },
  "pricing_differences": [
    {
      "item": "<item description>",
      "proposal_price": <float or null>,
      "invoice_price": <float or null>,
      "difference": <float>,
      "severity": "<LOW | MEDIUM | HIGH>"
    }
  ],
  "missing_items": [
    {"description": "<item in proposal but not in invoice>", "proposal_amount": <float or null>}
  ],
  "extra_items": [
    {"description": "<item in invoice but not in proposal>", "invoice_amount": <float or null>}
  ],
  "validity_check": {
    "proposal_valid": <bool>,
    "validity_date": "<YYYY-MM-DD or null>",
    "finding": "<one sentence>"
  },
  "terms_flags": [
    "<any concern about payment terms, delivery, warranties, penalties>"
  ],
  "po_alignment": {
    "has_po": <bool>,
    "finding": "<finding per the po_alignment instruction above>"
  },
  "recommendations": [
    "<actionable recommendation for the finance/procurement team>"
  ],
  "risk_level": "<LOW | MEDIUM | HIGH>"
}""" % (
        po_alignment_instruction,
        invoice.get("invoice_number", "N/A"),
        invoice.get("vendor_name", "N/A"),
        invoice.get("invoice_date", "N/A"),
        invoice.get("due_date", "N/A"),
        inv_currency,
        "{:,.2f}".format(float(invoice.get("total_amount") or 0)),
        _format_line_items(invoice.get("line_items") or []),
        proposal.get("proposal_id", "N/A"),
        proposal.get("vendor_name", "N/A"),
        proposal.get("proposal_date", "N/A"),
        proposal.get("validity_date", "N/A"),
        prop_currency,
        "{:,.2f}".format(float(proposal.get("total_amount") or 0)),
        proposal.get("terms_conditions", "N/A"),
        _format_line_items(proposal.get("line_items") or []),
        po_section,
    )


def _format_line_items(items: list) -> str:
    if not items:
        return "  (none)"
    lines = []
    for i, li in enumerate(items[:15], 1):
        desc  = (li.get("description") or "")[:60]
        qty   = li.get("quantity", "")
        price = li.get("unit_price", "")
        amt   = li.get("amount", "")
        lines.append("  %d. %s | qty:%s | unit:%s | total:%s" % (i, desc, qty, price, amt))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GeminiService class
# ---------------------------------------------------------------------------

class GeminiService:
    """Wraps the Gemini generative model for all document extractions."""

    def __init__(self):
        self._model = None

    def _get_model(self):
        if self._model is None:
            api_key = current_app.config["GEMINI_API_KEY"]
            if not api_key:
                raise RuntimeError("GEMINI_API_KEY is not configured.")
            genai.configure(api_key=api_key)
            model_name = current_app.config.get("GEMINI_MODEL", "gemini-2.0-flash")
            self._model = genai.GenerativeModel(model_name)
        return self._model

    # -- Invoice Extraction ---------------------------------------------------
    def extract_invoice(self, file_path: str, file_type: str) -> dict:
        parts = self._build_image_parts(file_path, file_type) + [INVOICE_PROMPT]
        raw   = self._call_with_retry(parts)
        data  = self._parse_json_response(raw, ["invoice_number", "vendor_name",
                                                "invoice_date", "total_amount",
                                                "currency", "line_items"])
        data["extraction_confidence"] = self._confidence(
            data, ["invoice_number", "vendor_name", "invoice_date",
                   "due_date", "total_amount", "currency", "line_items"])
        return data

    # -- PO Extraction --------------------------------------------------------
    def extract_po(self, file_path: str, file_type: str) -> dict:
        parts = self._build_image_parts(file_path, file_type) + [PO_PROMPT]
        raw   = self._call_with_retry(parts)
        data  = self._parse_json_response(raw, ["po_number", "vendor_name",
                                                "po_date", "total_amount",
                                                "currency", "line_items"])
        data["extraction_confidence"] = self._confidence(
            data, ["po_number", "vendor_name", "po_date",
                   "total_amount", "currency", "line_items"])
        return data

    # -- Proposal Extraction --------------------------------------------------
    def extract_proposal(self, file_path: str, file_type: str) -> dict:
        parts = self._build_image_parts(file_path, file_type) + [PROPOSAL_PROMPT]
        raw   = self._call_with_retry(parts)
        data  = self._parse_json_response(raw, ["proposal_id", "vendor_name",
                                                "proposal_date", "total_amount",
                                                "currency", "line_items"])
        data["extraction_confidence"] = self._confidence(
            data, ["proposal_id", "vendor_name", "proposal_date",
                   "validity_date", "total_amount", "currency", "line_items"])
        return data

    # -- AI Insight Generation ------------------------------------------------
    def generate_proposal_insights(self, invoice: dict, proposal: dict,
                                   po: dict | None = None) -> dict:
        """
        Call Gemini to produce structured business insights comparing
        Proposal vs Invoice (and PO if available).
        Returns the parsed insights dict.
        """
        prompt = _build_insight_prompt(invoice, proposal, po)
        raw    = self._call_with_retry([prompt], max_retries=3)

        try:
            insights = self._parse_json_response(
                raw, ["summary", "overall_verdict", "amount_analysis"])
        except Exception as exc:
            logger.warning("Insight JSON parse failed, using raw text: %s", exc)
            insights = {
                "summary":          raw[:500] if raw else "Insight generation failed.",
                "overall_verdict":  "ATTENTION_NEEDED",
                "amount_analysis":  {},
                "pricing_differences": [],
                "missing_items":    [],
                "extra_items":      [],
                "validity_check":   {},
                "terms_flags":      [],
                "po_alignment":     {"has_po": po is not None, "finding": "N/A"},
                "recommendations":  [],
                "risk_level":       "MEDIUM",
            }

        insights.setdefault("summary",             "")
        insights.setdefault("overall_verdict",     "ATTENTION_NEEDED")
        insights.setdefault("amount_analysis",     {})
        insights.setdefault("pricing_differences", [])
        insights.setdefault("missing_items",       [])
        insights.setdefault("extra_items",         [])
        insights.setdefault("validity_check",      {})
        insights.setdefault("terms_flags",         [])
        insights.setdefault("po_alignment",        {"has_po": po is not None, "finding": "N/A"})
        insights.setdefault("recommendations",     [])
        insights.setdefault("risk_level",          "MEDIUM")
        return insights

    # -- Internals ------------------------------------------------------------
    def _build_image_parts(self, file_path: str, file_type: str) -> list:
        if file_type == "pdf":
            return self._pdf_to_pil(file_path)
        return [Image.open(file_path).convert("RGB")]

    def _pdf_to_pil(self, pdf_path: str, dpi: int = 200) -> list:
        images = []
        try:
            doc = fitz.open(pdf_path)
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            for page in doc:
                pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
                img = Image.open(io.BytesIO(pix.tobytes("jpeg"))).convert("RGB")
                images.append(img)
            doc.close()
        except Exception as exc:
            raise RuntimeError("Failed to process PDF: %s" % exc) from exc
        if not images:
            raise RuntimeError("PDF has no renderable pages.")
        return images

    def _call_with_retry(self, parts: list, max_retries: int = 3) -> str:
        model    = self._get_model()
        last_exc = None
        for attempt in range(max_retries):
            try:
                response = model.generate_content(parts)
                if not response.candidates:
                    raise RuntimeError("Gemini response blocked or empty (no candidates).")
                try:
                    text = response.text
                except ValueError as ve:
                    raise RuntimeError("Gemini response flagged: %s" % ve) from ve
                if not text or not text.strip():
                    raise RuntimeError("Gemini returned empty text.")
                return text
            except Exception as exc:
                last_exc = exc
                wait = 2 ** attempt
                logger.warning("Gemini attempt %d/%d failed: %s. Retry in %ds",
                               attempt + 1, max_retries, exc, wait)
                time.sleep(wait)
        raise RuntimeError("Gemini failed after %d attempts: %s" % (max_retries, last_exc))

    def _parse_json_response(self, raw: str, required_keys: list) -> dict:
        if not raw or not raw.strip():
            raise RuntimeError("Gemini returned an empty response.")
        logger.debug("Gemini raw response:\n%s", raw[:800])

        text = raw.strip()
        text = re.sub(r"^`{3}(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"\s*`{3}\s*$",        "", text, flags=re.MULTILINE)
        text = text.strip()

        data = None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            pass

        if data is None:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group())
                except json.JSONDecodeError:
                    pass

        if data is None:
            raise RuntimeError("No valid JSON in Gemini response. Snippet: %r" % text[:400])

        data.setdefault("line_items",      [])
        data.setdefault("currency",        "USD")
        data.setdefault("currency_symbol", "$")

        if not data.get("total_amount"):
            line_sum = sum(float(li.get("amount") or 0) for li in data.get("line_items", []))
            data["total_amount"] = line_sum if line_sum else 0.0

        return data

    def _confidence(self, data: dict, key_fields: list) -> float:
        filled = sum(1 for f in key_fields if data.get(f))
        score  = filled / len(key_fields)
        if data.get("line_items"):
            score = min(1.0, score + 0.05)
        return round(score, 2)


# Module-level singleton
gemini_service = GeminiService()
