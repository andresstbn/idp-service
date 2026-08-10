"""Regresión: el total del ítem no debe filtrarse al campo %INC.

Coordenadas tomadas de facturas DIAN reales (datak2.pdf, datak.pdf) donde el
ítem no declara INC. Antes, la ventana de %INC capturaba la columna de Total
($1.500,00) y la guardaba como INC=1.500%.
"""

from __future__ import annotations

import unittest

from services.extractors.dian_factura import _detect_column_x, _parse_item_row


def _w(x0, text):
    # (x0, y0, x1, y1, text, block, line, word)
    return (x0, 0.0, x0 + 10, 10.0, text, 0, 0, 0)


class TestIncTotalLeak(unittest.TestCase):
    def test_inc_no_captura_total_cuando_item_no_tiene_inc(self):
        # Encabezado real datak2.pdf: "... IVA % INC %"
        header = [
            _w(425.8, "IVA"), _w(458.1, "%"), _w(486.9, "INC"), _w(515.4, "%"),
            _w(34.4, "Cantidad"),  # palabra clave para que _detect_column_x procese la línea
        ]
        col = _detect_column_x([header])

        # Fila real: IVA% = 19.00, sin INC, Total = $1.500,00
        row = [
            _w(39.3, "1"), _w(58.9, "T0001"), _w(87.7, "TORNILLOS"),
            _w(203.0, "1,00"), _w(264.1, "1.500,00"),
            _w(453.7, "19.00"),            # IVA %
            _w(529.3, "$"), _w(537.9, "1.500,00"),  # Total ítem (NO es INC)
        ]
        item = _parse_item_row(row, col)
        self.assertEqual(item["iva_percentage"], "19.00")
        self.assertEqual(item["inc_percentage"], "", "INC debe ser vacío, no el total")

    def test_inc_vacio_cuando_factura_no_tiene_columna_inc(self):
        # Encabezado real datak.pdf: "... IVA % Venta por compra" (sin columna INC)
        header = [
            _w(368.7, "IVA"), _w(424.6, "%"), _w(461.8, "Venta"),
            _w(253.1, "Cantidad"),
        ]
        col = _detect_column_x([header])
        self.assertIsNone(col["inc_pct"])  # no se inventa columna INC

        row = [
            _w(57.4, "1"), _w(99.5, "PT4035"), _w(262.4, "1.00"),
            _w(307.3, "10185.19"), _w(462.4, "10185.1900"), _w(515.7, "2025-02-28"),
        ]
        item = _parse_item_row(row, col)
        self.assertEqual(item["inc_percentage"], "")


if __name__ == "__main__":
    unittest.main()
