"""Configuración central del servicio IDP.

Lee todas las variables desde el entorno usando pydantic-settings. Esta es la
única fuente de configuración del servicio; ningún módulo debe leer os.environ
directamente.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings del servicio, poblados desde variables de entorno."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM (texto) ---
    LLM_PROVIDER: str = "anthropic"  # anthropic | openai
    LLM_MODEL: str = "claude-sonnet-4-20250514"
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    LLM_MAX_RETRIES: int = 2

    # --- Visión (fallback cuando el texto no es extraíble; Fase 2) ---
    VISION_PROVIDER: str = ""  # "" deshabilita el fallback | deepseek | mock
    VISION_MODEL: str = "deepseek-chat"
    DEEPSEEK_API_KEY: str = ""
    VISION_MAX_PAGES: int = 2  # ponytail: RUT/factura caben en pág 1-2; sube si hace falta
    VISION_RENDER_DPI: int = 200

    # --- Extracción ---
    SCHEMAS_DIR: str = "schemas"
    PDF_MIN_CHARS_PER_PAGE: int = 50  # umbral objetivo para fallback de extractor
    PDF_MAX_SIZE_MB: int = 20
    PDF_MAX_PAGES_FULL: int = 30  # páginas máximas para envío completo al LLM

    # --- Servicio ---
    LOG_LEVEL: str = "INFO"
    ENVIRONMENT: str = "development"  # development | production
    SERVICE_VERSION: str = "1.0.0"


# Instancia única reutilizable en toda la aplicación.
settings = Settings()
