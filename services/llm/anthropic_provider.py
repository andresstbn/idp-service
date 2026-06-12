"""Implementación del proveedor Anthropic.

Usa el cliente async de Anthropic a través de instructor para la construcción del
cliente, pero la generación estructurada se hace con tool-use nativo: se declara
una herramienta cuyo input_schema ES el JSON Schema registrado. Así el modelo
devuelve un dict que mapea exactamente al esquema, sin Pydantic dinámico.

La validación del output usa jsonschema; ante un error de validación se reintenta
(hasta LLM_MAX_RETRIES) realimentando el error como feedback al modelo.
"""

from __future__ import annotations

import json
import logging

import instructor
from anthropic import AsyncAnthropic
from jsonschema import Draft7Validator

from core.config import settings
from core.exceptions import LLMExtractionError
from core.logging import get_logger, log
from models.extraction import UsageMetrics
from services.llm.base import SYSTEM_PROMPT, BaseLLMProvider

logger = get_logger("idp.llm.anthropic")

# Tarifa de Claude Sonnet por millón de tokens (USD). Ajustable según pricing.
_PRICE_INPUT_PER_MTOK = 3.0
_PRICE_OUTPUT_PER_MTOK = 15.0


class AnthropicProvider(BaseLLMProvider):
    """Proveedor LLM basado en Anthropic Claude vía tool-use."""

    def __init__(self) -> None:
        if not settings.ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY no configurada para el provider anthropic")
        # instructor envuelve el cliente async; se usa también su modo de patching.
        self._raw = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        instructor.from_anthropic(self._raw)  # registra/valida compatibilidad
        self._model = settings.LLM_MODEL
        self._max_retries = settings.LLM_MAX_RETRIES

    async def extract(
        self,
        text: str,
        schema: dict,
        trace_id: str,
        hints: str | None = None,
    ) -> tuple[dict, UsageMetrics]:
        validator = Draft7Validator(schema)
        tool = {
            "name": "emit_extraction",
            "description": "Emite los campos extraídos del documento según el esquema.",
            "input_schema": schema,
        }

        user_content = _build_user_content(text, hints)
        feedback: str | None = None
        total_input = 0
        total_output = 0

        # Intento inicial + reintentos con feedback de validación.
        for attempt in range(self._max_retries + 1):
            messages = [{"role": "user", "content": user_content}]
            if feedback is not None:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "El resultado anterior no cumplió el esquema. "
                            f"Corrige estos errores y vuelve a emitir: {feedback}"
                        ),
                    }
                )

            response = await self._raw.messages.create(
                model=self._model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=[tool],
                tool_choice={"type": "tool", "name": "emit_extraction"},
                messages=messages,
            )
            total_input += response.usage.input_tokens
            total_output += response.usage.output_tokens

            data = _extract_tool_input(response)
            if data is None:
                feedback = "no se recibió una llamada a la herramienta emit_extraction"
                continue

            errors = sorted(validator.iter_errors(data), key=lambda e: e.path)
            if not errors:
                usage = _usage(total_input, total_output)
                return data, usage

            feedback = "; ".join(
                f"{'/'.join(str(p) for p in e.path) or '(raíz)'}: {e.message}"
                for e in errors[:10]
            )
            log(
                logger,
                logging.WARNING,
                "output del LLM no validó; reintentando",
                attempt=attempt + 1,
                validation_errors=feedback,
            )

        log(
            logger,
            logging.ERROR,
            "el LLM agotó los reintentos sin producir output válido",
            attempts=self._max_retries + 1,
        )
        raise LLMExtractionError()


def _build_user_content(text: str, hints: str | None) -> str:
    """Construye el mensaje de usuario con el texto del documento y pistas opcionales."""
    parts = ["Texto del documento a extraer:\n", text]
    if hints:
        parts.append(f"\n\nPistas de contexto (no inventes valores a partir de ellas): {hints}")
    return "".join(parts)


def _extract_tool_input(response) -> dict | None:
    """Recupera el input de la tool emit_extraction de la respuesta de Anthropic."""
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "emit_extraction":
            # block.input ya es un dict; se normaliza vía json por robustez.
            return json.loads(json.dumps(block.input))
    return None


def _usage(input_tokens: int, output_tokens: int) -> UsageMetrics:
    """Calcula las métricas de uso y el costo estimado en USD."""
    cost = (
        input_tokens / 1_000_000 * _PRICE_INPUT_PER_MTOK
        + output_tokens / 1_000_000 * _PRICE_OUTPUT_PER_MTOK
    )
    return UsageMetrics(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd_estimated=round(cost, 6),
    )
