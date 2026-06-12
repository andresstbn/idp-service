# Arquitectura y flujos clave

Documento de referencia de los flujos internos del servicio. Para el resumen de
alto nivel ver [`../AGENTS.md`](../AGENTS.md); para las reglas, [`../RULES.md`](../RULES.md).

---

## 1. Visión general de componentes

```
                         ┌──────────────────────────────────────────┐
   HTTP (multipart)      │                 main.py                  │
   ──────────────────►   │  middleware trace_id · lifespan · router │
                         └───────────────┬──────────────────────────┘
                                         │
                         ┌───────────────▼───────────────┐
                         │  api/v1/endpoints/extract.py   │
                         │  parse options · maneja errores│
                         └───────────────┬────────────────┘
                                         │  DocumentProcessor.process(...)
                         ┌───────────────▼─────────────────────────────┐
                         │      services/document_processor.py          │
                         │  orquesta los 8 pasos · mide tiempos · logs  │
                         └──┬─────────┬──────────┬───────────┬──────────┘
                            │         │          │           │
              pdf_extractor │ schema_ │ llm/     │ normalizer│
                            │ registry│ provider │           │
                            ▼         ▼          ▼           ▼
                         pymupdf/  registro   Anthropic/  reglas
                         pdfplumber en memoria  OpenAI    schema-driven
```

Recursos compartidos viven en `app.state` (se crean una vez en el lifespan):
- `app.state.registry` — `SchemaRegistry` cargado.
- `app.state.processor` — `DocumentProcessor` (registry + provider LLM).
- `app.state.started_at` — para el `uptime_seconds` de `/v1/health`.

---

## 2. Ciclo de vida (lifespan)

En `main.py`, al arrancar el servicio (antes de aceptar tráfico):

1. `configure_logging()` — instala el `JSONFormatter` en el root logger.
2. `SchemaRegistry(SCHEMAS_DIR).load()` — carga y valida los `.json`. Los inválidos se
   excluyen y se loggean como ERROR; **no** tumban el arranque.
3. `create_llm_provider()` — instancia el provider configurado. **Falla aquí** si el
   provider no existe o le falta API key (no en runtime).
4. Se guardan los recursos en `app.state` y se loggea `servicio IDP iniciado`.

**Invariante**: un despliegue mal configurado (provider inválido, sin key) **no llega
a aceptar tráfico**.

---

## 3. Middleware de trace_id

Cada request HTTP pasa por `trace_id_middleware`:

1. Toma `X-Trace-Id` del header entrante o genera un `uuid4`.
2. Lo guarda en `request.state.trace_id` y lo enlaza al `ContextVar` (`bind_request_context`).
3. Tras procesar, limpia el contexto (`reset_request_context`) y devuelve el `trace_id`
   en el header `X-Trace-Id` de la respuesta.

Gracias al `ContextVar`, cualquier `log(...)` en cualquier punto del pipeline incluye
el `trace_id` sin tener que pasarlo como parámetro.

---

## 4. El pipeline de 8 pasos (detalle)

Implementado en `DocumentProcessor.process(...)`. Cada paso mide su duración con
`time.perf_counter()` y reporta milisegundos en `extraction_metadata` y en logs.

### Paso 1 — VALIDATE (`pdf_extractor.validate_pdf_bytes`)
- Verifica **magic bytes** (`%PDF-`), no la extensión.
- Verifica tamaño ≤ `PDF_MAX_SIZE_MB`.
- Falla → `InvalidPDFError` (400).

### Paso 2 — LOAD SCHEMA (`registry.get`)
- Recupera el schema por `schema_id` del registro en memoria.
- No existe → `SchemaNotFoundError` (400) con **la lista de schemas disponibles** en
  el mensaje.

### Paso 3 — EXTRACT TEXT (`pdf_extractor.extract_text`)
- **Detección de texto nativo** primero: si ninguna página supera
  `PDF_MIN_CHARS_PER_PAGE / 10` → `NoNativeTextError` (400) que menciona OCR/Fase 2.
- Resuelve qué páginas procesar (`page_range` o truncado por `PDF_MAX_PAGES_FULL`).
- Intenta **pymupdf**; calcula `chars_per_page`.
- Si `chars_per_page < PDF_MIN_CHARS_PER_PAGE` → **fallback a pdfplumber**, registrando
  `fallback_reason`.
- Devuelve `ExtractionResult(text, pages_processed, extractor_used, fallback_reason, truncated)`.

### Paso 4 — NORMALIZE PRE-LLM (`_clean_text`)
- Elimina caracteres de control (conserva `\n` y `\t`), colapsa espacios horizontales.
- Conserva los separadores de página `--- Página N ---` que inserta el extractor.

### Paso 5 — LLM MAPPING (`provider.extract`)
- Envía texto + JSON Schema + hints al provider.
- El provider usa tool-use/function-calling nativo con el schema como esquema de la
  herramienta y **reintenta** (`LLM_MAX_RETRIES`) realimentando errores de validación.
- Devuelve `(data: dict, UsageMetrics)`.
- Falla tras reintentos → `LLMExtractionError` (500).

### Paso 6 — NORMALIZE OUTPUT (`normalizer.normalize`)
- Normaliza valores **schema-driven** (ver §5). Tolerante a fallos por campo.

### Paso 7 — VALIDATE OUTPUT (`Draft7Validator.iter_errors`)
- Valida el dict normalizado contra el schema registrado.
- Falla → loggea raw output del LLM como WARNING + `OutputValidationError` (500).

### Paso 8 — ASSEMBLE RESPONSE
- Construye `field_metadata` con `confidence=null` por cada propiedad del schema.
- Construye `extraction_metadata` con tiempos, extractor, provider, tokens y costo.
- Emite el **log de finalización exitosa** con los campos numéricos de métricas.
- Devuelve `ExtractResponse` (200).

---

## 5. Flujo de normalización (schema-driven)

`normalizer.normalize(data, schema)` recorre `data` y, por cada campo, busca su
sub-schema en `schema["properties"]`. La decisión de tratamiento:

```
valor None                      → None (se respeta)
format == "date"                → ISO 8601 (YYYY-MM-DD); prueba dd/mm/yyyy, dd-mm-yyyy, …
format == "date-time"           → ISO 8601 con timezone (soporta sufijo Z)
type (no-null) == "number"      → quita moneda/miles; coma decimal → punto; x-locale manda
type (no-null) == "integer"     → number, luego int()
type (no-null) == "array"       → normaliza cada item por items-schema
type (no-null) == "object"      → normaliza cada propiedad por su sub-schema (recursivo)
type (no-null) == "string"      → strip de extremos
```

Detalles críticos (cubiertos por reglas en RULES §R6):
- **Tipos en union** (`["number","null"]`): `_primary_type` extrae el tipo no-null.
- **`x-locale`**: usa el mapa `_LOCALE_SEPARATORS` (es-CO, es-ES, es-MX, en-US). Sin
  locale, `_autodetect_number` infiere el separador decimal por la posición del último
  `,`/`.`.
- **Tolerancia**: un valor no normalizable se conserva como original + WARNING; no
  rompe el pipeline (lo detecta el paso 7).

---

## 6. Manejo de errores end-to-end

```
IDPError (status_code, public_message)  ──► endpoint lo traduce a HTTP + trace_id
  ├─ InvalidPDFError        400
  ├─ SchemaNotFoundError    400
  ├─ NoNativeTextError      400
  ├─ LLMExtractionError     500
  └─ OutputValidationError  500

RequestValidationError (Pydantic) ──► handler global → 422 + trace_id
Exception no prevista             ──► barrera final en el endpoint → 500 genérico + trace_id
```

Los `500` se loggean como ERROR con `exc_info` cuando aplica; **nunca** se devuelve la
traza al cliente.

---

## 7. Puntos de extensión (sin romper la interfaz pública)

| Extensión | Dónde entra | Qué NO se toca |
|---|---|---|
| OCR (Fase 2) | nuevo paso dentro de `pdf_extractor.py`, activado por `NoNativeTextError` | `document_processor.py`, contrato `/v1` |
| Nuevo provider LLM | `services/llm/<x>_provider.py` + `factory.py` | `document_processor.py` |
| Nueva regla de normalización | `normalizer.py` guiado por metadata `x-` del schema | nombres de campo hardcodeados (prohibido) |
| Nueva métrica | log de finalización en `document_processor.py` + `ExtractionMetadata` | estructura existente |
| Confidence por campo (Fase 2) | poblar `field_metadata[campo].confidence` | el contrato ya existe |
