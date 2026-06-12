"""Registro de esquemas JSON cargados desde disco.

Los esquemas se cargan una sola vez en el lifespan de FastAPI. Cada archivo se
valida contra el meta-schema de JSON Schema Draft 7; los inválidos se excluyen
del registro sin tumbar el servicio. El registro vive en memoria (sin DB en Fase 1).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError

from core.logging import get_logger, log

logger = get_logger("idp.schema_registry")


class SchemaRegistry:
    """Almacena los JSON Schemas válidos indexados por su id (nombre de archivo sin .json)."""

    def __init__(self, schemas_dir: str) -> None:
        self._dir = Path(schemas_dir)
        self._schemas: dict[str, dict[str, Any]] = {}
        self._loaded = 0
        self._failed = 0

    def load(self) -> None:
        """Carga y valida todos los .json del directorio. Idempotente."""
        self._schemas.clear()
        self._loaded = 0
        self._failed = 0

        if not self._dir.is_dir():
            log(
                logger,
                logging.ERROR,
                "directorio de schemas inaccesible",
                schemas_dir=str(self._dir),
            )
            return

        for path in sorted(self._dir.glob("*.json")):
            schema_id = path.stem
            try:
                with path.open("r", encoding="utf-8") as fh:
                    schema = json.load(fh)
                # Valida que el propio schema sea un JSON Schema Draft 7 correcto.
                Draft7Validator.check_schema(schema)
            except (json.JSONDecodeError, SchemaError, OSError) as exc:
                self._failed += 1
                log(
                    logger,
                    logging.ERROR,
                    "schema inválido excluido del registro",
                    schema_file=path.name,
                    error=str(exc),
                )
                continue

            self._schemas[schema_id] = schema
            self._loaded += 1

        log(
            logger,
            logging.INFO,
            "carga de schemas finalizada",
            schemas_loaded=self._loaded,
            schemas_failed=self._failed,
            schemas_available=list(self._schemas.keys()),
        )

    def get(self, schema_id: str) -> dict[str, Any] | None:
        """Devuelve el schema por id, o None si no existe."""
        return self._schemas.get(schema_id)

    def list(self) -> list[str]:
        """IDs de schemas disponibles."""
        return sorted(self._schemas.keys())

    def dir_accessible(self) -> bool:
        """Verifica en runtime que el directorio de schemas sea legible."""
        return self._dir.is_dir() and os.access(self._dir, os.R_OK)

    @property
    def loaded_count(self) -> int:
        return self._loaded
