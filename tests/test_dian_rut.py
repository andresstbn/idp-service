"""Tests de la geometría del extractor RUT (sin PDFs reales: solo word-lists).

Reproducen las coordenadas observadas en RUTs reales para fijar el comportamiento
de los campos que distinguen persona natural de jurídica (la regresión que
arreglamos: nombres por columnas, id_type_label y nombre comercial en naturales).
No usan PDFs para no commitear datos personales. Un test de integración real
puede vivir aparte apuntando a fixtures locales no versionados.

Corre con: `python -m unittest discover -s tests`  (o pytest).
"""

from __future__ import annotations

import unittest

from services.extractors import dian_rut


def _w(x0: float, y0: float, text: str):
    """Construye una palabra con la firma de PyMuPDF: (x0,y0,x1,y1,text,b,l,w)."""
    return (x0, y0, x0 + 10 * len(text), y0 + 8, text, 0, 0, 0)


class TestNaturalPersonNames(unittest.TestCase):
    def _lines(self, words):
        return dian_rut._build_lines(words)

    def test_splits_apellidos_y_nombres_por_columna(self) -> None:
        # Fila y≈261.9: BOTELLO@27 | AREVALO@155 | ORLANDO@282 (otros nombres vacío).
        words = [_w(27, 261.9, "BOTELLO"), _w(155, 261.9, "AREVALO"), _w(282, 261.9, "ORLANDO")]
        result = dian_rut._extract_natural_person_names(self._lines(words))
        self.assertEqual(result, ("BOTELLO", "AREVALO", "ORLANDO", None))

    def test_apellido_compuesto_multiples_palabras(self) -> None:
        # Primer apellido compuesto: todas las palabras de la columna se unen.
        words = [
            _w(27, 261.9, "DE"), _w(45, 261.9, "LA"), _w(70, 261.9, "CRUZ"),
            _w(155, 261.9, "GOMEZ"),
            _w(282, 261.9, "MARIA"), _w(320, 261.9, "JOSE"),
        ]
        primer_ap, segundo_ap, primer_nom, otros = dian_rut._extract_natural_person_names(
            self._lines(words)
        )
        self.assertEqual(primer_ap, "DE LA CRUZ")
        self.assertEqual(segundo_ap, "GOMEZ")
        self.assertEqual(primer_nom, "MARIA JOSE")
        self.assertIsNone(otros)

    def test_sin_fila_de_nombres_devuelve_none(self) -> None:
        words = [_w(27, 999.0, "IRRELEVANTE")]
        result = dian_rut._extract_natural_person_names(self._lines(words))
        self.assertEqual(result, (None, None, None, None))


class TestPersonType(unittest.TestCase):
    def _lines(self, words):
        return dian_rut._build_lines(words)

    def test_natural_id_type_label_sin_sufijo_del_campo_24(self) -> None:
        # "Persona natural o sucesión ilíquida" (campo 24) + "Cédula de Ciudadanía"
        # (campo 25, a partir de x≈186). El id_type_label NO debe traer el sufijo.
        words = [
            _w(27, 215.0, "Persona"), _w(55, 215.0, "natural"), _w(78, 215.0, "o"),
            _w(84, 215.0, "sucesión"), _w(113, 215.0, "ilíquida"),
            _w(173, 213.9, "2"),
            _w(186, 215.0, "Cédula"), _w(210, 215.0, "de"), _w(219, 215.0, "Ciudadanía"),
        ]
        person_type, id_type_label = dian_rut._extract_person_type(self._lines(words))
        self.assertEqual(person_type, "NAT")
        self.assertEqual(id_type_label, "Cédula de Ciudadanía")

    def test_juridica_id_type_es_nit(self) -> None:
        words = [_w(27, 215.0, "Persona"), _w(55, 215.0, "jurídica"), _w(173, 213.9, "1")]
        person_type, id_type_label = dian_rut._extract_person_type(self._lines(words))
        self.assertEqual(person_type, "JUR")
        self.assertEqual(id_type_label, "NIT")


if __name__ == "__main__":
    unittest.main()
