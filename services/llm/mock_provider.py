"""Provider LLM mock para desarrollo y despliegues sin LLM real.

Permite levantar el servicio y ejercitar el pipeline completo (validación,
extracción, normalización, contrato HTTP) sin API key ni llamadas externas.

Por cada propiedad del schema emite un valor "vacío" que respeta su tipo: null si
el campo lo permite; en caso contrario un valor neutro por tipo (array→[],
object→{}, string→"", number/integer→0, boolean→false). Así el output mock pasa la
validación jsonschema sin importar el schema registrado.

No es para producción: la data no tiene significado. Se activa con LLM_PROVIDER=mock.
Cuando el LLM real esté listo, basta cambiar la variable.
"""

from __future__ import annotations

from typing import Any

from models.extraction import UsageMetrics
from services.llm.base import BaseLLMProvider


class MockProvider(BaseLLMProvider):
    """Provider de relleno: no llama a ningún LLM. Emite valores vacíos válidos."""

    async def extract(
        self,
        text: str,
        schema: dict,
        trace_id: str,
        hints: str | None = None,
    ) -> tuple[dict, UsageMetrics]:
        properties = schema.get("properties", {})
        data: dict[str, Any] = {
            key: _empty_value(prop) for key, prop in properties.items()
        }
        usage = UsageMetrics(input_tokens=0, output_tokens=0, cost_usd_estimated=0.0)
        return data, usage


def _empty_value(prop: dict[str, Any]) -> Any:
    """Valor vacío que satisface el tipo del sub-schema (null si está permitido)."""
    declared = prop.get("type")
    types = declared if isinstance(declared, list) else [declared]

    # Si el campo admite null, es la opción más limpia.
    if "null" in types:
        return None

    # Primer tipo no-null declarado.
    primary = next((t for t in types if t and t != "null"), None)
    return {
        "array": [],
        "object": {},
        "string": "",
        "number": 0,
        "integer": 0,
        "boolean": False,
    }.get(primary, None)
