"""Extractor rule-based para Facturas Electrónicas DIAN y Documentos Equivalentes POS.

Soporta:
  - Factura Electrónica de Venta (CUFE)
  - Documento Equivalente POS (CUDE)

Usa PyMuPDF para extraer texto con coordenadas y aplica búsqueda por etiquetas
para obtener los campos. Los ítems del detalle se extraen usando rangos de X
detectados desde el encabezado de la tabla.

Los catálogos de IDs (país, departamento, ciudad, medios de pago) NO se resuelven
aquí — se retorna el texto plano y el mapeo a IDs se hace en datak-app.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import fitz  # pymupdf

from core.logging import get_logger, log

logger = get_logger("idp.extractor.dian_factura")

# ─── Tipos ────────────────────────────────────────────────────────────────────

Word = tuple[float, float, float, float, str, int, int, int]
# (x0, y0, x1, y1, text, block_no, line_no, word_no)

Line = list[Word]

# ─── Marcadores de sección ────────────────────────────────────────────────────

_SUPPLIER_STARTS = {
    "datos del emisor / vendedor",
    "datos del vendedor",
}
_BUYER_STARTS = {
    "datos del adquiriente / comprador",
    "datos del adquiriente",
    "datos del adquirente / comprador",
    "datos del adquirente",
}
_ITEMS_START = "detalles de productos"
_ITEMS_ENDS = {"notas finales", "referencias", "datos totales"}

# ─── Etiquetas conocidas (para truncar valores al encontrar la siguiente) ─────

_ALL_LABELS_LOWER = {
    "número de factura:", "número de documento:", "número documento:",
    "fecha de emisión:", "fecha y hora de expedición:",
    "fecha de vencimiento:", "forma de pago:", "medio de pago:",
    "razón social:", "nombre comercial:", "nit del emisor:",
    "tipo de contribuyente:", "régimen fiscal:", "responsabilidad tributaria:",
    "actividad económica:", "país:", "departamento:", "municipio / ciudad:",
    "dirección:", "teléfono / móvil:", "correo:", "tipo de documento:",
    "nombre o razón social:", "orden de pedido:",
    "fecha de orden de pedido:", "tipo de operación:",
    "código único de factura - cufe :",
    "código único de factura - cufe:",
    "código único de documentos equivalente – cude:",
    "código único de documentos equivalente - cude:",
}


# ─── Construcción de líneas ───────────────────────────────────────────────────

def _build_lines(words: list[Word], y_tol: float = 5.0) -> list[Line]:
    """Agrupa palabras en líneas horizontales (orden top-to-bottom).

    En PyMuPDF, Y=0 está en la parte superior de la página y aumenta hacia abajo,
    a diferencia de pdfjs donde Y aumenta hacia arriba.
    """
    if not words:
        return []
    sorted_words = sorted(words, key=lambda w: (w[1], w[0]))  # Y asc, X asc
    lines: list[Line] = []
    current_y = sorted_words[0][1]
    current_line: Line = []

    for word in sorted_words:
        if abs(word[1] - current_y) <= y_tol:
            current_line.append(word)
        else:
            lines.append(sorted(current_line, key=lambda w: w[0]))
            current_line = [word]
            current_y = word[1]

    if current_line:
        lines.append(sorted(current_line, key=lambda w: w[0]))

    return lines


def _line_text(line: Line) -> str:
    return " ".join(w[4] for w in line)


# ─── Extracción de secciones ──────────────────────────────────────────────────

def _extract_section(
    lines: list[Line],
    starts: set[str],
    ends: set[str],
) -> list[Line]:
    """Retorna las líneas entre el marcador de inicio y el de fin (excluyendo ambos)."""
    in_section = False
    result: list[Line] = []
    for line in lines:
        lt = _line_text(line).strip().lower()
        if not in_section:
            if lt in starts:
                in_section = True
        else:
            if any(lt.startswith(e) for e in ends):
                break
            result.append(line)
    return result


# ─── Extracción de valor tras etiqueta ───────────────────────────────────────

def _value_after_label(line_str: str, label: str) -> str | None:
    """Extrae el valor que sigue a una etiqueta en un string de línea.

    Maneja:
    - "Etiqueta: valor siguiente  OtraEtiqueta: otro valor"
    - "Etiqueta:valor" (sin espacio tras los dos puntos)
    """
    idx = line_str.lower().find(label.lower())
    if idx == -1:
        return None

    value_start = idx + len(label)
    value_str = line_str[value_start:].lstrip()

    if not value_str:
        return None

    # Truncar en la siguiente etiqueta conocida
    value_lower = value_str.lower()
    earliest = len(value_str)
    for other in _ALL_LABELS_LOWER:
        if other == label.lower():
            continue
        pos = value_lower.find(other)
        if 0 <= pos < earliest:
            earliest = pos

    result = value_str[:earliest].strip()
    return result or None


def _find_field(lines: list[Line], *label_variants: str) -> str | None:
    """Busca cualquier variante de etiqueta en las líneas y retorna su valor."""
    for line in lines:
        lt = _line_text(line)
        for label in label_variants:
            value = _value_after_label(lt, label)
            if value:
                return value
    return None


# ─── Encabezado del documento ─────────────────────────────────────────────────

def _extract_header(all_lines: list[Line]) -> dict[str, Any]:
    """Extrae los campos del bloque 'Datos del Documento' (antes de Emisor)."""
    header_lines: list[Line] = []
    for line in all_lines:
        lt = _line_text(line).strip().lower()
        if lt in _SUPPLIER_STARTS or lt in _BUYER_STARTS:
            break
        header_lines.append(line)

    # CUFE/CUDE — string hexadecimal largo (≥ 32 chars) en la misma línea o la siguiente
    uuid: str | None = None
    for i, line in enumerate(header_lines):
        lt = _line_text(line).lower()
        if "cufe" not in lt and "cude" not in lt:
            continue
        # Buscar en esta línea y la siguiente
        candidate_lines = [line]
        if i + 1 < len(header_lines):
            candidate_lines.append(header_lines[i + 1])
        for cand_line in candidate_lines:
            for word in cand_line:
                wt = word[4]
                if len(wt) >= 32 and re.fullmatch(r"[0-9a-fA-F]+", wt):
                    uuid = wt
                    break
            if uuid:
                break
        if uuid:
            break

    supplier_code = _find_field(
        header_lines,
        "Número de Factura:",
        "Número de documento:",
        "Número de Documento:",
    )

    date_raw = _find_field(
        header_lines,
        "Fecha de Emisión:",
        "Fecha y hora de expedición:",
        "Fecha y Hora de Expedición:",
    )
    # "2025-02-28 18:41:08-05:00" → "2025-02-28"
    if date_raw:
        date_raw = date_raw.split(" ")[0].split("T")[0].strip()

    payment_date_raw = _find_field(
        header_lines,
        "Fecha de Vencimiento:",
        "Fecha de vencimiento:",
    )
    if payment_date_raw:
        payment_date_raw = payment_date_raw.split(" ")[0].split("T")[0].strip()

    payment_type_label = _find_field(
        header_lines,
        "Forma de pago:",
        "Forma de Pago:",
    )
    payment_method_label = _find_field(
        header_lines,
        "Medio de Pago:",
        "Medio de pago:",
    )

    return {
        "supplier_code": supplier_code,
        "uuid": uuid,
        "date": date_raw or None,
        "payment_date": payment_date_raw or None,
        "payment_type_label": payment_type_label,
        "payment_method_label": payment_method_label,
    }


# ─── Datos del emisor ─────────────────────────────────────────────────────────

def _extract_supplier(supplier_lines: list[Line]) -> dict[str, Any]:
    """Extrae los datos del Emisor / Vendedor."""

    def _find(*labels: str) -> str | None:
        return _find_field(supplier_lines, *labels)

    first_name = _find("Razón Social:", "Razón social:")
    id_number = _find("Nit del Emisor:", "Número de documento:", "Nit del emisor:")
    address = _find("Dirección:")
    email_raw = _find("Correo:")
    email = email_raw.lower().strip() if email_raw else None

    phone_raw = _find("Teléfono / Móvil:", "Teléfono / Movil:", "Teléfono/Móvil:")
    phone: str | None = None
    if phone_raw:
        digits = re.sub(r"\D", "", phone_raw)
        # Quitar prefijo de país si el número es más largo de 10 dígitos
        phone = digits[-10:] if len(digits) > 10 else digits
        phone = phone or None

    country_name = _find("País:")
    department_name = _find("Departamento:")
    city_name = _find("Municipio / Ciudad:")

    activity_raw = _find("Actividad Económica:", "Actividad Economica:")
    # Solo conservar si es un código CIIU numérico
    activity_code = activity_raw if activity_raw and re.fullmatch(r"\d{4}", activity_raw.strip()) else None

    person_type_raw = _find("Tipo de Contribuyente:", "Tipo de contribuyente:")
    person_type: str | None = None
    if person_type_raw:
        pt_lower = person_type_raw.lower()
        if "jurídica" in pt_lower or "juridica" in pt_lower:
            person_type = "JUR"
        elif "natural" in pt_lower:
            person_type = "NAT"

    tax_regime = _find("Régimen Fiscal:", "Régimen fiscal:", "Régimen Fiscal:")

    tax_liability_raw = _find("Responsabilidad tributaria:", "Responsabilidad Tributaria:")
    # Limitar a 30 chars para no capturar basura de línea contigua
    tax_liability_label = tax_liability_raw[:30].strip() if tax_liability_raw else None

    return {
        "id_number": id_number,
        "first_name": first_name,
        "address": address,
        "email": email,
        "phone": phone,
        "country_name": country_name,
        "department_name": department_name,
        "city_name": city_name,
        "activity_code": activity_code,
        "person_type": person_type,
        "tax_regime": tax_regime,
        "tax_liability_label": tax_liability_label,
    }


# ─── NIT del adquiriente ──────────────────────────────────────────────────────

def _extract_buyer_nit(buyer_lines: list[Line]) -> str | None:
    """Extrae el NIT del adquiriente / comprador."""
    raw = _find_field(
        buyer_lines,
        "Número Documento:",
        "Número de documento:",
        "NIT del adquiriente:",
        "NIT del Adquiriente:",
    )
    if raw:
        digits = re.sub(r"\D", "", raw)
        return digits or None
    return None


# ─── Ítems de la factura ──────────────────────────────────────────────────────

def _normalize_colombian_number(text: str) -> str:
    """Convierte un número en formato colombiano a string decimal.

    "1.500,00" → "1500.00"
    "1,00"     → "1.00"
    "19.00"    → "19.00"  (ya está en formato decimal)
    """
    if not text:
        return ""
    # Eliminar caracteres no numéricos excepto separadores
    cleaned = re.sub(r"[^\d,.]", "", text)
    if not cleaned:
        return ""

    last_comma = cleaned.rfind(",")
    last_dot = cleaned.rfind(".")

    if last_comma > last_dot:
        # La coma es el separador decimal → formato colombiano
        result = cleaned.replace(".", "").replace(",", ".")
    else:
        # El punto es el separador decimal → ya en formato decimal
        result = cleaned.replace(",", "")

    return result


def _detect_column_x(items_lines: list[Line]) -> dict[str, float]:
    """Detecta las posiciones X de las columnas desde el encabezado de la tabla.

    Devuelve un dict con claves: 'um', 'cantidad', 'precio', 'iva', 'inc',
    'iva_pct', 'inc_pct'. Los valores son la coordenada X0 de cada columna.
    Usa valores por defecto si no puede detectar el encabezado.
    """
    col: dict[str, float] = {}
    pct_xs: list[float] = []
    iva_x: float | None = None

    # Escanear las primeras líneas buscando palabras de encabezado
    for line in items_lines[:8]:
        lt = _line_text(line)
        if not any(k in lt for k in ("U/M", "Cantidad", "Descripci", "Nro")):
            continue
        for word in line:
            t, x = word[4], word[0]
            if t == "U/M" and "um" not in col:
                col["um"] = x
            elif t == "Cantidad" and "cantidad" not in col:
                col["cantidad"] = x
            elif t == "Precio" and "precio" not in col:
                col["precio"] = x
            elif t == "IVA" and "iva" not in col:
                col["iva"] = x
                iva_x = x
            elif t == "INC" and "inc" not in col:
                col["inc"] = x
            elif t == "%" :
                pct_xs.append(x)

    # Asignar posiciones de los porcentajes (primero IVA %, luego INC %)
    if pct_xs and iva_x is not None:
        pcts_after_iva = sorted(x for x in pct_xs if x >= iva_x - 5)
        if pcts_after_iva:
            col["iva_pct"] = pcts_after_iva[0]
        if len(pcts_after_iva) >= 2:
            col["inc_pct"] = pcts_after_iva[1]

    # Valores por defecto basados en el layout típico de factura DIAN (A4, márgenes ~35pt)
    return {
        "um": col.get("um", 242.0),
        "cantidad": col.get("cantidad", 285.0),
        "precio": col.get("precio", 335.0),
        "iva": col.get("iva", 460.0),
        "inc": col.get("inc", 510.0),
        "iva_pct": col.get("iva_pct", 490.0),
        "inc_pct": col.get("inc_pct", 530.0),
    }


def _find_data_start(items_lines: list[Line]) -> int:
    """Retorna el índice de la primera línea de datos (row que empieza con dígito)."""
    for i, line in enumerate(items_lines):
        if not line:
            continue
        first_word = line[0][4]
        first_x = line[0][0]
        # Las filas de datos comienzan con un número pequeño (1,2,3...) en posición izquierda
        if re.fullmatch(r"\d{1,2}", first_word) and first_x < 70:
            return i
    return len(items_lines)  # Sin datos


def _parse_item_row(
    line: Line,
    col: dict[str, float],
) -> dict[str, Any] | None:
    """Parsea una fila de ítem usando rangos de coordenada X."""
    if not line:
        return None

    first_word = line[0][4]
    if not re.fullmatch(r"\d{1,2}", first_word):
        return None

    row_x = line[0][0]
    um_x = col["um"]
    cantidad_x = col["cantidad"]
    precio_x = col["precio"]
    iva_pct_x = col["iva_pct"]
    inc_pct_x = col["inc_pct"]

    # Descripción: entre la columna de número y la columna U/M
    # Incluye el código de producto (primera columna después del número)
    desc_words = [w[4] for w in line if row_x + 10 <= w[0] < um_x]
    description = " ".join(desc_words).strip()

    # Código de unidad (U/M)
    um_words = [w[4] for w in line if um_x - 8 <= w[0] < cantidad_x]
    unit_code = um_words[0] if um_words else ""

    # Cantidad
    qty_words = [w[4] for w in line if cantidad_x - 8 <= w[0] < precio_x]
    qty_raw = next((w for w in qty_words if re.search(r"\d", w)), "")
    quantity = _normalize_colombian_number(qty_raw)

    # Precio unitario: entre precio_x e iva_pct_x - 80 (hay varias columnas intermedias)
    # Saltamos el símbolo "$"
    price_x_end = iva_pct_x - 80
    price_words = [
        w[4] for w in line
        if precio_x - 5 <= w[0] < price_x_end and w[4] != "$" and re.search(r"\d", w[4])
    ]
    price_raw = price_words[0] if price_words else ""
    price = _normalize_colombian_number(price_raw)

    # IVA %: cerca de iva_pct_x
    iva_words = [
        w[4] for w in line
        if iva_pct_x - 15 <= w[0] <= iva_pct_x + 30 and re.search(r"\d", w[4])
    ]
    iva_pct = _normalize_colombian_number(iva_words[0]) if iva_words else ""

    # INC %: cerca de inc_pct_x
    inc_words = [
        w[4] for w in line
        if inc_pct_x - 15 <= w[0] <= inc_pct_x + 30 and re.search(r"\d", w[4])
    ]
    inc_pct = _normalize_colombian_number(inc_words[0]) if inc_words else ""

    if not description and not unit_code and not price:
        return None

    return {
        "description": description,
        "price": price,
        "quantity": quantity,
        "unit_code": unit_code,
        "iva_percentage": iva_pct,
        "inc_percentage": inc_pct,
    }


def _extract_items_from_lines(items_lines: list[Line]) -> list[dict[str, Any]]:
    """Extrae los ítems de las líneas filtradas de la sección Detalles de Productos."""
    if not items_lines:
        return []

    col = _detect_column_x(items_lines)
    data_start = _find_data_start(items_lines)

    items: list[dict[str, Any]] = []
    for line in items_lines[data_start:]:
        item = _parse_item_row(line, col)
        if item:
            items.append(item)

    return items


# ─── Punto de entrada ─────────────────────────────────────────────────────────

def extract(content: bytes) -> dict[str, Any]:
    """Extrae los campos de una factura electrónica DIAN desde el PDF.

    Retorna un dict cuya estructura coincide con el schema factura_dian.json.
    Los valores nulos se retornan como None.
    """
    with fitz.open(stream=content, filetype="pdf") as doc:
        if doc.page_count == 0:
            log(logger, logging.WARNING, "PDF sin páginas")
            return {}

        header: dict[str, Any] = {}
        supplier: dict[str, Any] = {}
        imported_bu: str | None = None
        all_items: list[dict[str, Any]] = []

        for page_num in range(doc.page_count):
            page = doc[page_num]
            words: list[Word] = page.get_text("words")  # type: ignore[assignment]
            lines = _build_lines(words)

            if page_num == 0:
                header = _extract_header(lines)

                supplier_lines = _extract_section(
                    lines,
                    _SUPPLIER_STARTS,
                    _BUYER_STARTS | {_ITEMS_START},
                )
                supplier = _extract_supplier(supplier_lines)

                buyer_lines = _extract_section(
                    lines,
                    _BUYER_STARTS,
                    {_ITEMS_START},
                )
                imported_bu = _extract_buyer_nit(buyer_lines)

            items_lines = _extract_section(lines, {_ITEMS_START}, _ITEMS_ENDS)
            page_items = _extract_items_from_lines(items_lines)
            all_items.extend(page_items)

    result: dict[str, Any] = {
        **header,
        "third_party": supplier,
        "imported_bu": imported_bu,
        "items": all_items,
    }

    log(
        logger,
        logging.INFO,
        "extracción factura DIAN completada",
        supplier_code=result.get("supplier_code"),
        items_count=len(all_items),
        supplier_fields=sum(1 for v in supplier.values() if v is not None),
    )

    return result
