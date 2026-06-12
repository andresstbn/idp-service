"""Excepciones custom del dominio IDP.

Cada excepción mapea a un código HTTP y lleva un mensaje seguro para el cliente
externo. Nunca deben contener stack traces ni raw output del LLM. Los detalles
sensibles se loggean por separado y se correlacionan vía trace_id.
"""

from __future__ import annotations


class IDPError(Exception):
    """Excepción base del dominio. status_code mapea al HTTP de respuesta."""

    status_code: int = 500
    # Mensaje seguro y descriptivo para exponer al cliente.
    public_message: str = "internal error"

    def __init__(self, public_message: str | None = None) -> None:
        if public_message is not None:
            self.public_message = public_message
        super().__init__(self.public_message)


class InvalidPDFError(IDPError):
    """El archivo no es un PDF válido (magic bytes) o supera el tamaño máximo."""

    status_code = 400
    public_message = "el archivo no es un PDF válido"


class SchemaNotFoundError(IDPError):
    """El schema_id solicitado no existe en el registro en memoria."""

    status_code = 400
    public_message = "schema_id no registrado"


class NoNativeTextError(IDPError):
    """El PDF no tiene capa de texto nativa detectable (requiere OCR en Fase 2)."""

    status_code = 400
    public_message = (
        "documento sin texto nativo detectado; requiere procesamiento OCR "
        "(disponible en Fase 2)"
    )


class LLMExtractionError(IDPError):
    """El proveedor LLM falló tras agotar los reintentos."""

    status_code = 500
    public_message = "error en la extracción del modelo"


class OutputValidationError(IDPError):
    """El output del LLM no validó contra el JSON Schema registrado."""

    status_code = 500
    public_message = "el resultado extraído no cumple el esquema registrado"
