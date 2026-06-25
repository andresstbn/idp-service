"""Proveedor de visión basado en DeepSeek (API OpenAI-compatible).

Plan B del pipeline: cuando el PDF no tiene texto extraíble (escaneado) o el primer
intento dejó campos críticos vacíos, se rasteriza el documento a imagen y se envía
a un modelo multimodal. DeepSeek expone una API compatible con OpenAI, por lo que se
reutiliza el cliente `openai` ya instalado (sin dependencia nueva).

A diferencia del provider de texto (Anthropic, tool-use nativo), aquí se pide JSON
directo y se valida con jsonschema, reintentando con feedback hasta LLM_MAX_RETRIES.
No se asume soporte de tool-use sobre la capa OpenAI-compat.
"""

from __future__ import annotations

import base64
import json
import logging
import re

from jsonschema import Draft7Validator
from openai import AsyncOpenAI

from core.config import settings
from core.exceptions import LLMExtractionError
from core.logging import get_logger, log
from models.extraction import UsageMetrics
from services.llm.base import VISION_SYSTEM_PROMPT, BaseLLMProvider

logger = get_logger("idp.llm.deepseek")

_DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# Tarifas hardcodeadas por MTok (USD). Deuda técnica conocida (ver ROADMAP).
_PRICE_INPUT_PER_MTOK = 0.27
_PRICE_OUTPUT_PER_MTOK = 1.10


class DeepSeekVisionProvider(BaseLLMProvider):
    """Provider de visión vía DeepSeek. Solo implementa extract_vision."""

    def __init__(self) -> None:
        if not settings.DEEPSEEK_API_KEY:
            raise ValueError("DEEPSEEK_API_KEY no configurada para el provider deepseek")
        self._client = AsyncOpenAI(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=_DEEPSEEK_BASE_URL,
        )
        self._model = settings.VISION_MODEL
        self._max_retries = settings.LLM_MAX_RETRIES

    async def extract(self, text, schema, trace_id, hints=None):  # type: ignore[override]
        raise NotImplementedError("deepseek está configurado solo como provider de visión")

    async def extract_vision(
        self,
        images: list[tuple[str, bytes]],
        schema: dict,
        trace_id: str,
        hints: str | None = None,
    ) -> tuple[dict, UsageMetrics]:
        validator = Draft7Validator(schema)
        system = VISION_SYSTEM_PROMPT + json.dumps(schema, ensure_ascii=False)

        user_content: list[dict] = [{"type": "text", "text": _user_text(hints)}]
        for mime, data in images:
            b64 = base64.b64encode(data).decode("ascii")
            user_content.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
            )

        feedback: str | None = None
        total_input = 0
        total_output = 0

        for attempt in range(self._max_retries + 1):
            messages: list[dict] = [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ]
            if feedback is not None:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "El JSON anterior no cumplió el esquema. "
                            f"Corrige estos errores y vuelve a emitir SOLO el JSON: {feedback}"
                        ),
                    }
                )

            response = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=4096,
                messages=messages,  # type: ignore[arg-type]
                response_format={"type": "json_object"},
            )
            usage = response.usage
            if usage is not None:
                total_input += usage.prompt_tokens
                total_output += usage.completion_tokens

            data = _parse_json(response.choices[0].message.content)
            if data is None:
                feedback = "la respuesta no era un objeto JSON parseable"
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
                "output de visión no validó; reintentando",
                attempt=attempt + 1,
                validation_errors=feedback,
            )

        log(
            logger,
            logging.ERROR,
            "el modelo de visión agotó los reintentos sin output válido",
            attempts=self._max_retries + 1,
        )
        raise LLMExtractionError()


def _user_text(hints: str | None) -> str:
    base = "Extrae los campos del documento de la(s) imagen(es) y devuelve solo el JSON."
    if hints:
        base += f"\n\nPistas de contexto (no inventes valores a partir de ellas): {hints}"
    return base


def _parse_json(content: str | None) -> dict | None:
    """Parsea el JSON de la respuesta; tolera texto alrededor del objeto."""
    if not content:
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _usage(input_tokens: int, output_tokens: int) -> UsageMetrics:
    cost = (
        input_tokens / 1_000_000 * _PRICE_INPUT_PER_MTOK
        + output_tokens / 1_000_000 * _PRICE_OUTPUT_PER_MTOK
    )
    return UsageMetrics(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd_estimated=round(cost, 6),
    )
