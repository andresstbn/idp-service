"""Tests de los endpoints de la API (health, extract, validaciones).

Cumplen con el flujo de verificación obligatorio en AGENTS.md.
"""

from __future__ import annotations

import unittest
from fastapi.testclient import TestClient

import main
from models.extraction import UsageMetrics
from tests.test_vision_fallback import _blank_pdf


class TestAPI(unittest.TestCase):
    def setUp(self) -> None:
        # Usamos el TestClient como context manager para activar los eventos de lifespan.
        self.client_ctx = TestClient(main.app)
        self.client = self.client_ctx.__enter__()

    def tearDown(self) -> None:
        self.client_ctx.__exit__(None, None, None)

    def test_health_endpoint(self) -> None:
        response = self.client.get("/v1/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["llm_provider"], "mock")  # En test corre con mock por defecto

    def test_extract_invalid_schema(self) -> None:
        pdf_bytes = _blank_pdf()
        response = self.client.post(
            "/v1/extract",
            files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
            data={"schema_id": "non_existent_schema", "project_id": "test_project"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
        self.assertIn("schema_id", response.json()["error"])

    def test_extract_invalid_pdf_bytes(self) -> None:
        response = self.client.post(
            "/v1/extract",
            files={"file": ("test.txt", b"not a pdf or image", "text/plain")},
            data={"schema_id": "rut", "project_id": "test_project"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
        self.assertIn("no es un PDF ni una imagen soportada", response.json()["error"])

    def test_extract_invalid_options_json(self) -> None:
        pdf_bytes = _blank_pdf()
        response = self.client.post(
            "/v1/extract",
            files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
            data={
                "schema_id": "rut",
                "project_id": "test_project",
                "options": "{invalid_json",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("error", response.json())
        self.assertEqual(response.json()["error"], "opciones inválidas")

    def test_extract_invalid_options_structure(self) -> None:
        pdf_bytes = _blank_pdf()
        response = self.client.post(
            "/v1/extract",
            files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
            data={
                "schema_id": "rut",
                "project_id": "test_project",
                "options": '{"page_range": "not_an_array"}',
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("error", response.json())
        self.assertEqual(response.json()["error"], "opciones inválidas")


if __name__ == "__main__":
    unittest.main()
