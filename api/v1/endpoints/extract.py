"""Endpoint POST /v1/extract y GET /v1/health.

El endpoint de extracción recibe multipart/form-data, parsea las opciones,
delega en el DocumentProcessor y traduce las excepciones de dominio a respuestas
HTTP seguras (sin stack traces ni raw LLM output).
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from core.config import settings
from core.exceptions import IDPError
from core.logging import bind_request_context, get_logger, log
from models.requests import ErrorResponse, ExtractOptions
from services.document_processor import DocumentProcessor

router = APIRouter()
logger = get_logger("idp.api")


@router.post("/extract")
async def extract(
    request: Request,
    file: UploadFile = File(...),
    schema_id: str = Form(...),
    project_id: str = Form(...),
    options: str | None = Form(None),
) -> JSONResponse:
    """Extrae datos estructurados de un PDF según un schema registrado."""
    trace_id = getattr(request.state, "trace_id", "unknown")
    bind_request_context(trace_id=trace_id, project_id=project_id, schema_id=schema_id)

    # Parseo de opciones: JSON inválido o estructura inválida → 422.
    try:
        parsed_options = (
            ExtractOptions.model_validate(json.loads(options))
            if options
            else ExtractOptions()
        )
    except (json.JSONDecodeError, ValidationError) as exc:
        return _error(422, "opciones inválidas", str(exc), trace_id)

    content = await file.read()

    processor: DocumentProcessor = request.app.state.processor
    try:
        result = await processor.process(
            content=content,
            schema_id=schema_id,
            project_id=project_id,
            trace_id=trace_id,
            options=parsed_options,
        )
    except IDPError as exc:
        # Excepciones de dominio: mensaje seguro, código mapeado.
        if exc.status_code >= 500:
            log(
                logger,
                logging.ERROR,
                "error interno procesando el request",
                error_type=type(exc).__name__,
            )
        return _error(exc.status_code, exc.public_message, None, trace_id)
    except Exception:  # noqa: BLE001 — barrera final: nunca exponer trazas al cliente.
        log(
            logger,
            logging.ERROR,
            "excepción no controlada en el pipeline",
            exc_info=True,
        )
        return _error(500, "error interno", None, trace_id)

    return JSONResponse(status_code=200, content=result.model_dump())


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    """Health check con estado del registro de schemas y configuración del LLM."""
    registry = request.app.state.registry
    started_at: float = request.app.state.started_at

    dir_accessible = registry.dir_accessible()
    schemas_available = registry.list()
    status = "ok"
    if not dir_accessible or registry.loaded_count == 0:
        status = "degraded"

    body = {
        "status": status,
        "version": settings.SERVICE_VERSION,
        "environment": settings.ENVIRONMENT,
        "llm_provider": settings.LLM_PROVIDER,
        "llm_model": settings.LLM_MODEL,
        "schemas_loaded": registry.loaded_count,
        "schemas_available": schemas_available,
        "schemas_dir_accessible": dir_accessible,
        "uptime_seconds": int(time.time() - started_at),
    }
    return JSONResponse(status_code=200, content=body)


def _error(
    status_code: int,
    message: str,
    detail: str | None,
    trace_id: str,
) -> JSONResponse:
    """Construye una respuesta de error estándar con trace_id."""
    payload = ErrorResponse(error=message, detail=detail, trace_id=trace_id)
    return JSONResponse(status_code=status_code, content=payload.model_dump())
