"""Cada schema en schemas/ debe cargar y ser un JSON Schema Draft 7 válido.

Es la red de seguridad mínima del registro: un schema mal formado no debe llegar
a producción (el servicio lo excluiría en silencio en el arranque).
Corre con: `python -m unittest discover -s tests`  (o pytest, si está instalado).
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft7Validator

_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"


class TestSchemas(unittest.TestCase):
    def test_dir_has_schemas(self) -> None:
        self.assertTrue(list(_SCHEMAS_DIR.glob("*.json")), "no hay schemas en schemas/")

    def test_every_schema_is_valid_draft7(self) -> None:
        for path in sorted(_SCHEMAS_DIR.glob("*.json")):
            with self.subTest(schema=path.name):
                schema = json.loads(path.read_text(encoding="utf-8"))
                # Lanza SchemaError si el schema en sí está mal formado.
                Draft7Validator.check_schema(schema)


if __name__ == "__main__":
    unittest.main()
