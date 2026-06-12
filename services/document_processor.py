"""Orquestador principal del pipeline de procesamiento de documentos.

Ejecuta los pasos secuenciales descritos en la especificación, registra la
duración de cada uno y ensambla la respuesta. Es agnóstico al provider LLM: solo
depende de BaseLLMProvider, por lo que la capa LLM es intercambiable sin tocar
este archivo.

Cuando el schema declara `"x-extractor": "<nombre>"`, el pipeline despacha a un
extractor rule-based específico en lugar del LLM. Los extractores viven en
services/extractors/ e implementan la firma `extract(content: bytes) -> dict`.
Esto permite documentos con geometría conocida (ej: formularios DIAN) sin costo
de inferencia LLM.

Fase 2 podrá insertar el paso OCR dentro de pdf_extractor sin modificar esta
orquestación ni la interfaz pública.
"""

from __future__ import annotations

import logging
import time
from importlib import import_module
from typing import Any

from jsonschema import Draft7Validator

from core.exceptions import OutputValidationError, SchemaNotFoundError
from core.logging import get_logger, log
from models.requests import (
    ExtractionMetadata,
    ExtractOptions,
    ExtractResponse,
    FieldMetadata,
    TokenUsage,
)
from services import normalizer, pdf_extractor
from services.llm.base import BaseLLMProvider
from services.schema_registry import SchemaRegistry

logger = get_logger("idp.processor")


def _clean_text(text: str) -> str:
    """Normalización pre-LLM: elimina caracteres de control, normaliza espacios."""
    # Conserva saltos de línea y tabs; elimina otros controles.
    cleaned_chars = [
        ch for ch in text if ch in ("\n", "\t") or ch >= " "
    ]
    cleaned = "".join(cleaned_chars)
    # Colapsa espacios horizontales múltiples sin tocar los saltos de línea.
    lines = [
        " ".join(segment.split(" ")).rstrip()
        for segment in cleaned.split("\n")
    ]
    return "\n".join(lines).strip()


class DocumentProcessor:
    """Orquesta el pipeline VALIDATE → … → ASSEMBLE para un request de extracción."""

    def __init__(self, registry: SchemaRegistry, llm: BaseLLMProvider) -> None:
        self._registry = registry
        self._llm = llm

    async def process(
        self,
        *,
        content: bytes,
        schema_id: str,
        project_id: str,
        trace_id: str,
        options: ExtractOptions,
    ) -> ExtractResponse:
        """Ejecuta el pipeline completo y devuelve la respuesta ensamblada.

        Si el schema declara `"x-extractor": "<nombre>"`, se usa un extractor
        rule-based del paquete `services.extractors.<nombre>` en lugar del LLM.
        """
        t_start = time.perf_counter()

        # 1. VALIDATE — magic bytes y tamaño (lanza InvalidPDFError → 400).
        pdf_extractor.validate_pdf_bytes(content)

        # 2. LOAD SCHEMA — del registro en memoria.
        schema = self._registry.get(schema_id)
        if schema is None:
            available = self._registry.list()
            raise SchemaNotFoundError(
                f"schema_id '{schema_id}' no registrado. Disponibles: {available}"
            )

        # 3. DISPATCH — rule-based o LLM según el schema.
        rule_extractor_name: str | None = schema.get("x-extractor")

        if rule_extractor_name:
            return await self._process_rule_based(
                content=content,
                schema=schema,
                schema_id=schema_id,
                project_id=project_id,
                extractor_name=rule_extractor_name,
                t_start=t_start,
            )
        else:
            return await self._process_llm(
                content=content,
                schema=schema,
                schema_id=schema_id,
                project_id=project_id,
                trace_id=trace_id,
                options=options,
                t_start=t_start,
            )

    # ------------------------------------------------------------------
    # Pipeline rule-based (sin LLM)
    # ------------------------------------------------------------------

    async def _process_rule_based(
        self,
        *,
        content: bytes,
        schema: dict[str, Any],
        schema_id: str,
        project_id: str,
        extractor_name: str,
        t_start: float,
    ) -> ExtractResponse:
        """Pipeline para schemas con extractor rule-based.

        VALIDATE → LOAD SCHEMA → RULE EXTRACT → NORMALIZE → VALIDATE OUTPUT → ASSEMBLE
        """
        # Cargar el módulo extractor dinámicamente.
        module_path = f"services.extractors.{extractor_name}"
        try:
            extractor_module = import_module(module_path)
        except ImportError as exc:
            raise SchemaNotFoundError(
                f"extractor '{extractor_name}' no encontrado en {module_path}: {exc}"
            ) from exc

        # 3. RULE EXTRACT — el extractor accede directamente al PDF con PyMuPDF.
        t0 = time.perf_counter()
        raw_data: dict[str, Any] = extractor_module.extract(content)
        extraction_time_ms = _ms(t0)

        # 4. NORMALIZE OUTPUT — normalización schema-driven.
        t0 = time.perf_counter()
        data = normalizer.normalize(raw_data, schema)
        normalization_time_ms = _ms(t0)

        # 5. VALIDATE OUTPUT — validación final contra el JSON Schema.
        errors = sorted(
            Draft7Validator(schema).iter_errors(data), key=lambda e: e.path
        )
        if errors:
            detail = "; ".join(
                f"{'/'.join(str(p) for p in e.path) or '(raíz)'}: {e.message}"
                for e in errors[:10]
            )
            log(
                logger,
                logging.WARNING,
                "output rule-based no validó contra el schema",
                validation_errors=detail,
                raw_extractor_output=raw_data,
            )
            raise OutputValidationError()

        # 6. ASSEMBLE RESPONSE.
        processing_time_ms = _ms(t_start)
        field_metadata = {
            key: FieldMetadata(confidence=None)
            for key in schema.get("properties", {})
        }

        metadata = ExtractionMetadata(
            pages_processed=1,
            extractor_used=extractor_name,
            fallback_reason=None,
            processing_time_ms=processing_time_ms,
            extraction_time_ms=extraction_time_ms,
            llm_time_ms=0,
            normalization_time_ms=normalization_time_ms,
            llm_provider="none",
            llm_model="none",
            token_usage=TokenUsage(input=0, output=0),
            cost_usd_estimated=0.0,
            confidence_available=False,
            truncated=False,
        )

        log(
            logger,
            logging.INFO,
            "request rule-based completado",
            processing_time_ms=processing_time_ms,
            extractor_used=extractor_name,
        )

        return ExtractResponse(
            schema_id=schema_id,
            project_id=project_id,
            extraction_metadata=metadata,
            data=data,
            field_metadata=field_metadata,
        )

    # ------------------------------------------------------------------
    # Pipeline LLM (comportamiento original)
    # ------------------------------------------------------------------

    async def _process_llm(
        self,
        *,
        content: bytes,
        schema: dict[str, Any],
        schema_id: str,
        project_id: str,
        trace_id: str,
        options: ExtractOptions,
        t_start: float,
    ) -> ExtractResponse:
        """Pipeline LLM original: EXTRACT TEXT → CLEAN → LLM → NORMALIZE → VALIDATE."""
        # 3. EXTRACT TEXT — pymupdf → pdfplumber (lanza NoNativeTextError → 400).
        t0 = time.perf_counter()
        extraction = pdf_extractor.extract_text(content, options.page_range)
        extraction_time_ms = _ms(t0)

        # 4. NORMALIZE PRE-LLM — limpieza del texto extraído.
        clean = _clean_text(extraction.text)

        # 5. LLM MAPPING — mapeo estructurado contra el schema.
        t0 = time.perf_counter()
        raw_data, usage = await self._llm.extract(
            text=clean,
            schema=schema,
            trace_id=trace_id,
            hints=options.hints,
        )
        llm_time_ms = _ms(t0)

        # 6. NORMALIZE OUTPUT — normalización schema-driven de los valores.
        t0 = time.perf_counter()
        data = normalizer.normalize(raw_data, schema)
        normalization_time_ms = _ms(t0)

        # 7. VALIDATE OUTPUT — validación final contra el JSON Schema.
        errors = sorted(
            Draft7Validator(schema).iter_errors(data), key=lambda e: e.path
        )
        if errors:
            detail = "; ".join(
                f"{'/'.join(str(p) for p in e.path) or '(raíz)'}: {e.message}"
                for e in errors[:10]
            )
            log(
                logger,
                logging.WARNING,
                "output normalizado no validó contra el schema",
                validation_errors=detail,
                raw_llm_output=raw_data,
            )
            raise OutputValidationError()

        # 8. ASSEMBLE RESPONSE.
        processing_time_ms = _ms(t_start)
        field_metadata = {
            key: FieldMetadata(confidence=None)
            for key in schema.get("properties", {})
        }

        metadata = ExtractionMetadata(
            pages_processed=extraction.pages_processed,
            extractor_used=extraction.extractor_used,
            fallback_reason=extraction.fallback_reason,
            processing_time_ms=processing_time_ms,
            extraction_time_ms=extraction_time_ms,
            llm_time_ms=llm_time_ms,
            normalization_time_ms=normalization_time_ms,
            llm_provider=_provider_name(),
            llm_model=_model_name(),
            token_usage=TokenUsage(input=usage.input_tokens, output=usage.output_tokens),
            cost_usd_estimated=usage.cost_usd_estimated,
            confidence_available=False,
            truncated=extraction.truncated,
        )

        log(
            logger,
            logging.INFO,
            "request de extracción completado",
            processing_time_ms=processing_time_ms,
            extractor_used=extraction.extractor_used,
            fallback_reason=extraction.fallback_reason,
            llm_provider=_provider_name(),
            llm_model=_model_name(),
            token_usage_input=usage.input_tokens,
            token_usage_output=usage.output_tokens,
            cost_usd_estimated=usage.cost_usd_estimated,
        )

        return ExtractResponse(
            schema_id=schema_id,
            project_id=project_id,
            extraction_metadata=metadata,
            data=data,
            field_metadata=field_metadata,
        )


def _ms(start: float) -> int:
    """Milisegundos transcurridos desde `start` (perf_counter)."""
    return int((time.perf_counter() - start) * 1000)


def _provider_name() -> str:
    from core.config import settings

    return settings.LLM_PROVIDER


def _model_name() -> str:
    from core.config import settings

    return settings.LLM_MODEL
