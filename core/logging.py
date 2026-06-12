"""Logging estructurado JSON para Cloud Logging.

Emite cada registro como una línea JSON en stdout. Los campos numéricos de los
logs de finalización de request son la capa de métricas de Fase 1: Cloud
Monitoring puede construir dashboards a partir de ellos sin infraestructura extra.

El trace_id se propaga vía ContextVar para que cualquier punto del pipeline
pueda loggear sin tener que recibir el trace_id como parámetro explícito.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from core.config import settings

# Contexto del request actual; lo setea el middleware al inicio de cada request.
_trace_id_ctx: ContextVar[str | None] = ContextVar("trace_id", default=None)
_project_id_ctx: ContextVar[str | None] = ContextVar("project_id", default=None)
_schema_id_ctx: ContextVar[str | None] = ContextVar("schema_id", default=None)


def bind_request_context(
    trace_id: str | None = None,
    project_id: str | None = None,
    schema_id: str | None = None,
) -> None:
    """Asocia el contexto del request en curso a los logs subsiguientes."""
    if trace_id is not None:
        _trace_id_ctx.set(trace_id)
    if project_id is not None:
        _project_id_ctx.set(project_id)
    if schema_id is not None:
        _schema_id_ctx.set(schema_id)


def reset_request_context() -> None:
    """Limpia el contexto al finalizar el request."""
    _trace_id_ctx.set(None)
    _project_id_ctx.set(None)
    _schema_id_ctx.set(None)


class JSONFormatter(logging.Formatter):
    """Formatea cada LogRecord como una línea JSON apta para Cloud Logging."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            )[:-3]
            + "Z",
            "level": record.levelname,
            "message": record.getMessage(),
            "trace_id": _trace_id_ctx.get(),
            "project_id": _project_id_ctx.get(),
            "schema_id": _schema_id_ctx.get(),
            "environment": settings.ENVIRONMENT,
        }

        # Campos extra explícitos pasados vía logger.info(..., extra={"extra_fields": {...}})
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)

        if record.exc_info:
            # Solo para logs internos; nunca se expone al cliente.
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    """Configura el root logger con el JSONFormatter. Idempotente."""
    root = logging.getLogger()
    root.setLevel(settings.LOG_LEVEL.upper())

    # Evita handlers duplicados en hot-reload.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)


def get_logger(name: str = "idp") -> logging.Logger:
    """Devuelve un logger con el contexto de request inyectado por el formatter."""
    return logging.getLogger(name)


def log(
    logger: logging.Logger,
    level: int,
    message: str,
    **fields: Any,
) -> None:
    """Helper para loggear con campos estructurados adicionales."""
    logger.log(level, message, extra={"extra_fields": fields})
