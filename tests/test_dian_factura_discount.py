"""SPEC-109: el descuento por ítem se extrae de la columna 'Descuento' del PDF.

Coordenadas tomadas del layout de la representación gráfica DIAN, donde la
columna de descuento va entre Precio unitario e IVA. Los casos de borde
(encabezado en mayúsculas, monto ancho alineado a la derecha, descuento a la
izquierda del precio, fecha en la ventana) salieron de la revisión del PR.
"""

from __future__ import annotations

import unittest

from services.extractors.dian_factura import _detect_column_x, _parse_item_row


def _w(x0, text):
    # (x0, y0, x1, y1, text, block, line, word)
    return (x0, 0.0, x0 + 10, 10.0, text, 0, 0, 0)


def _header(discount_label="Descuento", recargo_label="Recargo"):
    return [
        _w(200.0, "Cantidad"), _w(250.0, "Precio"), _w(330.0, discount_label),
        _w(390.0, recargo_label), _w(425.8, "IVA"), _w(458.1, "%"),
    ]


class TestItemDiscount(unittest.TestCase):
    def test_descuento_por_item_se_extrae_como_monto(self):
        col = _detect_column_x([_header()])
        self.assertEqual(col["descuento"], 330.0)

        row = [
            _w(39.3, "1"), _w(58.9, "LAXAT"), _w(87.7, "XL"),
            _w(203.0, "1,00"), _w(255.0, "142.848,74"),
            _w(332.0, "$"), _w(340.0, "100.840,34"),   # Descuento
            _w(453.7, "19.00"),                         # IVA %
            _w(529.3, "$"), _w(537.9, "42.008,40"),     # Total ítem
        ]
        item = _parse_item_row(row, col)
        self.assertEqual(item["discount_amount"], "100840.34")
        self.assertEqual(item["price"], "142848.74", "el precio no debe leer el descuento")
        self.assertEqual(item["quantity"], "1.00")
        self.assertEqual(item["iva_percentage"], "19.00")

    def test_monto_ancho_alineado_a_la_derecha_se_asigna_al_descuento(self):
        """El x0 del monto queda a la izquierda del x0 de su encabezado."""
        col = _detect_column_x([_header()])
        row = [
            _w(39.3, "1"), _w(58.9, "LAXAT"),
            _w(203.0, "1,00"), _w(255.0, "142.848,74"),
            _w(318.0, "100.840,34"),                    # Descuento, corrido a la izquierda
            _w(453.7, "19.00"),
        ]
        item = _parse_item_row(row, col)
        self.assertEqual(item["discount_amount"], "100840.34")
        self.assertEqual(item["price"], "142848.74")

    def test_celda_de_precio_vacia_no_reporta_el_descuento_como_precio(self):
        col = _detect_column_x([_header()])
        row = [
            _w(39.3, "1"), _w(58.9, "LAXAT"),
            _w(203.0, "1,00"),                          # celda de precio vacía
            _w(340.0, "100.840,34"),
            _w(453.7, "19.00"),
        ]
        item = _parse_item_row(row, col)
        self.assertEqual(item["price"], "", "el descuento no debe reportarse como precio")
        # Sin precio el consumidor no puede expresarlo como porcentaje, así que
        # tampoco se emite el descuento.
        self.assertEqual(item["discount_amount"], "")

    def test_encabezado_en_mayusculas_tambien_se_detecta(self):
        header = [
            _w(200.0, "CANTIDAD"), _w(250.0, "PRECIO"), _w(330.0, "DESCUENTO"),
            _w(390.0, "RECARGO"), _w(425.8, "IVA"), _w(458.1, "%"),
        ]
        col = _detect_column_x([header])
        # Todas las columnas, no solo la del descuento: con la comparación
        # case-sensitive el resto caía a los defaults.
        self.assertEqual(col["cantidad"], 200.0)
        self.assertEqual(col["precio"], 250.0)
        self.assertEqual(col["descuento"], 330.0)

        row = [
            _w(39.3, "1"), _w(58.9, "TORNILLOS"),
            _w(203.0, "1,00"), _w(255.0, "1.500,00"),
            _w(400.0, "45,00"),                         # Recargo, NO descuento
            _w(453.7, "19.00"),
        ]
        item = _parse_item_row(row, col)
        self.assertEqual(item["discount_amount"], "", "no debe leer el recargo")

    def test_monto_de_miles_sin_centavos_no_se_divide_por_mil(self):
        col = _detect_column_x([_header()])
        row = [
            _w(39.3, "1"), _w(58.9, "TORNILLOS"),
            _w(203.0, "1,00"), _w(255.0, "142.848"),
            _w(340.0, "100.840"),                       # Descuento sin centavos
            _w(453.7, "19.00"),
        ]
        item = _parse_item_row(row, col)
        self.assertEqual(item["discount_amount"], "100840")

    def test_sin_columna_detectada_a_la_derecha_no_se_adivina_el_ancho(self):
        """Adivinar el ancho es como el total del ítem terminaba en el campo."""
        header = [_w(200.0, "Cantidad"), _w(250.0, "Precio"), _w(460.0, "Descuento")]
        col = _detect_column_x([header])
        self.assertEqual(col["descuento"], 460.0)
        self.assertIsNone(col["descuento_from"])

        row = [
            _w(39.3, "1"), _w(58.9, "PT4035"), _w(203.0, "1,00"),
            _w(255.0, "10185.19"), _w(490.0, "10185.1900"),  # Total ítem
        ]
        item = _parse_item_row(row, col)
        self.assertEqual(item["discount_amount"], "")

    def test_sin_columna_de_precio_detectada_no_se_emite_descuento(self):
        """Su ventana usaría el default 335 y podría solaparse con la del descuento."""
        header = [
            _w(200.0, "Cantidad"), _w(250.0, "Vr"), _w(415.0, "Descuento"),
            _w(450.0, "IVA"), _w(480.0, "%"),
        ]
        col = _detect_column_x([header])
        self.assertEqual(col["descuento"], 415.0)
        self.assertIsNone(col["descuento_from"])

        row = [
            _w(39.3, "1"), _w(58.9, "TORNILLOS"), _w(203.0, "1,00"),
            _w(340.0, "142.848,74"),
        ]
        item = _parse_item_row(row, col)
        self.assertEqual(item["price"], "142848.74")
        self.assertEqual(item["discount_amount"], "")

    def test_desc_no_es_encabezado_de_descuento(self):
        """'Desc.' abrevia Descripción: como descuento leería el código del producto."""
        header = [
            _w(35.0, "Nro"), _w(88.0, "Desc."), _w(242.0, "U/M"),
            _w(285.0, "Cantidad"), _w(335.0, "Precio"), _w(460.0, "IVA"), _w(490.0, "%"),
        ]
        col = _detect_column_x([header])
        self.assertIsNone(col["descuento"])

        row = [
            _w(39.3, "1"), _w(100.0, "7702001"), _w(120.0, "TORNILLOS"),
            _w(244.0, "94"), _w(290.0, "1,00"), _w(340.0, "1.500,00"),
        ]
        item = _parse_item_row(row, col)
        self.assertEqual(item["discount_amount"], "")

    def test_una_fila_de_datos_no_define_las_columnas(self):
        """'nro' como subcadena dejaba que una descripción entrara al encabezado."""
        lines = [
            [_w(35.0, "Nro"), _w(88.0, "Descripción"), _w(242.0, "U/M"),
             _w(285.0, "Cantidad"), _w(335.0, "Precio"), _w(460.0, "IVA"), _w(490.0, "%")],
            [_w(39.3, "1"), _w(88.0, "CABLE"), _w(120.0, "ENROLLADO"),
             _w(150.0, "dto"), _w(244.0, "94"), _w(290.0, "1,00"), _w(340.0, "9.900")],
        ]
        col = _detect_column_x(lines)
        self.assertIsNone(col["descuento"], "una fila de datos no define columnas")

    def test_descuento_vacio_cuando_la_celda_esta_vacia(self):
        col = _detect_column_x([_header()])
        row = [
            _w(39.3, "1"), _w(58.9, "TORNILLOS"),
            _w(203.0, "1,00"), _w(255.0, "1.500,00"),
            _w(400.0, "0,00"),                          # Recargo, NO descuento
            _w(453.7, "19.00"),
        ]
        item = _parse_item_row(row, col)
        self.assertEqual(item["discount_amount"], "")

    def test_descuento_vacio_cuando_la_factura_no_tiene_columna(self):
        col = _detect_column_x([[
            _w(253.1, "Cantidad"), _w(300.0, "Precio"), _w(368.7, "IVA"), _w(424.6, "%"),
        ]])
        self.assertIsNone(col["descuento"])  # no se inventa la columna
        self.assertIsNone(col["descuento_from"])

        row = [_w(57.4, "1"), _w(99.5, "PT4035"), _w(262.4, "1.00"), _w(307.3, "10185.19")]
        item = _parse_item_row(row, col)
        self.assertEqual(item["discount_amount"], "")
        self.assertEqual(item["price"], "10185.19", "sin columna de descuento el precio no cambia")

    def test_descuento_a_la_izquierda_del_precio_no_borra_el_precio(self):
        header = [
            _w(200.0, "Cantidad"), _w(300.0, "Dto."), _w(360.0, "Precio"),
            _w(440.0, "IVA"), _w(470.0, "%"),
        ]
        col = _detect_column_x([header])
        row = [
            _w(39.3, "1"), _w(58.9, "TORNILLOS"), _w(203.0, "1,00"),
            _w(305.0, "150,00"), _w(365.0, "1.500,00"),
        ]
        item = _parse_item_row(row, col)
        self.assertEqual(item["price"], "1500.00")
        self.assertEqual(item["discount_amount"], "150.00")

    def test_una_fecha_en_la_ventana_no_se_reporta_como_descuento(self):
        header = [_w(253.1, "Cantidad"), _w(300.0, "Precio"), _w(460.0, "Descuento")]
        col = _detect_column_x([header])
        row = [
            _w(57.4, "1"), _w(99.5, "PT4035"), _w(262.4, "1.00"),
            _w(307.3, "10185.19"), _w(490.0, "2025-02-28"),
        ]
        item = _parse_item_row(row, col)
        self.assertEqual(item["discount_amount"], "", "una fecha no es un monto")


if __name__ == "__main__":
    unittest.main()
