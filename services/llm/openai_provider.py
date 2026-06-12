"""Implementación del proveedor OpenAI.

Usa el cliente async de OpenAI (vía instructor para la construcción) con function
calling: se declara una función cuyos parámetros SON el JSON Schema registrado.
El modelo devuelve un dict que se valida con jsonschema; ante error de validación
se reintenta (hasta LLM_MAX_RETRIES) realimentando el error.
"""

from __future__ import annotations

import json
import logging

import instructor
from jsonschema import Draft7Validator
from openai import AsyncOpenAI

from core.config import settings
from core.exceptions import LLMExtractionError
from core.logging import get_logger, log
from models.extraction import UsageMetrics
from services.llm.base import SYSTEM_PROMPT, BaseLLMProvider

logger = get_logger("idp.llm.openai")

# Tarifa de GPT-4o por millón de tokens (USD). Ajustable según pricing.
_PRICE_INPUT_PER_MTOK = 2.5
_PRICE_OUTPUT_PER_MTOK = 10.0


class OpenAIProvider(BaseLLMProvider):
    """Proveedor LLM basado en OpenAI vía function calling."""

    def __init__(self) -> None:
        if not settings.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY no configurada para el provider openai")
        self._raw = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        instructor.from_openai(self._raw)  # registra/valida compatibilidad
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
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "emit_extraction",
                    "description": "Emite los campos extraídos del documento según el esquema.",
                    "parameters": schema,
                },
            }
        ]

        base_messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_content(text, hints)},
        ]
        feedback: str | None = None
        total_input = 0
        total_output = 0

        for attempt in range(self._max_retries + 1):
            messages = list(base_messages)
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

            response = await self._raw.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=tools,
                tool_choice={"type": "function", "function": {"name": "emit_extraction"}},
            )
            usage = response.usage
            if usage is not None:
                total_input += usage.prompt_tokens
                total_output += usage.completion_tokens

            data = _extract_tool_arguments(response)
            if data is None:
                feedback = "no se recibió una llamada a la función emit_extraction"
                continue

            errors = sorted(validator.iter_errors(data), key=lambda e: e.path)
            if not errors:
                return data, _usage(total_input, total_output)

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


def _extract_tool_arguments(response) -> dict | None:
    """Recupera los argumentos de la función emit_extraction de la respuesta de OpenAI."""
    choice = response.choices[0]
    tool_calls = getattr(choice.message, "tool_calls", None)
    if not tool_calls:
        return None
    for call in tool_calls:
        if call.function.name == "emit_extraction":
            try:
                return json.loads(call.function.arguments)
            except json.JSONDecodeError:
                return None
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
