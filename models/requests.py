"""Modelos Pydantic v2 para request y response HTTP del endpoint /v1/extract.

Los campos del multipart se reciben en el endpoint; aquí se modelan las opciones
parseadas y la estructura completa de la respuesta. La validación Pydantic de las
opciones produce 422 si el JSON es inválido en estructura.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ExtractOptions(BaseModel):
    """Opciones opcionales del request, parseadas desde el campo `options` (JSON)."""

    # Rango de páginas 1-indexado inclusivo [inicio, fin].
    page_range: tuple[int, int] | None = None
    # Pistas de texto libre para guiar al LLM (no se inventan valores con ellas).
    hints: str | None = None


class TokenUsage(BaseModel):
    """Conteo de tokens reportado por el provider LLM."""

    input: int
    output: int


class ExtractionMetadata(BaseModel):
    """Metadatos de la extracción, incluidos tiempos y métricas de costo."""

    pages_processed: int
    extractor_used: str  # pymupdf | pdfplumber
    fallback_reason: str | None = None
    processing_time_ms: int
    extraction_time_ms: int
    llm_time_ms: int
    normalization_time_ms: int
    llm_provider: str
    llm_model: str
    token_usage: TokenUsage
    cost_usd_estimated: float
    confidence_available: bool = False
    truncated: bool = False


class FieldMetadata(BaseModel):
    """Metadata por campo. confidence es null en Fase 1; el contrato existe para Fase 2."""

    confidence: float | None = None


class ExtractResponse(BaseModel):
    """Respuesta completa del endpoint /v1/extract."""

    schema_id: str
    project_id: str
    extraction_metadata: ExtractionMetadata
    data: dict[str, Any]
    field_metadata: dict[str, FieldMetadata]


class ErrorResponse(BaseModel):
    """Respuesta de error estándar. Siempre incluye trace_id para correlación."""

    error: str
    detail: str | None = None
    trace_id: str
