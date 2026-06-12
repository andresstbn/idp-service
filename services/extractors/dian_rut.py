"""Extractor rule-based para el formulario DIAN RUT (Registro Único Tributario).

Usa PyMuPDF para obtener texto con posiciones desde la página 1 del PDF y
extrae los campos aprovechando que el formulario DIAN tiene una geometría
consistente: los Y de las líneas de valores son fijos en todos los RUT emitidos.

No usa LLM. Los campos de catálogo (país, departamento, ciudad) se retornan
como texto plano; el mapeo a IDs es responsabilidad de datak-app.

Campos extraídos y su correspondencia en el formulario DIAN:
  nit               → Campo 5  (sin DV)
  dv                → Campo 6
  direccion_seccional → Campo 12
  person_type       → Campo 24  ("JUR" | "NAT")
  id_type_label     → Campo 25  (texto)
  razon_social      → Campo 35
  nombre_comercial  → Campo 36
  primer_apellido   → Campo 31
  segundo_apellido  → Campo 32
  primer_nombre     → Campo 33
  otros_nombres     → Campo 34
  pais_nombre       → Campo 38
  departamento_nombre → Campo 29/39
  ciudad_nombre     → Campo 30/40
  direccion         → Campo 41
  correo            → Campo 42
  telefono          → Campo 44
  codigo_ciiu       → Campo 46
"""

from __future__ import annotations

import logging
import re
from typing import Any

import fitz  # pymupdf

from core.logging import get_logger, log

logger = get_logger("idp.extractor.dian_rut")

# ---------------------------------------------------------------------------
# Geometría del formulario DIAN RUT (en puntos PDF, Y=0 en la parte inferior).
# Validado contra MEJIA, AMERICAN RUBBER y MEGASERVICIOS.
# ---------------------------------------------------------------------------

# Y aproximado de las líneas de VALORES (no etiquetas).
# Tolerancia: ±Y_TOL puntos.
_Y_TOL = 6.0

_Y_NIT_VALUE = 179.9          # Dígitos del NIT + DV + nombre seccional
_Y_PERSON_TYPE_VALUE = 215.0  # "Persona jurídica" / "Persona natural"
_Y_RAZON_SOCIAL_VALUE = 285.9 # Razón social (campo 35) o apellidos (persona natural)
_Y_NOMBRE_COM_VALUE = 309.9   # Nombre comercial + sigla (puede estar vacío)
_Y_LOCATION_VALUE = 347.0     # País, departamento, ciudad en una sola línea
_Y_ADDRESS_VALUE = 370.9      # Dirección principal
_Y_CORREO_LINE = 385.1        # "42. Correo electrónico <email>" (etiqueta + valor)
_Y_PHONE_LINE = 397.1         # "44. Teléfono 1 <dígitos> 45. Teléfono 2 <dígitos>"
_Y_CIIU_VALUE = 453.9         # Dígitos CIIU + fechas (primer grupo = código 46)

# X de las etiquetas en la línea de ubicación (y≈337.1), consistentes en todos los RUT.
_X_DEPTO_LABEL = 200.0    # "39. Departamento"
_X_CIUDAD_LABEL = 392.0   # "40. Ciudad/Municipio"

# X de las etiquetas en la línea de teléfono (y≈397.1).
_X_TEL1_LABEL = 201.5     # "44. Teléfono 1"
_X_TEL2_LABEL = 398.5     # "45. Teléfono 2"
# Los dígitos reales del teléfono 1 empiezan mucho más a la derecha que el label
# ("44. Teléfono 1" termina en x≈242; los dígitos del teléfono comienzan en x≈330).
_X_TEL1_DIGITS_START = 300.0

# ---------------------------------------------------------------------------
# Tipos internos
# ---------------------------------------------------------------------------

Word = tuple[float, float, float, float, str, int, int, int]
# (x0, y0, x1, y1, text, block_no, line_no, word_no)

Line = list[Word]


# ---------------------------------------------------------------------------
# Helpers de geometría
# ---------------------------------------------------------------------------

def _build_lines(words: list[Word], y_tol: float = _Y_TOL) -> list[Line]:
    """Agrupa palabras en líneas horizontales por proximidad de Y.

    Ordena el resultado de arriba a abajo (Y decreciente en coordenadas PDF).
    Dentro de cada línea las palabras van de izquierda a derecha.
    """
    if not words:
        return []
    sorted_words = sorted(words, key=lambda w: (-w[1], w[0]))
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


def _find_line(lines: list[Line], target_y: float, tol: float = _Y_TOL) -> Line | None:
    """Devuelve la línea cuyo Y medio está más cerca de target_y, dentro de tol."""
    best: Line | None = None
    best_dist = float("inf")
    for line in lines:
        y = line[0][1]
        dist = abs(y - target_y)
        if dist < tol and dist < best_dist:
            best_dist = dist
            best = line
    return best


def _line_text(line: Line) -> str:
    """Texto completo de una línea."""
    return " ".join(w[4] for w in line)


def _words_in_x_range(line: Line, x_min: float, x_max: float) -> list[str]:
    """Palabras de una línea cuyo X inicial cae en [x_min, x_max)."""
    return [w[4] for w in line if x_min <= w[0] < x_max]


def _words_from_x(line: Line, x_min: float) -> list[str]:
    """Palabras de una línea desde x_min en adelante."""
    return [w[4] for w in line if w[0] >= x_min]


def _words_up_to_x(line: Line, x_max: float) -> list[str]:
    """Palabras de una línea hasta x_max (exclusive)."""
    return [w[4] for w in line if w[0] < x_max]


def _strip_digits(tokens: list[str]) -> str:
    """Concatena solo los dígitos de los tokens dados."""
    return "".join(re.findall(r"\d", " ".join(tokens)))


def _strip_non_digits(tokens: list[str]) -> str:
    """Palabras NO puramente numéricas, unidas con espacio."""
    return " ".join(t for t in tokens if not re.fullmatch(r"[\d\s]+", t)).strip()


# ---------------------------------------------------------------------------
# Extractores por campo
# ---------------------------------------------------------------------------

def _extract_nit_dv_seccional(lines: list[Line]) -> tuple[str | None, str | None, str | None]:
    """Extrae NIT (sin DV), DV y dirección seccional desde la línea del NIT.

    La línea y≈179.9 tiene el formato:
        <dígito> <dígito> ... <nombre seccional> <más dígitos>

    Los dígitos ANTES del nombre seccional forman NIT + DV (último dígito = DV).
    El nombre seccional es la primera secuencia no numérica de la línea.
    """
    line = _find_line(lines, _Y_NIT_VALUE)
    if not line:
        return None, None, None

    pre_seccional: list[str] = []
    seccional_tokens: list[str] = []
    found_seccional = False

    for word in line:
        token = word[4]
        if not found_seccional:
            if re.fullmatch(r"\d+", token):
                pre_seccional.append(token)
            else:
                found_seccional = True
                seccional_tokens.append(token)
        else:
            # Continuar acumulando el nombre seccional hasta encontrar dígitos solos
            if re.fullmatch(r"\d+", token):
                break  # el resto son hoja/página, no nos interesa
            seccional_tokens.append(token)

    all_digits = "".join(pre_seccional)
    if len(all_digits) < 2:
        return all_digits or None, None, " ".join(seccional_tokens) or None

    nit = all_digits[:-1]
    dv = all_digits[-1]
    seccional = " ".join(seccional_tokens).strip() or None
    return nit or None, dv or None, seccional


def _extract_person_type(lines: list[Line]) -> tuple[str | None, str | None]:
    """Devuelve (person_type, id_type_label) desde la línea y≈215.

    Formato: 'Persona jurídica <código>' o 'Persona natural <código>'.
    El id_type_label aplica solo para personas naturales (campo 25).
    Para personas jurídicas el tipo de documento es implícitamente NIT.
    """
    line = _find_line(lines, _Y_PERSON_TYPE_VALUE)
    if not line:
        return None, None

    text = _line_text(line)
    person_type: str | None = None

    if re.search(r"jurídica|juridica", text, re.IGNORECASE):
        person_type = "JUR"
    elif re.search(r"natural", text, re.IGNORECASE):
        person_type = "NAT"

    # id_type_label: cualquier token que no sea "Persona", "jurídica", "natural"
    # ni sea un código numérico de un solo dígito.
    # Para JUR siempre es NIT; para NAT puede aparecer "Cédula de Ciudadanía", etc.
    id_type_label: str | None = None
    if person_type == "JUR":
        id_type_label = "NIT"
    else:
        non_type_tokens = [
            w[4] for w in line
            if not re.fullmatch(r"[\d]+", w[4])
            and w[4].lower() not in ("persona", "jurídica", "juridica", "natural")
        ]
        if non_type_tokens:
            id_type_label = " ".join(non_type_tokens).strip() or None

    return person_type, id_type_label


def _extract_razon_social(lines: list[Line]) -> str | None:
    """Razón social (campo 35) o primer bloque de apellidos (persona natural)."""
    line = _find_line(lines, _Y_RAZON_SOCIAL_VALUE)
    if not line:
        return None
    text = _line_text(line).strip()
    return text or None


def _extract_nombre_comercial(lines: list[Line]) -> str | None:
    """Nombre comercial (campo 36). Puede estar ausente (línea vacía o inexistente)."""
    line = _find_line(lines, _Y_NOMBRE_COM_VALUE)
    if not line:
        return None
    # La línea puede contener: "<nombre comercial> <sigla>"
    # Tomamos todo el texto; si la sigla es necesaria en otro campo se separa con X.
    # Por ahora retornamos la primera parte (hasta el centro aprox de la línea).
    # Usamos el X del label "37. Sigla" como separador si está disponible.
    # Como heurística: tomar el texto hasta que se repita un token similar al inicio.
    text = _line_text(line).strip()
    if not text:
        return None
    # Si hay una coma o patrón que indique fin del nombre comercial, truncamos.
    # Retornamos el texto completo de la zona izquierda (x < _X_CIUDAD_LABEL aprox).
    left_words = _words_up_to_x(line, _X_DEPTO_LABEL)
    return " ".join(left_words).strip() or text or None


def _extract_location(lines: list[Line]) -> tuple[str | None, str | None, str | None]:
    """Extrae (país, departamento, ciudad) desde la línea y≈347.

    La línea tiene formato:
        <país> <código_país> <departamento...> <código_depto> <ciudad...> <código_ciudad>

    Se usan los X de las etiquetas "39." y "40." como fronteras.
    Los tokens puramente numéricos son códigos y se descartan.
    """
    line = _find_line(lines, _Y_LOCATION_VALUE)
    if not line:
        return None, None, None

    # País: palabras con x < _X_DEPTO_LABEL y no puramente numéricas
    pais_tokens = [
        w[4] for w in line
        if w[0] < _X_DEPTO_LABEL and not re.fullmatch(r"[\d]+", w[4])
    ]

    # Departamento: _X_DEPTO_LABEL ≤ x < _X_CIUDAD_LABEL, no numérico
    depto_tokens = [
        w[4] for w in line
        if _X_DEPTO_LABEL <= w[0] < _X_CIUDAD_LABEL and not re.fullmatch(r"[\d]+", w[4])
    ]

    # Ciudad: x ≥ _X_CIUDAD_LABEL, no numérico
    ciudad_tokens = [
        w[4] for w in line
        if w[0] >= _X_CIUDAD_LABEL and not re.fullmatch(r"[\d]+", w[4])
    ]

    return (
        " ".join(pais_tokens).strip() or None,
        " ".join(depto_tokens).strip() or None,
        " ".join(ciudad_tokens).strip() or None,
    )


def _extract_address(lines: list[Line]) -> str | None:
    """Dirección principal (campo 41), línea y≈370.9."""
    line = _find_line(lines, _Y_ADDRESS_VALUE)
    if not line:
        return None
    return _line_text(line).strip() or None


def _extract_correo(lines: list[Line]) -> str | None:
    """Correo electrónico (campo 42) desde la línea y≈385.1 que mezcla etiqueta y valor."""
    line = _find_line(lines, _Y_CORREO_LINE)
    if not line:
        return None
    # Buscar el token que tiene formato de email
    for word in line:
        if re.fullmatch(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", word[4]):
            return word[4].lower()
    return None


def _extract_telefono(lines: list[Line]) -> str | None:
    """Teléfono 1 (campo 44) desde la línea y≈397.1.

    La línea mezcla etiquetas y dígitos:
        43. Código postal [<dígitos cp>] 44. Teléfono 1 <dígitos> 45. Teléfono 2 <dígitos>

    Se toman los dígitos entre x_tel1 y x_tel2.
    """
    line = _find_line(lines, _Y_PHONE_LINE)
    if not line:
        return None

    # Usamos _X_TEL1_DIGITS_START (no _X_TEL1_LABEL) para excluir el "1" del
    # label "44. Teléfono 1" que aparece en x≈237, mucho antes de los dígitos reales.
    digits_tel1 = [
        w[4] for w in line
        if _X_TEL1_DIGITS_START <= w[0] < _X_TEL2_LABEL and re.fullmatch(r"\d+", w[4])
    ]
    result = "".join(digits_tel1)
    return result or None


def _extract_ciiu(lines: list[Line]) -> str | None:
    """Código CIIU actividad principal (campo 46), línea y≈453.9.

    La línea tiene todos los dígitos de campos 46, 47, 48, 49... concatenados.
    Los primeros 4 dígitos (posición x más a la izquierda) corresponden al campo 46.
    """
    line = _find_line(lines, _Y_CIIU_VALUE)
    if not line:
        return None

    all_digits = "".join(
        w[4] for w in line if re.fullmatch(r"\d+", w[4])
    )
    # El código CIIU es 4 dígitos
    if len(all_digits) >= 4:
        return all_digits[:4]
    return all_digits or None


def _extract_natural_person_names(
    lines: list[Line],
) -> tuple[str | None, str | None, str | None, str | None]:
    """Extrae (primer_apellido, segundo_apellido, primer_nombre, otros_nombres)
    para personas naturales desde la línea y≈285.9.

    En personas naturales, la línea y≈285.9 contiene los apellidos y nombres
    en el mismo orden que los campos 31-34. Esta función es tentativa ya que
    no hay PDFs de personas naturales en la muestra validada.
    """
    line = _find_line(lines, _Y_RAZON_SOCIAL_VALUE)
    if not line:
        return None, None, None, None
    # Sin muestra de persona natural, retornamos el texto completo como primer_apellido
    # y dejamos el resto como None. En producción esto se refinará con datos reales.
    text = _line_text(line).strip()
    return text or None, None, None, None


# ---------------------------------------------------------------------------
# Función principal
# ---------------------------------------------------------------------------

def extract(content: bytes) -> dict[str, Any]:
    """Extrae los campos del RUT desde el PDF y devuelve un dict con los valores raw.

    Solo procesa la página 1 (Hoja 1) del formulario DIAN, que contiene todos los
    campos relevantes para datak-app.

    Retorna None para campos no encontrados o vacíos.
    """
    with fitz.open(stream=content, filetype="pdf") as doc:
        if doc.page_count == 0:
            log(logger, logging.WARNING, "PDF sin páginas")
            return {}
        page = doc[0]
        words: list[Word] = page.get_text("words")  # type: ignore[assignment]

    if not words:
        log(logger, logging.WARNING, "página 1 sin palabras extraíbles")
        return {}

    lines = _build_lines(words)

    # --- Campos core ---
    nit, dv, seccional = _extract_nit_dv_seccional(lines)
    person_type, id_type_label = _extract_person_type(lines)
    pais, depto, ciudad = _extract_location(lines)

    result: dict[str, Any] = {
        "nit": nit,
        "dv": dv,
        "direccion_seccional": seccional,
        "person_type": person_type,
        "id_type_label": id_type_label,
        "pais_nombre": pais,
        "departamento_nombre": depto,
        "ciudad_nombre": ciudad,
        "direccion": _extract_address(lines),
        "correo": _extract_correo(lines),
        "telefono": _extract_telefono(lines),
        "codigo_ciiu": _extract_ciiu(lines),
        # Campos de persona jurídica
        "razon_social": None,
        "nombre_comercial": None,
        # Campos de persona natural
        "primer_apellido": None,
        "segundo_apellido": None,
        "primer_nombre": None,
        "otros_nombres": None,
    }

    if person_type == "JUR" or person_type is None:
        result["razon_social"] = _extract_razon_social(lines)
        result["nombre_comercial"] = _extract_nombre_comercial(lines)
    else:
        primer_ap, segundo_ap, primer_nom, otros_nom = _extract_natural_person_names(lines)
        result["primer_apellido"] = primer_ap
        result["segundo_apellido"] = segundo_ap
        result["primer_nombre"] = primer_nom
        result["otros_nombres"] = otros_nom

    log(
        logger,
        logging.INFO,
        "extracción RUT completada",
        nit=nit,
        person_type=person_type,
        fields_extracted=sum(1 for v in result.values() if v is not None),
    )

    return result
