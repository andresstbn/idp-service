"""Factory de proveedores LLM.

Instancia el provider según LLM_PROVIDER en config. Si el provider configurado no
está implementado o le falta su API key, falla en startup (no en runtime), de modo
que un despliegue mal configurado nunca llega a aceptar tráfico.
"""

from __future__ import annotations

from core.config import settings
from services.llm.anthropic_provider import AnthropicProvider
from services.llm.base import BaseLLMProvider
from services.llm.mock_provider import MockProvider
from services.llm.openai_provider import OpenAIProvider

# Registro de providers implementados.
# 'mock' permite levantar el servicio sin LLM real (desarrollo / despliegue inicial).
_PROVIDERS: dict[str, type[BaseLLMProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "mock": MockProvider,
}


def create_llm_provider() -> BaseLLMProvider:
    """Crea el provider configurado. Lanza ValueError si no está soportado."""
    provider_key = settings.LLM_PROVIDER.lower()
    provider_cls = _PROVIDERS.get(provider_key)
    if provider_cls is None:
        raise ValueError(
            f"LLM_PROVIDER '{settings.LLM_PROVIDER}' no implementado. "
            f"Disponibles: {sorted(_PROVIDERS.keys())}"
        )
    return provider_cls()
