"""Pipeline de extracción de texto: pymupdf (primario) → pdfplumber (fallback).

El criterio de fallback es objetivo y configurable (PDF_MIN_CHARS_PER_PAGE) y se
registra en el log del request. La detección de PDF sin texto nativo permite
fallar rápido y, en Fase 2, derivar al pipeline OCR.

Preparado para Fase 2: el OCR puede agregarse como un nuevo paso aquí sin tocar
document_processor.py.
"""

from __future__ import annotations

import io
import logging

import fitz  # pymupdf
import pdfplumber

from core.config import settings
from core.exceptions import InvalidPDFError, NoNativeTextError
from core.logging import get_logger, log
from models.extraction import ExtractionResult

logger = get_logger("idp.pdf_extractor")

# Magic bytes de un archivo PDF.
_PDF_MAGIC = b"%PDF-"


def validate_pdf_bytes(content: bytes) -> None:
    """Valida magic bytes y tamaño máximo. Lanza InvalidPDFError si falla."""
    if not content[:5].startswith(_PDF_MAGIC):
        raise InvalidPDFError("el archivo no es un PDF válido (magic bytes)")

    max_bytes = settings.PDF_MAX_SIZE_MB * 1024 * 1024
    if len(content) > max_bytes:
        raise InvalidPDFError(
            f"el PDF supera el tamaño máximo de {settings.PDF_MAX_SIZE_MB} MB"
        )


def _resolve_page_indices(
    total_pages: int,
    page_range: tuple[int, int] | None,
) -> tuple[list[int], bool]:
    """Resuelve los índices 0-based de páginas a procesar y si hubo truncado.

    page_range viene 1-indexado e inclusivo. Para documentos largos sin page_range,
    se procesan las primeras PDF_MAX_PAGES_FULL páginas (truncado con WARNING).
    """
    if page_range is not None:
        start = max(1, page_range[0]) - 1
        end = min(total_pages, page_range[1])
        return list(range(start, end)), False

    if total_pages > settings.PDF_MAX_PAGES_FULL:
        # Truncado: primeras N páginas. No fallar silenciosamente.
        return list(range(settings.PDF_MAX_PAGES_FULL)), True

    return list(range(total_pages)), False


def _extract_pymupdf(content: bytes, page_indices: list[int]) -> tuple[str, int]:
    """Extrae texto con pymupdf. Devuelve (texto, total_chars)."""
    parts: list[str] = []
    total_chars = 0
    with fitz.open(stream=content, filetype="pdf") as doc:
        for idx in page_indices:
            page = doc[idx]
            text = page.get_text("text") or ""
            total_chars += len(text.strip())
            parts.append(_page_marker(idx) + "\n" + text)
    return "\n".join(parts), total_chars


def _extract_pdfplumber(content: bytes, page_indices: list[int]) -> tuple[str, int]:
    """Extrae texto con pdfplumber (mejor layout espacial). Devuelve (texto, total_chars)."""
    parts: list[str] = []
    total_chars = 0
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for idx in page_indices:
            page = pdf.pages[idx]
            text = page.extract_text() or ""
            total_chars += len(text.strip())
            parts.append(_page_marker(idx) + "\n" + text)
    return "\n".join(parts), total_chars


def _page_marker(idx: int) -> str:
    """Separador de página legible por el LLM (1-indexado)."""
    return f"--- Página {idx + 1} ---"


def _detect_native_text(content: bytes) -> bool:
    """True si el PDF tiene texto embebido detectable en alguna página.

    Umbral por página: PDF_MIN_CHARS_PER_PAGE / 10. Si ninguna página supera ese
    mínimo, se clasifica como "sin texto nativo".
    """
    per_page_min = max(1, settings.PDF_MIN_CHARS_PER_PAGE // 10)
    with fitz.open(stream=content, filetype="pdf") as doc:
        for page in doc:
            if len((page.get_text("text") or "").strip()) >= per_page_min:
                return True
    return False


def extract_text(
    content: bytes,
    page_range: tuple[int, int] | None = None,
) -> ExtractionResult:
    """Ejecuta el pipeline de extracción con criterio objetivo de fallback.

    Lanza NoNativeTextError (400) si el documento no tiene texto nativo.
    """
    # Detección temprana de PDF sin texto nativo (candidato a OCR en Fase 2).
    if not _detect_native_text(content):
        log(
            logger,
            logging.WARNING,
            "PDF sin texto nativo detectado",
        )
        raise NoNativeTextError()

    with fitz.open(stream=content, filetype="pdf") as doc:
        total_pages = doc.page_count
    page_indices, truncated = _resolve_page_indices(total_pages, page_range)
    pages_processed = len(page_indices)

    if truncated:
        log(
            logger,
            logging.WARNING,
            "documento truncado para envío al LLM",
            total_pages=total_pages,
            pages_processed=pages_processed,
            max_pages_full=settings.PDF_MAX_PAGES_FULL,
        )

    # --- Intento primario: pymupdf ---
    text, total_chars = _extract_pymupdf(content, page_indices)
    chars_per_page = total_chars / pages_processed if pages_processed else 0.0

    if chars_per_page >= settings.PDF_MIN_CHARS_PER_PAGE:
        log(
            logger,
            logging.INFO,
            "extracción con pymupdf",
            extractor_used="pymupdf",
            fallback_reason=None,
            chars_per_page=round(chars_per_page, 1),
        )
        return ExtractionResult(
            text=text,
            pages_processed=pages_processed,
            extractor_used="pymupdf",
            fallback_reason=None,
            truncated=truncated,
        )

    # --- Fallback: pdfplumber ---
    fallback_reason = (
        f"pymupdf: {chars_per_page:.1f} chars/pág < umbral "
        f"{settings.PDF_MIN_CHARS_PER_PAGE}"
    )
    log(
        logger,
        logging.INFO,
        "fallback a pdfplumber",
        extractor_used="pdfplumber",
        fallback_reason=fallback_reason,
    )

    pp_text, pp_chars = _extract_pdfplumber(content, page_indices)
    pp_chars_per_page = pp_chars / pages_processed if pages_processed else 0.0

    # Si ninguno extrae texto suficiente: sin texto nativo utilizable.
    if pp_chars_per_page < settings.PDF_MIN_CHARS_PER_PAGE and pp_chars == 0:
        raise NoNativeTextError()

    return ExtractionResult(
        text=pp_text,
        pages_processed=pages_processed,
        extractor_used="pdfplumber",
        fallback_reason=fallback_reason,
        truncated=truncated,
    )
