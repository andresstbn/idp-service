"""Factory de proveedores LLM.

Instancia el provider según LLM_PROVIDER en config. Si el provider configurado no
está implementado o le falta su API key, falla en startup (no en runtime), de modo
que un despliegue mal configurado nunca llega a aceptar tráfico.
"""

from __future__ import annotations

from core.config import settings
from services.llm.anthropic_provider import AnthropicProvider
from services.llm.base import BaseLLMProvider
from services.llm.deepseek_provider import DeepSeekVisionProvider
from services.llm.mock_provider import MockProvider
from services.llm.openai_provider import OpenAIProvider

# Registro de providers implementados.
# 'mock' permite levantar el servicio sin LLM real (desarrollo / despliegue inicial).
_PROVIDERS: dict[str, type[BaseLLMProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "mock": MockProvider,
}

# Providers de visión (fallback). 'mock' ejercita el pipeline sin API key.
_VISION_PROVIDERS: dict[str, type[BaseLLMProvider]] = {
    "deepseek": DeepSeekVisionProvider,
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


def create_vision_provider() -> BaseLLMProvider | None:
    """Crea el provider de visión, o None si el fallback está deshabilitado.

    VISION_PROVIDER vacío deshabilita el fallback (comportamiento Fase 1). Si está
    mal configurado, falla en startup (no en runtime).
    """
    provider_key = settings.VISION_PROVIDER.lower().strip()
    if not provider_key:
        return None
    provider_cls = _VISION_PROVIDERS.get(provider_key)
    if provider_cls is None:
        raise ValueError(
            f"VISION_PROVIDER '{settings.VISION_PROVIDER}' no implementado. "
            f"Disponibles: {sorted(_VISION_PROVIDERS.keys())}"
        )
    return provider_cls()
