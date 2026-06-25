"""Tests del fallback a visión (Plan B) y del soporte de imágenes.

Cubre: detección de tipo de archivo por magic bytes, render a imagen, la regla de
campos críticos (`x-vision-fallback-fields`) y los tres triggers del orquestador
(imagen directa, sin texto nativo → incompleto, e intento incompleto). Usa un
provider de visión mock y PDFs/imágenes generados en memoria: sin API key ni PII.

Corre con: `python -m unittest discover -s tests`  (o pytest).
"""

from __future__ import annotations

import unittest

import fitz  # pymupdf

from core.config import settings
from core.exceptions import InvalidPDFError
from models.requests import ExtractOptions
from services import pdf_extractor
from services.document_processor import DocumentProcessor
from services.llm.mock_provider import MockProvider
from services.schema_registry import SchemaRegistry


def _blank_pdf(pages: int = 1) -> bytes:
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def _png() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    data = page.get_pixmap(dpi=36).tobytes("png")
    doc.close()
    return data


class TestFileDetection(unittest.TestCase):
    def test_detecta_pdf(self) -> None:
        self.assertEqual(pdf_extractor.validate_input(_blank_pdf()), "pdf")

    def test_detecta_imagen_png(self) -> None:
        self.assertEqual(pdf_extractor.validate_input(_png()), "image")
        self.assertEqual(pdf_extractor._image_mime(_png()), "image/png")

    def test_archivo_no_soportado_lanza_400(self) -> None:
        with self.assertRaises(InvalidPDFError):
            pdf_extractor.validate_input(b"esto no es ni pdf ni imagen")


class TestRenderImages(unittest.TestCase):
    def test_imagen_se_pasa_tal_cual(self) -> None:
        png = _png()
        result = pdf_extractor.render_images(png, "image", max_pages=2, dpi=72)
        self.assertEqual(result, [("image/png", png)])

    def test_pdf_respeta_max_pages(self) -> None:
        result = pdf_extractor.render_images(_blank_pdf(pages=3), "pdf", max_pages=1, dpi=72)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], "image/png")


class TestNeedsVision(unittest.TestCase):
    def test_campo_critico_vacio_dispara(self) -> None:
        schema = {"x-vision-fallback-fields": ["nit", "person_type"]}
        self.assertTrue(DocumentProcessor._needs_vision({"nit": None, "person_type": "NAT"}, schema))

    def test_campos_criticos_presentes_no_dispara(self) -> None:
        schema = {"x-vision-fallback-fields": ["nit"]}
        self.assertFalse(DocumentProcessor._needs_vision({"nit": "123"}, schema))

    def test_sin_campos_declarados_no_dispara(self) -> None:
        self.assertFalse(DocumentProcessor._needs_vision({"nit": None}, {}))


class TestVisionTriggers(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.reg = SchemaRegistry(settings.SCHEMAS_DIR)
        self.reg.load()

    async def _process(self, proc, content):
        return await proc.process(
            content=content, schema_id="rut", project_id="test",
            trace_id="t", options=ExtractOptions(),
        )

    async def test_pdf_sin_datos_cae_a_vision(self) -> None:
        # PDF en blanco: el rule-based deja nit/person_type en null → fallback.
        proc = DocumentProcessor(self.reg, MockProvider(), MockProvider())
        resp = await self._process(proc, _blank_pdf())
        self.assertEqual(resp.extraction_metadata.extractor_used, "vision")

    async def test_imagen_va_directo_a_vision(self) -> None:
        proc = DocumentProcessor(self.reg, MockProvider(), MockProvider())
        resp = await self._process(proc, _png())
        self.assertEqual(resp.extraction_metadata.extractor_used, "vision")

    async def test_imagen_sin_vision_configurada_es_400(self) -> None:
        proc = DocumentProcessor(self.reg, MockProvider(), None)
        with self.assertRaises(InvalidPDFError):
            await self._process(proc, _png())


if __name__ == "__main__":
    unittest.main()
