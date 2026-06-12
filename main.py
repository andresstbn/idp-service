"""Entrypoint FastAPI del servicio IDP.

Configura el logging estructurado, carga el registro de schemas y construye el
provider LLM en el lifespan (fallando en startup si la config es inválida),
instala el middleware de trace_id y monta el router v1.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from api.v1.router import api_router
from core.config import settings
from core.logging import (
    bind_request_context,
    configure_logging,
    get_logger,
    log,
    reset_request_context,
)
from services.document_processor import DocumentProcessor
from services.llm.factory import create_llm_provider
from services.schema_registry import SchemaRegistry

logger = get_logger("idp.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa recursos al arrancar: logging, schemas y provider LLM."""
    configure_logging()

    registry = SchemaRegistry(settings.SCHEMAS_DIR)
    registry.load()

    # Falla en startup si el provider está mal configurado (no en runtime).
    llm = create_llm_provider()

    app.state.registry = registry
    app.state.processor = DocumentProcessor(registry, llm)
    app.state.started_at = time.time()

    log(
        logger,
        logging.INFO,
        "servicio IDP iniciado",
        llm_provider=settings.LLM_PROVIDER,
        llm_model=settings.LLM_MODEL,
        schemas_loaded=registry.loaded_count,
    )
    yield


app = FastAPI(
    title="IDP Service",
    version=settings.SERVICE_VERSION,
    lifespan=lifespan,
)


@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    """Genera y propaga un trace_id por request para correlación en logs."""
    trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())
    request.state.trace_id = trace_id
    bind_request_context(trace_id=trace_id)
    try:
        response = await call_next(request)
    finally:
        reset_request_context()
    response.headers["X-Trace-Id"] = trace_id
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Traduce errores de validación del request a 422 con trace_id."""
    trace_id = getattr(request.state, "trace_id", "unknown")
    return JSONResponse(
        status_code=422,
        content={
            "error": "validación del request fallida",
            "detail": exc.errors(),
            "trace_id": trace_id,
        },
    )


app.include_router(api_router)
