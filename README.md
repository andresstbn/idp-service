# IDP Service — Fase 1

Microservicio de *Intelligent Document Processing* (IDP) para extracción de datos
estructurados de PDFs con capa de texto nativa, usando esquemas JSON registrados y
un LLM configurable (Anthropic / OpenAI). Stateless, pensado para Google Cloud Run.

En Fase 1 **solo** se procesan PDFs con texto embebido. Los documentos escaneados
(sin texto nativo) se rechazan con un `400` que indica que requieren OCR (Fase 2).

---

## Documentación

| Documento | Contenido |
|---|---|
| [`AGENTS.md`](AGENTS.md) | **Punto de partida** para agentes: mapa del repo, modelo mental, cómo verificar |
| [`RULES.md`](RULES.md) | Reglas de codificación y corrección técnica (cumplimiento obligatorio) |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Flujos clave del pipeline paso a paso |
| [`docs/SCHEMAS.md`](docs/SCHEMAS.md) | Cómo agregar/editar esquemas registrados |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Estado de Fase 1, próximos pasos y Fase 2 |
| [`docs/ORIGINAL_SPEC.md`](docs/ORIGINAL_SPEC.md) | Definición fundacional del servicio (fuente de verdad) |

---

## Arquitectura del pipeline

```
VALIDATE → LOAD SCHEMA → EXTRACT TEXT → NORMALIZE PRE-LLM →
LLM MAPPING → NORMALIZE OUTPUT → VALIDATE OUTPUT → ASSEMBLE RESPONSE
```

- **Extractor**: `pymupdf` primario, fallback a `pdfplumber` con criterio objetivo
  (`PDF_MIN_CHARS_PER_PAGE`), registrado en logs.
- **LLM**: capa abstracta (`BaseLLMProvider`) intercambiable vía factory; el
  orquestador no conoce el provider concreto.
- **Validación**: el output del LLM se valida con `jsonschema` contra el esquema
  registrado (no Pydantic dinámico).
- **Observabilidad**: logs JSON con `trace_id` por request; los logs de finalización
  llevan los campos numéricos de métricas para Cloud Monitoring.

---

## Ejecución local

Requiere **Python 3.12+**.

```bash
cd idp-service
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Editar .env y poner ANTHROPIC_API_KEY (o cambiar LLM_PROVIDER a openai + OPENAI_API_KEY)

uvicorn main:app --reload --port 8080
```

Health check:

```bash
curl http://localhost:8080/v1/health
```

Extracción:

```bash
curl -X POST http://localhost:8080/v1/extract \
  -F "file=@factura.pdf" \
  -F "schema_id=factura_simple" \
  -F "project_id=proyecto-a" \
  -F 'options={"page_range": [1, 3], "hints": "factura de servicios"}'
```

`options` es opcional y debe ser un JSON válido.

---

## Cómo agregar un nuevo schema

1. Crear un archivo `schemas/<nombre>.json` con un **JSON Schema Draft 7** válido.
2. El `schema_id` será el nombre del archivo sin `.json`.
3. (Opcional) Añadir metadata de normalización por campo:
   - `x-locale`: locale para separadores numéricos (`es-CO`, `es-ES`, `es-MX`, `en-US`).
   - `x-format`: pista de formato (p. ej. `currency`).
   - `format`: `date` / `date-time` para normalización a ISO 8601.
4. Reiniciar el servicio: los schemas se cargan en el *lifespan* de arranque.
   Los inválidos se excluyen del registro y se loggean como `ERROR` sin tumbar el
   servicio.

Ejemplo mínimo:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "properties": {
    "total": { "type": "number", "x-locale": "es-CO" },
    "fecha": { "type": "string", "format": "date" }
  }
}
```

Ver [`schemas/factura_simple.json`](schemas/factura_simple.json) como referencia.

---

## Variables de entorno

Todas se documentan en [`.env.example`](.env.example). Requeridas:

| Variable | Requerida | Descripción |
|---|---|---|
| `LLM_PROVIDER` | sí | `anthropic` o `openai` |
| `LLM_MODEL` | sí | Modelo del provider |
| `ANTHROPIC_API_KEY` | si provider=anthropic | API key de Anthropic |
| `OPENAI_API_KEY` | si provider=openai | API key de OpenAI |
| `SCHEMAS_DIR` | no (default `schemas`) | Directorio de schemas |
| `PDF_MIN_CHARS_PER_PAGE` | no (default `50`) | Umbral de fallback de extractor |
| `PDF_MAX_SIZE_MB` | no (default `20`) | Tamaño máximo de PDF |
| `PDF_MAX_PAGES_FULL` | no (default `30`) | Páginas máximas antes de truncar |

Si el provider configurado no está implementado o le falta su API key, el servicio
**falla en el arranque**, no en runtime.

---

## Despliegue en Cloud Run

**Fase 1 inicial (sin LLM real)** — el servicio se despliega con `LLM_PROVIDER=mock`,
así que **no requiere API key ni Secret Manager**. El script aplica las prácticas
actuales de GCP (Artifact Registry, gen2, sin acceso público por defecto):

```bash
export PROJECT_ID=mi-proyecto    # default: datak-production
./deploy/deploy-cloudrun.sh
```

El script es idempotente y parametrizable por variables de entorno (región, memoria,
CPU, timeout, concurrencia, instancias min/max). Ver la cabecera de
[`deploy/deploy-cloudrun.sh`](deploy/deploy-cloudrun.sh) para todas las opciones.

El provider `mock` devuelve valores vacíos válidos por campo (null/`[]`/`""` según el
schema), con costo y tokens en cero. Sirve para validar todo el pipeline end-to-end.
Cuando el LLM real esté listo, se reintroduce Secret Manager y se cambia `LLM_PROVIDER`
a `anthropic`/`openai` (ver [`RULES.md`](RULES.md) §R10).

Alternativa rápida (build desde fuente con Buildpacks, para pruebas):

```bash
gcloud run deploy idp-service \
  --source . \
  --region us-central1 \
  --timeout 120 \
  --set-env-vars LLM_PROVIDER=anthropic,ENVIRONMENT=production \
  --set-secrets ANTHROPIC_API_KEY=anthropic-api-key:latest
```

### Nota sobre el timeout

El procesamiento es **síncrono** (sin background tasks en Fase 1): el LLM puede
tardar varios segundos por documento. Configurar el **timeout de Cloud Run en
mínimo 60 s** (`--timeout 60`, recomendado 120 s para documentos grandes). El
contenedor respeta la variable `PORT` inyectada por Cloud Run (default 8080).

---

## Códigos de error

| Código | Causa |
|---|---|
| `400` | `schema_id` no existe / no es PDF válido / PDF sin texto nativo |
| `422` | Validación del request (campos del form u `options`) |
| `500` | Error interno; incluye `trace_id` para correlación en Cloud Logging |

Las respuestas de error nunca exponen stack traces ni el output crudo del LLM.
