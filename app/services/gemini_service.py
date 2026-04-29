"""
Gemini AI Service - Invoice Data Extraction

Fixes applied:
  - PIL images passed directly to SDK (most reliable format)
  - Empty/blocked response detection before parsing
  - Full raw-response logging for debugging
  - Retry only on transient API errors
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

EXTRACTION_PROMPT = """You are an expert invoice parser. Analyze the invoice image(s) and extract ALL available information.

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
  "currency": "<ISO 4217 e.g. USD, EUR, INR>",
  "currency_symbol": "<e.g. $>",
  "subtotal": null,
  "tax_amount": null,
  "tax_rate": null,
  "total_amount": 0.0,
  "line_items": [
    {
      "description": "<string>",
      "quantity": null,
      "unit_price": null,
      "amount": null,
      "category": "<utilities|travel|office_supplies|software|hardware|professional_services|marketing|logistics|maintenance|other>"
    }
  ],
  "notes": "<string or null>",
  "category": "<dominant category>",
  "raw_text": "<full text of invoice>"
}

Rules:
- Numbers must be plain floats without currency symbols or commas.
- If invoice_number is absent set it to null.
- If total_amount cannot be read, sum the line item amounts.
- Aggregate line items from ALL pages for multi-page invoices.
- Return ONLY the JSON object, nothing else."""


class GeminiService:
    """Wraps the Gemini generative model for invoice extraction."""

    def __init__(self):
        self._model = None

    def _get_model(self):
        if self._model is None:
            api_key = current_app.config["GEMINI_API_KEY"]
            if not api_key:
                raise RuntimeError("GEMINI_API_KEY is not configured.")
            genai.configure(api_key=api_key)
            self._model = genai.GenerativeModel(
                model_name=current_app.config["GEMINI_MODEL"],
                generation_config={
                    "temperature": 0.1,
                    "top_p": 0.95,
                    "max_output_tokens": 8192,
                },
            )
        return self._model

    def extract_invoice(self, file_path: str, file_type: str) -> dict:
        """Main entry point. Returns extracted fields dict with extraction_confidence."""
        parts = self._build_content_parts(file_path, file_type)
        raw_response = self._call_with_retry(parts)
        extracted = self._parse_response(raw_response)
        extracted["extraction_confidence"] = self._compute_confidence(extracted)
        return extracted

    def _build_content_parts(self, file_path: str, file_type: str) -> list:
        """
        Build [PIL.Image, ..., prompt_string] for generate_content().
        PIL images are the most reliable format for the google-generativeai SDK.
        """
        parts = []
        if file_type == "pdf":
            parts.extend(self._pdf_to_pil(file_path))
        else:
            parts.append(Image.open(file_path).convert("RGB"))
        parts.append(EXTRACTION_PROMPT)
        return parts

    def _pdf_to_pil(self, pdf_path: str, dpi: int = 200) -> list:
        """Render every PDF page to a PIL Image."""
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
            logger.error("PDF to PIL conversion failed: %s", exc)
            raise RuntimeError("Failed to process PDF: %s" % exc) from exc
        if not images:
            raise RuntimeError("PDF has no renderable pages.")
        return images

    def _call_with_retry(self, parts: list, max_retries: int = 3) -> str:
        """
        Call Gemini API with exponential backoff for transient errors.
        parts = [PIL.Image, ..., prompt_string]
        """
        model = self._get_model()
        last_exc = None

        for attempt in range(max_retries):
            try:
                response = model.generate_content(parts)

                # Empty candidates = safety filter blocked the request
                if not response.candidates:
                    raise RuntimeError(
                        "Gemini returned no candidates (safety filter). "
                        "Check finish_reason in the response."
                    )

                # response.text raises ValueError when response is blocked
                try:
                    text = response.text
                except ValueError as ve:
                    raise RuntimeError("Gemini response was blocked: %s" % ve) from ve

                if not text or not text.strip():
                    logger.warning(
                        "Gemini empty text on attempt %d/%d", attempt + 1, max_retries
                    )
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    raise RuntimeError("Gemini returned empty response after all retries.")

                logger.debug("Gemini raw response (first 300 chars): %s", text[:300])
                return text

            except RuntimeError:
                raise  # our own errors - don't retry
            except Exception as exc:
                last_exc = exc
                wait = 2 ** attempt
                logger.warning(
                    "Gemini API attempt %d/%d failed: %s. Retrying in %ds",
                    attempt + 1, max_retries, exc, wait,
                )
                time.sleep(wait)

        raise RuntimeError(
            "Gemini API failed after %d attempts: %s" % (max_retries, last_exc)
        )

    def _parse_response(self, raw: str) -> dict:
        """
        Parse raw Gemini text into a dict.
        Strips markdown fences and falls back to regex extraction of first JSON block.
        """
        if not raw or not raw.strip():
            raise RuntimeError("Gemini returned an empty response string.")

        logger.debug("Gemini full response:\n%s", raw)

        text = raw.strip()

        # Remove markdown code fences if the model wrapped the output
        text = re.sub(r"^`{3}(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"\s*`{3}\s*$", "", text, flags=re.MULTILINE)
        text = text.strip()

        data = None

        # Attempt 1: direct JSON parse
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            pass

        # Attempt 2: extract first { ... } block
        if data is None:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    pass

        if data is None:
            snippet = text[:400] if text else "(empty)"
            logger.error("No parseable JSON in Gemini response. Snippet:\n%s", snippet)
            raise RuntimeError("Gemini did not return valid JSON. Snippet: %r" % snippet)

        # Safe defaults
        data.setdefault("line_items", [])
        data.setdefault("currency", "USD")
        data.setdefault("currency_symbol", "$")

        if not data.get("total_amount"):
            line_sum = sum(
                float(li.get("amount") or 0) for li in data.get("line_items", [])
            )
            data["total_amount"] = line_sum if line_sum else 0.0

        return data

    def _compute_confidence(self, data: dict) -> float:
        """Heuristic 0-1 confidence score based on filled key fields."""
        key_fields = [
            "invoice_number", "vendor_name", "invoice_date",
            "due_date", "total_amount", "currency", "line_items",
        ]
        filled = sum(1 for f in key_fields if data.get(f))
        score = filled / len(key_fields)
        if data.get("line_items"):
            score = min(1.0, score + 0.05)
        return round(score, 2)


# Module-level singleton
gemini_service = GeminiService()
