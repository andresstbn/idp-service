# AGENTS.md — Punto de partida para agentes

> Este archivo es el **primer documento que debe leer cualquier agente** (o
> desarrollador) antes de tocar el servicio. Resume qué es, cómo está organizado,
> cómo correrlo y dónde está cada cosa. Para las reglas de codificación de
> obligado cumplimiento, ver [`RULES.md`](RULES.md).

---

## 1. Qué es este servicio

**IDP Service** es un microservicio de *Intelligent Document Processing* que extrae
datos estructurados de PDFs **con capa de texto nativa**, los mapea a un **JSON
Schema registrado** mediante un LLM configurable (Anthropic / OpenAI), normaliza
los valores y los valida. Es **stateless** y está pensado para **Google Cloud Run**.

- **Fase 1 (actual)**: PDF nativo (texto embebido) + esquemas en disco. **Sin base
  de datos, sin auth, sin OCR.**
- **Fase 2 (futura)**: OCR + visión multimodal para documentos escaneados. Ver
  [`docs/ROADMAP.md`](docs/ROADMAP.md).

El documento fundacional completo está en
[`docs/ORIGINAL_SPEC.md`](docs/ORIGINAL_SPEC.md) — es la fuente de verdad sobre el
**porqué** de cada decisión. Si una propuesta de cambio contradice ese documento,
detente y plantéalo explícitamente.

---

## 2. Mapa del repositorio

```
idp-service/
├── AGENTS.md                      # ← estás aquí
├── RULES.md                       # reglas de codificación (cumplimiento obligatorio)
├── README.md                      # guía de uso para humanos (run local, deploy)
├── docs/
│   ├── ORIGINAL_SPEC.md           # definición que dio origen al servicio
│   ├── ARCHITECTURE.md            # flujos clave paso a paso
│   ├── SCHEMAS.md                 # cómo agregar/editar esquemas
│   └── ROADMAP.md                 # próximos pasos y Fase 2
├── main.py                        # entrypoint FastAPI: lifespan, middleware, router
├── api/v1/
│   ├── router.py                  # agrupa rutas v1
│   └── endpoints/extract.py       # POST /v1/extract  +  GET /v1/health
├── core/
│   ├── config.py                  # Settings (pydantic-settings) — única fuente de config
│   ├── exceptions.py              # IDPError y subtipos → códigos HTTP
│   └── logging.py                 # logs JSON + trace_id (ContextVar)
├── services/
│   ├── schema_registry.py         # carga/valida esquemas en memoria
│   ├── pdf_extractor.py           # pymupdf → pdfplumber (criterio objetivo)
│   ├── normalizer.py              # normalización schema-driven
│   ├── document_processor.py      # orquestador de los 8 pasos del pipeline
│   └── llm/
│       ├── base.py                # BaseLLMProvider (interfaz abstracta) + system prompt
│       ├── anthropic_provider.py  # tool-use nativo + validación jsonschema
│       ├── openai_provider.py     # function-calling nativo + validación jsonschema
│       └── factory.py             # instancia el provider según config (falla en startup)
├── models/
│   ├── requests.py                # modelos HTTP request/response (Pydantic v2)
│   └── extraction.py              # modelos internos del pipeline (dataclasses)
├── schemas/
│   └── factura_simple.json        # esquema de ejemplo (referencia)
├── deploy/
│   └── deploy-cloudrun.sh         # despliegue a Cloud Run
├── Dockerfile · .dockerignore · .env.example · requirements.txt
```

---

## 3. Modelo mental: el pipeline

Todo gira en torno a un pipeline secuencial de **8 pasos** en
[`services/document_processor.py`](services/document_processor.py). Cada paso
mide su duración y la registra en el log del request:

```
1. VALIDATE         magic bytes + tamaño            → 400 si falla
2. LOAD SCHEMA      del registro en memoria          → 400 si no existe
3. EXTRACT TEXT     pymupdf → fallback pdfplumber     → 400 si no hay texto nativo
4. NORMALIZE PRE    limpieza del texto extraído
5. LLM MAPPING      texto + JSON Schema → dict        → 500 si agota reintentos
6. NORMALIZE OUT    valores schema-driven (x-locale)
7. VALIDATE OUT     jsonschema.validate()             → 500 si no valida
8. ASSEMBLE         data + field_metadata + métricas  → 200
```

El detalle de cada paso, con sus invariantes, está en
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

**Regla de oro de extensibilidad**: la capa LLM se toca **solo** detrás de
`BaseLLMProvider`; el OCR de Fase 2 entra **dentro** de `pdf_extractor.py`. Ninguno
de los dos debe obligar a editar `document_processor.py` ni la interfaz pública.

---

## 4. Cómo correrlo (local)

Requiere **Python 3.12+**.

```bash
cd idp-service
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# editar .env: poner ANTHROPIC_API_KEY (o LLM_PROVIDER=openai + OPENAI_API_KEY)

uvicorn main:app --reload --port 8080
```

Pruebas rápidas:

```bash
curl http://localhost:8080/v1/health

curl -X POST http://localhost:8080/v1/extract \
  -F "file=@factura.pdf" \
  -F "schema_id=factura_simple" \
  -F "project_id=proyecto-a" \
  -F 'options={"page_range":[1,3],"hints":"factura de servicios"}'
```

---

## 5. Cómo verificar un cambio (obligatorio antes de cerrar)

No basta con que compile. El flujo de verificación mínimo:

```bash
# 1. Byte-compile de todo
python -m py_compile main.py core/*.py services/*.py services/llm/*.py \
    models/*.py api/v1/router.py api/v1/endpoints/extract.py

# 2. Import + arranque de lifespan + endpoints con TestClient
#    (health, error de schema_id, no-PDF, options inválido)
# 3. Pipeline end-to-end con un PDF real y un LLM stub que devuelva
#    valores en formato "sucio" para ejercitar el normalizer.
```

Patrón de stub del LLM (no consume API real):

```python
from fastapi.testclient import TestClient
import main
from models.extraction import UsageMetrics

async def fake_extract(text, schema, trace_id, hints=None):
    return {...}, UsageMetrics(input_tokens=1000, output_tokens=120,
                               cost_usd_estimated=0.0021)

with TestClient(main.app) as c:
    main.app.state.processor._llm.extract = fake_extract
    r = c.post("/v1/extract", files=..., data=...)
```

Casos que **siempre** hay que cubrir al tocar el pipeline:
- Tipos anulables en union (`["number","null"]`).
- Objetos anidados dentro de arrays (`line_items[].unit_price`).
- Fechas en formatos locales (`dd/mm/yyyy`) → ISO 8601.
- Moneda con separadores de miles/decimal según `x-locale`.

---

## 6. Tareas frecuentes — a dónde ir

| Quiero… | Archivo / doc |
|---|---|
| Agregar/editar un esquema | [`docs/SCHEMAS.md`](docs/SCHEMAS.md), `schemas/` |
| Cambiar el criterio de fallback de extractor | `services/pdf_extractor.py` + `PDF_MIN_CHARS_PER_PAGE` |
| Agregar un nuevo provider LLM | `services/llm/` + registrar en `factory.py` |
| Cambiar reglas de normalización | `services/normalizer.py` (schema-driven, ver RULES) |
| Tocar el contrato de la respuesta | `models/requests.py` (¡versionar! ver RULES) |
| Añadir un campo de log/métrica | `services/document_processor.py` (log de finalización) |
| Desplegar | `deploy/deploy-cloudrun.sh`, [`README.md`](README.md) |
| Entender el "porqué" de una decisión | [`docs/ORIGINAL_SPEC.md`](docs/ORIGINAL_SPEC.md) |

---

## 7. Invariantes que nunca se rompen

Estas son no negociables; la lista completa con justificación está en
[`RULES.md`](RULES.md):

1. Las respuestas de error **nunca** exponen stack traces ni raw output del LLM.
2. Todo log relacionado con un request lleva `trace_id`.
3. La validación del output del LLM usa `jsonschema`, **no** Pydantic dinámico.
4. La config se lee **solo** desde `core/config.py` (nada de `os.environ` suelto).
5. Comentarios en **español**; identificadores (variables/funciones/clases) en **inglés**.
6. No se agregan dependencias fuera de `requirements.txt` sin justificación en comentario.
7. No hay stubs vacíos ni módulos sin contrato.
