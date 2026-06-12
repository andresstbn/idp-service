# Definición fundacional — IDP Service, Fase 1

> Este es el documento que **dio origen al servicio**: la especificación de la Fase 1
> (PDF nativo con esquemas registrados). Se conserva textualmente como **fuente de
> verdad** del *porqué* de cada decisión de diseño. Si un cambio contradice este
> documento, debe justificarse explícitamente (ver [`../RULES.md`](../RULES.md)).

---

## Rol

Desarrollador backend senior especializado en Python, FastAPI y pipelines de
procesamiento de documentos. Implementación de la Fase 1 de un microservicio de
Intelligent Document Processing (IDP) para desplegarse en Google Cloud Run.

---

## Contexto arquitectónico

Este servicio es **infraestructura compartida** que consumirán múltiples proyectos
cliente independientes. Las decisiones de diseño priorizan:
- Contratos de API estables y versionados.
- Observabilidad desde el primer día (structured logs como fuente de métricas en Cloud Run).
- Aislamiento de errores: un PDF mal formado no debe tumbar el servicio.
- Preparación para Fase 2 (OCR + visión multimodal) sin romper la interfaz pública.

El servicio **NO tiene base de datos** en Fase 1. Los esquemas registrados se cargan
desde archivos JSON en disco al iniciar el servicio (en `schemas/`).

---

## Stack obligatorio

- **Python 3.12+**
- **FastAPI** con **Uvicorn** (modo async)
- **pymupdf (fitz)** como extractor primario (velocidad en PDFs corporativos estándar)
- **pdfplumber** como extractor secundario/fallback (layout espacial y tablas complejas)
- **Pydantic v2** para requests, responses y configuración interna
- **instructor** para structured outputs del LLM, con soporte multi-provider
- **jsonschema** para validar los esquemas registrados al cargar y el output del LLM
- **Google Cloud Run** como target de despliegue (stateless, containerizado)

El proveedor LLM es configurable vía variables de entorno. El modelo por defecto es
**Claude claude-sonnet-4-20250514** vía Anthropic, pero la capa de LLM debe estar
completamente abstraída del pipeline.

---

## Estructura de directorios esperada

```
idp-service/
├── main.py                        # Entrypoint FastAPI
├── api/v1/
│   ├── router.py                  # Agrupación de rutas v1
│   └── endpoints/extract.py       # POST /v1/extract
├── core/
│   ├── config.py                  # Settings con pydantic-settings
│   ├── exceptions.py              # Excepciones custom del dominio
│   └── logging.py                 # Structured logging JSON para Cloud Logging
├── services/
│   ├── schema_registry.py         # Carga y validación de esquemas registrados
│   ├── pdf_extractor.py           # Pipeline pymupdf → pdfplumber con criterio objetivo
│   ├── normalizer.py              # Normalización de valores post-LLM
│   ├── llm/
│   │   ├── base.py                # Interfaz abstracta del proveedor LLM
│   │   ├── anthropic_provider.py  # Implementación Anthropic vía instructor
│   │   ├── openai_provider.py     # Implementación OpenAI vía instructor
│   │   └── factory.py             # Factory que instancia el provider según config
│   └── document_processor.py      # Orquestador principal del pipeline
├── models/
│   ├── requests.py                # Pydantic models para request/response HTTP
│   └── extraction.py              # Modelos internos del pipeline
├── schemas/factura_simple.json    # JSON Schemas registrados
├── Dockerfile · .env.example · requirements.txt
```

---

## Endpoint principal — `POST /v1/extract`

**Content-Type:** `multipart/form-data`

| Campo | Tipo | Requerido | Descripción |
|---|---|---|---|
| `file` | `UploadFile` | ✅ | Archivo PDF |
| `schema_id` | `str` | ✅ | ID del esquema registrado (nombre del archivo sin `.json`) |
| `project_id` | `str` | ✅ | Identificador del proyecto cliente (observabilidad y futuro rate limiting) |
| `options` | `str` (JSON) | ❌ | `{"page_range": [1, 3], "hints": "..."}` |

**Response `200`** (estructura):

```json
{
  "schema_id": "factura_simple",
  "project_id": "proyecto-a",
  "extraction_metadata": {
    "pages_processed": 2,
    "extractor_used": "pymupdf",
    "fallback_reason": null,
    "processing_time_ms": 340,
    "extraction_time_ms": 45,
    "llm_time_ms": 280,
    "normalization_time_ms": 15,
    "llm_provider": "anthropic",
    "llm_model": "claude-sonnet-4-20250514",
    "token_usage": { "input": 1240, "output": 180 },
    "cost_usd_estimated": 0.0021,
    "confidence_available": false
  },
  "data": { /* campos según el JSON Schema, tipos ya normalizados */ },
  "field_metadata": {
    "invoice_number": { "confidence": null },
    "amount_total":   { "confidence": null }
  }
}
```

**Errores esperados**:
- `400` — schema_id no existe en el registro.
- `400` — el archivo no es un PDF válido (falla en magic bytes).
- `400` — el PDF no tiene capa de texto nativa.
- `422` — validación Pydantic del request fallida.
- `500` — error interno; incluir `trace_id`; nunca exponer stack trace ni raw LLM output.

---

## Pipeline de procesamiento (document_processor.py)

Pipeline secuencial con pasos explícitos. Cada paso registra su duración en el
structured log del request. El `trace_id` se propaga a todos los pasos.

```
1. VALIDATE          magic bytes del PDF · tamaño máximo · fallo rápido 400
2. LOAD SCHEMA       del registro en memoria · 400 con lista de disponibles si falta
3. EXTRACT TEXT      pymupdf → evaluar calidad (PDF_MIN_CHARS_PER_PAGE) → fallback pdfplumber
                     si ninguno extrae suficiente: 400 "sin texto nativo" (OCR en Fase 2)
4. NORMALIZE PRE-LLM limpiar texto · marcar "--- Página N ---"
5. LLM MAPPING       texto + JSON Schema al provider · null para ausentes · capturar tokens y costo
                     si falla tras max_retries: 500 con trace_id
6. NORMALIZE OUTPUT  según tipos del schema (moneda, fechas, números) · metadata x-locale/x-format
7. VALIDATE OUTPUT   jsonschema.validate · si falla: WARNING con raw output + 500 con trace_id
8. ASSEMBLE RESPONSE data + field_metadata (confidence=null) + extraction_metadata
```

---

## PDF Extractor — criterio de fallback

```
chars_per_page = total_chars_extraidos / total_paginas
if chars_per_page < PDF_MIN_CHARS_PER_PAGE:
    fallback_reason = f"pymupdf: {chars_per_page:.1f} chars/pág < umbral {PDF_MIN_CHARS_PER_PAGE}"
    intentar pdfplumber
```

Cada decisión de extractor se registra con `extractor_used` y `fallback_reason`.

**Detección de PDF sin texto nativo**: si todas las páginas retornan texto vacío o menor
a `PDF_MIN_CHARS_PER_PAGE / 10`, clasificar como "sin texto nativo" → 400 mencionando OCR.

**Documentos largos**: sin RAG ni chunking semántico. Hasta `PDF_MAX_PAGES_FULL` (default 30)
se envía completo; por encima, usar `page_range` o truncar las primeras N páginas con WARNING.

---

## LLM Provider

**Interfaz abstracta (base.py)**:

```python
async def extract(self, text: str, schema: dict, trace_id: str) -> tuple[dict, UsageMetrics]:
    ...
```

`UsageMetrics`: dataclass con `input_tokens`, `output_tokens`, `cost_usd_estimated`.
Cada provider calcula su propio costo.

**Implementaciones**: usar `instructor` con el cliente correspondiente. El system prompt:
- Instruir como extractor de información estructurada, no asistente.
- Campos no encontrados → `null`.
- Prohibir inventar/inferir/completar información no presente.
- Tipos estrictos: fechas ISO 8601, números sin formato de miles.

La validación del output usa `jsonschema`, **no** modelos Pydantic dinámicos. Si falla,
se reintenta con el error como feedback (`max_retries=2`).

**Factory**: instancia el provider según `LLM_PROVIDER`. Si no está implementado, falla
en **startup** (no runtime).

---

## Normalizer

Opera sobre el dict del LLM antes de la validación final. Reglas **schema-driven**: lee
metadata del campo en el JSON Schema. Sin metadata, normalización conservadora por tipo.

| Tipo JSON Schema | Transformación |
|---|---|
| `number` | Remover moneda/miles, coma decimal → punto |
| `integer` | Igual que number, luego `int()` |
| `format: date` | dd/mm/yyyy, dd-mm-yyyy → ISO 8601 |
| `format: date-time` | ISO 8601 con timezone |
| `string` | Strip de extremos |
| `array` | Normalizar cada elemento según su tipo |

Metadata `x-`: `x-locale` (separadores del locale), `x-format` (p. ej. `currency`).

**Errores**: un valor no normalizable se conserva como string original + WARNING. No
fallar el pipeline; lo detecta la validación posterior.

---

## Schema Registry

- Cargar todos los `.json` de `schemas/` en el lifespan.
- Validar cada uno contra el meta-schema Draft 7.
- Schema inválido → ERROR con nombre del archivo, excluir del registro, no crashear.
- `get(schema_id) -> dict | None` · `list() -> list[str]`.
- Loggear al startup cuántos cargados/fallaron.

---

## Configuración (pydantic-settings)

```python
# LLM
LLM_PROVIDER: str = "anthropic"           # anthropic | openai
LLM_MODEL: str = "claude-sonnet-4-20250514"
ANTHROPIC_API_KEY: str = ""
OPENAI_API_KEY: str = ""
LLM_MAX_RETRIES: int = 2
# Extracción
SCHEMAS_DIR: str = "schemas"
PDF_MIN_CHARS_PER_PAGE: int = 50
PDF_MAX_SIZE_MB: int = 20
PDF_MAX_PAGES_FULL: int = 30
# Servicio
LOG_LEVEL: str = "INFO"
ENVIRONMENT: str = "development"           # development | production
SERVICE_VERSION: str = "1.0.0"
```

---

## Logging estructurado

JSON (no texto plano). Cada log incluye: `timestamp`, `level`, `message`, `trace_id`,
`project_id`, `schema_id`, `environment`.

Logs de finalización exitosa añaden los campos numéricos de métricas: `processing_time_ms`,
`extractor_used`, `fallback_reason`, `llm_provider`, `llm_model`, `token_usage_input`,
`token_usage_output`, `cost_usd_estimated`. Son la **capa de métricas de Fase 1** para
Cloud Monitoring sin infraestructura adicional.

El `trace_id` se genera en middleware, se propaga por el pipeline y se incluye en las
respuestas de error.

---

## Health endpoint — `GET /v1/health`

```json
{
  "status": "ok",
  "version": "1.0.0",
  "environment": "production",
  "llm_provider": "anthropic",
  "llm_model": "claude-sonnet-4-20250514",
  "schemas_loaded": 3,
  "schemas_available": ["factura_simple", "orden_compra", "certificado_laboral"],
  "schemas_dir_accessible": true,
  "uptime_seconds": 3847
}
```

`status` puede ser `"degraded"` si el directorio es inaccesible o hay 0 schemas cargados.

---

## Restricciones y decisiones explícitas

- **No background tasks** en Fase 1. Procesamiento síncrono. Timeout de Cloud Run ≥ 60s.
- **No autenticación** en esta fase (la maneja el API Gateway / cliente).
- **No persistir PDFs**. Procesar en memoria y descartar.
- **No Pydantic dinámico** para validar el output del LLM. Usar `jsonschema.validate()`.
- **No RAG ni chunking semántico**. Envío completo hasta `PDF_MAX_PAGES_FULL`, luego truncado con WARNING.
- **No stubs vacíos**. Si un módulo no tiene contrato, no se agrega.
- Comentarios en **español**; identificadores en **inglés**.
- No agregar dependencias no listadas sin justificación explícita en comentario.

---

## Criterios de calidad evaluados

- El pipeline falla rápido y con mensajes claros en cada paso.
- Los errores nunca exponen stack traces ni raw LLM output al cliente externo.
- El `trace_id` aparece en todos los logs relacionados con un request.
- Los campos numéricos de métricas aparecen en los logs de finalización.
- La capa LLM es completamente intercambiable sin tocar `document_processor.py`.
- El normalizer opera sobre metadata del schema, no sobre heurísticas hardcodeadas.
- El criterio de fallback pymupdf → pdfplumber es configurable y loggeable.
- La estructura permite que Fase 2 agregue un extractor OCR como un nuevo paso sin
  modificar la interfaz pública ni `document_processor.py`.
