"""Modelos internos del pipeline de extracción.

Estos modelos circulan entre los pasos del pipeline; no se exponen directamente
en la API. UsageMetrics es el contrato que devuelve cada provider LLM.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UsageMetrics:
    """Métricas de uso devueltas por un provider LLM. Cada provider calcula su costo."""

    input_tokens: int
    output_tokens: int
    cost_usd_estimated: float


@dataclass
class ExtractionResult:
    """Resultado del paso de extracción de texto del PDF."""

    text: str
    pages_processed: int
    extractor_used: str  # pymupdf | pdfplumber
    fallback_reason: str | None
    truncated: bool
