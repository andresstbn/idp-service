"""Normalización schema-driven del output del LLM antes de la validación final.

Las reglas se leen de la metadata del campo en el JSON Schema (x-locale, x-format).
Si no hay metadata explícita, se aplica normalización conservadora por tipo JSON
Schema. Un valor que no puede normalizarse se conserva como string original y se
loggea un WARNING; el pipeline no falla aquí (lo detectará la validación posterior).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from core.logging import get_logger, log

logger = get_logger("idp.normalizer")

# Locales conocidos y sus separadores (miles, decimal).
_LOCALE_SEPARATORS: dict[str, tuple[str, str]] = {
    "es-CO": (".", ","),
    "es-ES": (".", ","),
    "es-MX": (",", "."),
    "en-US": (",", "."),
}

# Formatos de fecha comunes a intentar al normalizar a ISO 8601.
_DATE_FORMATS = (
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%Y/%m/%d",
    "%Y-%m-%d",
    "%d.%m.%Y",
    "%m/%d/%Y",
)


def normalize(data: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Normaliza el dict del LLM según los tipos/metadata del schema."""
    properties = schema.get("properties", {})
    result: dict[str, Any] = {}
    for key, value in data.items():
        field_schema = properties.get(key, {})
        result[key] = _normalize_value(key, value, field_schema)
    return result


def _primary_type(declared: Any) -> str | None:
    """Devuelve el tipo JSON Schema relevante, ignorando 'null' en uniones.

    Los schemas suelen declarar tipos anulables como ["number", "null"]; aquí se
    toma el primer tipo no-null para decidir la normalización.
    """
    if isinstance(declared, list):
        for t in declared:
            if t != "null":
                return t
        return None
    return declared


def _normalize_value(
    field: str,
    value: Any,
    field_schema: dict[str, Any],
) -> Any:
    """Normaliza un único valor según su sub-schema. Conserva original si falla."""
    if value is None:
        return None

    json_type = _primary_type(field_schema.get("type"))
    json_format = field_schema.get("format")
    x_strip = field_schema.get("x-strip")

    try:
        # x-strip: "non-digits" → conservar solo dígitos (NIT, teléfonos, códigos CIIU).
        # Se aplica ANTES que el resto para que el valor resultante sea un string limpio.
        if x_strip == "non-digits":
            digits = re.sub(r"\D", "", str(value))
            return digits if digits else None

        if json_format == "date":
            return _normalize_date(value)
        if json_format == "date-time":
            return _normalize_datetime(value)
        if json_type == "number":
            return _normalize_number(value, field_schema)
        if json_type == "integer":
            return int(_normalize_number(value, field_schema))
        if json_type == "array":
            return _normalize_array(field, value, field_schema)
        if json_type == "object":
            return _normalize_object(value, field_schema)
        if json_type == "string":
            return value.strip() if isinstance(value, str) else value
    except (ValueError, TypeError) as exc:
        # No fallar el pipeline: conservar original y loggear.
        log(
            logger,
            logging.WARNING,
            "no se pudo normalizar el campo; se conserva el valor original",
            field=field,
            original_value=str(value),
            error=str(exc),
        )
        return value

    return value


def _normalize_number(value: Any, field_schema: dict[str, Any]) -> float:
    """Convierte a float removiendo símbolos de moneda y separadores de miles."""
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    # Remover todo lo que no sea dígito, separadores o signo.
    text = re.sub(r"[^\d,.\-]", "", text)

    locale = field_schema.get("x-locale")
    if locale and locale in _LOCALE_SEPARATORS:
        thousands, decimal = _LOCALE_SEPARATORS[locale]
        text = text.replace(thousands, "")
        text = text.replace(decimal, ".")
    else:
        text = _autodetect_number(text)

    return float(text)


def _autodetect_number(text: str) -> str:
    """Detecta el formato numérico cuando no hay x-locale.

    Asume que el último separador (',' o '.') es el decimal y el resto miles.
    """
    last_comma = text.rfind(",")
    last_dot = text.rfind(".")

    if last_comma == -1 and last_dot == -1:
        return text
    if last_comma > last_dot:
        # La coma es el decimal.
        return text.replace(".", "").replace(",", ".")
    # El punto es el decimal.
    return text.replace(",", "")


def _normalize_date(value: Any) -> str:
    """Normaliza una fecha a ISO 8601 (YYYY-MM-DD)."""
    text = str(value).strip()
    # Ya viene en ISO: validar y devolver.
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"formato de fecha no reconocido: {text}")


def _normalize_datetime(value: Any) -> str:
    """Normaliza un datetime a ISO 8601 con timezone."""
    text = str(value).strip()
    # Soportar el sufijo Z de UTC.
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed.isoformat()


def _normalize_array(
    field: str,
    value: Any,
    field_schema: dict[str, Any],
) -> Any:
    """Normaliza cada elemento del array según el sub-schema de items."""
    if not isinstance(value, list):
        return value
    items_schema = field_schema.get("items", {})
    return [_normalize_value(field, item, items_schema) for item in value]


def _normalize_object(value: Any, field_schema: dict[str, Any]) -> Any:
    """Normaliza recursivamente las propiedades de un objeto anidado."""
    if not isinstance(value, dict):
        return value
    properties = field_schema.get("properties", {})
    return {
        key: _normalize_value(key, val, properties.get(key, {}))
        for key, val in value.items()
    }
