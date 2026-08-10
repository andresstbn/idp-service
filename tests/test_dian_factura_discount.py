"""SPEC-109: el descuento por ítem se extrae de la columna 'Descuento' del PDF.

Coordenadas tomadas del layout de la representación gráfica DIAN, donde la
columna de descuento va entre Precio unitario e IVA.
"""

from __future__ import annotations

from services.extractors.dian_factura import _detect_column_x, _parse_item_row


def _w(x0, text):
    # (x0, y0, x1, y1, text, block, line, word)
    return (x0, 0.0, x0 + 10, 10.0, text, 0, 0, 0)


def _header():
    return [
        _w(200.0, "Cantidad"), _w(250.0, "Precio"), _w(330.0, "Descuento"),
        _w(390.0, "Recargo"), _w(425.8, "IVA"), _w(458.1, "%"),
    ]


def test_descuento_por_item_se_extrae_como_monto():
    col = _detect_column_x([_header()])
    assert col["descuento"] == 330.0

    row = [
        _w(39.3, "1"), _w(58.9, "LAXAT"), _w(87.7, "XL"),
        _w(203.0, "1,00"), _w(255.0, "142.848,74"),
        _w(332.0, "$"), _w(340.0, "100.840,34"),   # Descuento
        _w(453.7, "19.00"),                         # IVA %
        _w(529.3, "$"), _w(537.9, "42.008,40"),     # Total ítem
    ]
    item = _parse_item_row(row, col)
    assert item["discount_amount"] == "100840.34"
    assert item["price"] == "142848.74", f"el precio no debe leer el descuento: {item['price']!r}"
    assert item["quantity"] == "1.00"
    assert item["iva_percentage"] == "19.00"


def test_descuento_vacio_cuando_la_celda_esta_vacia():
    col = _detect_column_x([_header()])
    row = [
        _w(39.3, "1"), _w(58.9, "TORNILLOS"),
        _w(203.0, "1,00"), _w(255.0, "1.500,00"),
        _w(400.0, "0,00"),                          # Recargo, NO descuento
        _w(453.7, "19.00"),
    ]
    item = _parse_item_row(row, col)
    assert item["discount_amount"] == "", f"no debe leer el recargo: {item['discount_amount']!r}"


def test_descuento_vacio_cuando_la_factura_no_tiene_columna():
    col = _detect_column_x([[_w(253.1, "Cantidad"), _w(368.7, "IVA"), _w(424.6, "%")]])
    assert col["descuento"] is None  # no se inventa la columna

    row = [_w(57.4, "1"), _w(99.5, "PT4035"), _w(262.4, "1.00"), _w(307.3, "10185.19")]
    item = _parse_item_row(row, col)
    assert item["discount_amount"] == ""


if __name__ == "__main__":
    test_descuento_por_item_se_extrae_como_monto()
    test_descuento_vacio_cuando_la_celda_esta_vacia()
    test_descuento_vacio_cuando_la_factura_no_tiene_columna()
    print("OK")
