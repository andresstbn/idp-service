"""Interfaz abstracta del proveedor LLM.

El pipeline depende únicamente de esta interfaz; las implementaciones concretas
son intercambiables sin tocar document_processor.py. El output es un dict Python
validado luego con jsonschema (no Pydantic dinámico).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from models.extraction import UsageMetrics

# Prompt de sistema común: extractor estructurado estricto, sin invenciones.
SYSTEM_PROMPT = (
    "Eres un extractor de información estructurada de documentos. "
    "No eres un asistente conversacional. "
    "Tu única tarea es mapear el texto del documento al esquema solicitado.\n"
    "Reglas estrictas:\n"
    "- Los campos que no encuentres en el documento deben ser null.\n"
    "- Está prohibido inventar, inferir o completar información no presente.\n"
    "- Respeta los tipos: fechas en formato ISO 8601, números sin separadores "
    "de miles ni símbolos de moneda.\n"
    "- No agregues campos que no estén en el esquema."
)


class BaseLLMProvider(ABC):
    """Contrato que toda implementación de proveedor LLM debe cumplir."""

    @abstractmethod
    async def extract(
        self,
        text: str,
        schema: dict,
        trace_id: str,
        hints: str | None = None,
    ) -> tuple[dict, UsageMetrics]:
        """Mapea `text` al `schema` y devuelve (data, métricas de uso).

        Debe lanzar LLMExtractionError si falla tras agotar los reintentos.
        """
        ...
