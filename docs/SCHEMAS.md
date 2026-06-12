# Guía de esquemas registrados

Cómo crear, editar y validar los JSON Schemas que el servicio usa para extraer datos.
Reglas asociadas: [`../RULES.md`](../RULES.md) §R6 (normalización) y §R8 (contratos).

---

## 1. Conceptos

- Un **schema** es un archivo `schemas/<nombre>.json` en formato **JSON Schema Draft 7**.
- El `schema_id` es el **nombre del archivo sin `.json`** (ej. `factura_simple.json`
  → `schema_id = "factura_simple"`).
- Los esquemas se cargan **una vez** en el lifespan de arranque. Cambios → **reiniciar**
  el servicio.
- Un schema inválido se **excluye** del registro y se loggea como ERROR; no tumba el
  servicio.

El schema cumple **doble función**:
1. Es el esquema de la **herramienta** que recibe el LLM (define qué campos extraer).
2. Es el validador final del output (paso 7 del pipeline).
3. Su **metadata** dirige la normalización (paso 6).

---

## 2. Crear un schema nuevo (paso a paso)

1. Crea `schemas/mi_documento.json`.
2. Declara un objeto JSON Schema Draft 7 válido. Recomendado:
   - `"$schema": "http://json-schema.org/draft-07/schema#"`
   - `"type": "object"`
   - `"additionalProperties": false` para que el LLM no agregue campos.
   - `required` con los campos imprescindibles.
3. Para cada propiedad, declara `type`. **Hazlo anulable** (`["tipo","null"]`) salvo que
   el campo deba existir siempre — el system prompt instruye al LLM a poner `null`
   cuando no encuentra un valor, así que casi todos los campos deben permitir `null`.
4. Añade metadata de normalización donde aplique (§3).
5. Reinicia el servicio y verifica en `/v1/health` que aparece en `schemas_available`.

---

## 3. Metadata de normalización (schema-driven)

El normalizer lee esta metadata por campo. Si no hay metadata, aplica normalización
conservadora por `type`.

### Fechas

```json
{ "issue_date": { "type": ["string", "null"], "format": "date" } }
```
- `format: "date"` → normaliza a `YYYY-MM-DD`. Detecta `dd/mm/yyyy`, `dd-mm-yyyy`,
  `yyyy/mm/dd`, `dd.mm.yyyy`, `mm/dd/yyyy`.
- `format: "date-time"` → ISO 8601 con timezone (soporta sufijo `Z`).

### Números y moneda

```json
{
  "amount_total": { "type": ["number", "null"], "x-locale": "es-CO", "x-format": "currency" }
}
```
- `x-locale` define los separadores de **miles** y **decimal**. Soportados hoy:
  `es-CO`, `es-ES` (`.` miles, `,` decimal); `es-MX`, `en-US` (`,` miles, `.` decimal).
- `x-format: "currency"` es una pista semántica; el tratamiento numérico ya remueve
  símbolos de moneda.
- **Sin `x-locale`**: se autodetecta el separador decimal por la posición del último
  `,`/`.`. Para montos críticos, **declara siempre `x-locale`** y evita la ambigüedad.

### Arrays y objetos anidados

```json
{
  "line_items": {
    "type": "array",
    "items": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "description": { "type": ["string", "null"] },
        "quantity":    { "type": ["number", "null"] },
        "unit_price":  { "type": ["number", "null"], "x-locale": "es-CO" }
      }
    }
  }
}
```
El normalizer recurre en arrays y en objetos, aplicando la metadata de cada sub-campo.

> ¿Agregaste un locale nuevo? Debe registrarse en `_LOCALE_SEPARATORS` dentro de
> `services/normalizer.py`. La metadata sola no basta si el locale no está mapeado.

---

## 4. Ejemplo completo

Ver [`../schemas/factura_simple.json`](../schemas/factura_simple.json) como referencia
canónica: incluye string, number con `x-locale`, `format: date`, y un array de objetos.

---

## 5. Verificar un schema antes de usarlo

```bash
# 1. JSON bien formado
python -c "import json; json.load(open('schemas/mi_documento.json'))"

# 2. Es un JSON Schema Draft 7 válido
python -c "from jsonschema import Draft7Validator; import json; \
Draft7Validator.check_schema(json.load(open('schemas/mi_documento.json'))); print('OK')"

# 3. Arranca el servicio y confirma que se cargó
curl -s localhost:8080/v1/health | python -c "import sys,json; print(json.load(sys.stdin)['schemas_available'])"
```

Luego prueba el pipeline end-to-end con un PDF representativo y revisa los logs de
normalización (WARNINGs de campos no normalizables indican metadata faltante o
formatos no contemplados).

---

## 6. Schemas con extractor rule-based (`x-extractor`)

Para documentos con geometría conocida y fija (ej: formularios DIAN) se puede
registrar un **extractor rule-based** que omite completamente el paso LLM.

### Declaración en el schema

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "rut",
  "x-extractor": "dian_rut",
  ...
}
```

`x-extractor` es el nombre del módulo Python en `services/extractors/<nombre>.py`.
El módulo debe exponer una función `extract(content: bytes) -> dict[str, Any]`.

### Metadata de normalización para campos numéricos con dígitos espaciados

```json
{ "nit": { "type": ["string", "null"], "x-strip": "non-digits" } }
```

`x-strip: "non-digits"` → elimina todo lo que no sea dígito del valor recibido.
Útil para NITs, teléfonos y códigos CIIU que en algunos PDFs llegan con espacios.

### Diferencias vs pipeline LLM

| Aspecto | LLM | Rule-based |
|---|---|---|
| Costo por request | tokens consumidos | $0 |
| Latencia | 2-10 s | < 100 ms |
| Flexibilidad | alta | baja (formulario fijo) |
| `extraction_metadata.llm_provider` | nombre del provider | `"none"` |
| `extraction_metadata.llm_time_ms` | tiempo real | `0` |
| `extraction_metadata.cost_usd_estimated` | real | `0.0` |

---

## 7. Schemas registrados

| schema_id | Extractor | Descripción |
|---|---|---|
| `factura_simple` | LLM | Factura electrónica DIAN (proveedor/emisor) |
| `rut` | `dian_rut` (rule-based) | Registro Único Tributario Colombia |

---

## 8. Buenas prácticas

- **Nombres de campo en inglés** y descriptivos; el LLM los usa como guía semántica.
- Usa `description` por campo: mejora la extracción del LLM.
- Mantén los schemas **pequeños y enfocados**; un schema por tipo de documento.
- `additionalProperties: false` salvo razón explícita.
- No metas lógica de negocio en el schema; solo estructura, tipos y metadata `x-`.
- **Versiona** si cambias un schema de forma incompatible para un consumidor: crea
  `mi_documento_v2.json` en lugar de romper `mi_documento.json` (ver RULES §R8).
