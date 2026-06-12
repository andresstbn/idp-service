# RULES.md — Reglas de codificación y corrección técnica

> Reglas de **cumplimiento obligatorio** para mantener coherencia y corrección.
> Derivan del documento fundacional ([`docs/ORIGINAL_SPEC.md`](docs/ORIGINAL_SPEC.md)).
> Si un cambio necesita romper una regla, **no lo hagas en silencio**: explícalo y
> justifícalo en el PR/respuesta. Para orientación general y mapa del repo, ver
> [`AGENTS.md`](AGENTS.md).

---

## R0. Convenciones de lenguaje y estilo

- **Comentarios y docstrings en español.** Texto orientado a quien mantiene el código.
- **Identificadores en inglés**: variables, funciones, clases, módulos.
- **Type hints obligatorios** en firmas públicas. `from __future__ import annotations`
  al inicio de cada módulo.
- Estilo: longitud de línea ~90, imports ordenados (stdlib → terceros → locales).
- Sin código muerto, sin `print()` de depuración, sin TODO sin issue asociado.

---

## R1. Aislamiento de errores y seguridad de la respuesta

- Las respuestas al cliente externo **nunca** contienen: stack traces, mensajes de
  excepción de librerías, ni el **raw output del LLM**.
- Toda excepción de dominio hereda de `IDPError` (`core/exceptions.py`) y expone:
  - `status_code` → código HTTP.
  - `public_message` → texto seguro y descriptivo para el cliente.
- El endpoint traduce `IDPError` a su `status_code`; cualquier `Exception` no prevista
  cae en una **barrera final** que devuelve `500` genérico con `trace_id`.
- Los detalles sensibles (errores de validación, raw LLM output) se **loggean**
  aparte (nivel WARNING/ERROR) y se correlacionan por `trace_id`.

```python
# Correcto: detalle sensible al log, mensaje seguro al cliente
log(logger, logging.WARNING, "output no validó", raw_llm_output=raw, validation_errors=detail)
raise OutputValidationError()  # public_message genérico
```

**Mapeo de códigos** (no inventar otros sin justificación):

| Código | Cuándo |
|---|---|
| `400` | schema_id inexistente · no es PDF (magic bytes) · PDF sin texto nativo · supera tamaño |
| `422` | validación Pydantic del request o de `options` |
| `500` | fallo del LLM tras reintentos · output que no valida · excepción no prevista |

---

## R2. Observabilidad: trace_id y métricas

- El `trace_id` se genera en el **middleware** (`main.py`) al inicio de cada request,
  se propaga por `ContextVar` (`core/logging.py`) y se incluye en **todos** los logs
  y en **todas** las respuestas de error.
- **Nunca** pasar `trace_id` como parámetro suelto entre funciones internas para
  loggear: usar el `ContextVar` ya enlazado. (Sí se pasa al provider LLM por contrato
  de interfaz.)
- Los **logs JSON** son la capa de métricas de Fase 1. El log de **finalización
  exitosa** del request debe incluir estos campos numéricos planos (para Cloud
  Monitoring sin infraestructura extra):
  `processing_time_ms`, `extractor_used`, `fallback_reason`,
  `llm_provider`, `llm_model`, `token_usage_input`, `token_usage_output`,
  `cost_usd_estimated`.
- Si agregas una nueva métrica, va **plana** en el log de finalización, no anidada.

---

## R3. Configuración

- **Única fuente de configuración**: `core/config.py` (`pydantic-settings`).
- Prohibido leer `os.environ` directamente fuera de ese módulo.
- Toda nueva variable: se agrega a `Settings`, con default sensato, y se documenta en
  `.env.example` con comentario. Sin defaults mágicos dispersos por el código.
- Los umbrales operativos (p. ej. fallback de extractor, tamaño máximo, páginas) son
  **configurables**, nunca hardcodeados.

---

## R4. Capa LLM: intercambiable y aislada

- El pipeline depende **solo** de `BaseLLMProvider.extract(...)`. `document_processor.py`
  **no conoce** el provider concreto.
- Un nuevo provider:
  1. Implementa `BaseLLMProvider` en `services/llm/<nombre>_provider.py`.
  2. Se registra en `services/llm/factory.py`.
  3. Calcula su **propio costo** y devuelve `UsageMetrics`.
  4. Usa el **system prompt común** (`services/llm/base.py`): extractor estricto, campos
     ausentes → `null`, prohibido inventar/inferir, tipos estrictos (ISO 8601, números
     sin separadores de miles).
- **Falla en startup, no en runtime**: si el provider configurado no existe o le falta
  API key, el servicio no debe arrancar (el factory lanza en el lifespan).
- La generación estructurada usa el mecanismo **nativo** del provider (tool-use en
  Anthropic, function-calling en OpenAI) con el **JSON Schema registrado** como
  esquema de la herramienta. **No** se construyen modelos Pydantic dinámicos.

---

## R5. Validación: jsonschema, no Pydantic dinámico

- El output del LLM es un `dict` Python validado con `jsonschema.Draft7Validator`
  contra el esquema **registrado**.
- Pydantic v2 se usa **solo** para: request/response HTTP, `options`, y config interna.
- Reintentos del LLM (`LLM_MAX_RETRIES`): ante output que no valida, se reintenta
  **realimentando el error de validación** como feedback al modelo. Agotados los
  reintentos → `500` con `trace_id`.
- La validación final del paso 7 es la **última barrera**: si falla, se loggea el raw
  output como WARNING y se devuelve `500`.

---

## R6. Normalización schema-driven

- El normalizer (`services/normalizer.py`) opera sobre la **metadata del schema**, no
  sobre heurísticas hardcodeadas por nombre de campo.
- Prioridad de reglas:
  1. `format` (`date`, `date-time`) → ISO 8601.
  2. `x-locale` / `x-format` → separadores numéricos del locale.
  3. `type` (incluyendo uniones como `["number","null"]`) → normalización conservadora.
- **Tipos en union**: extraer el tipo no-`null` para decidir el tratamiento
  (`_primary_type`). Nunca asumir que `type` es un string escalar.
- **Recursión**: arrays normalizan cada item; objetos normalizan cada propiedad por su
  sub-schema. No olvidar objetos anidados dentro de arrays.
- **Tolerancia a fallos**: si un valor no se puede normalizar, **conservar el original**
  y loggear WARNING (campo, valor, error). **No** fallar el pipeline aquí — lo detecta
  la validación del paso 7 con un error más descriptivo.

---

## R7. Extracción de PDF

- Orden fijo: **pymupdf primario → pdfplumber fallback**.
- El criterio de fallback es **objetivo y loggeable**:
  `chars_per_page < PDF_MIN_CHARS_PER_PAGE`. Se registra `extractor_used` y
  `fallback_reason` (null si no hubo fallback) en el log del request.
- **Detección de PDF sin texto nativo** antes de extraer: si ninguna página supera
  `PDF_MIN_CHARS_PER_PAGE / 10`, se clasifica como sin texto nativo → `400` con mensaje
  que menciona OCR de Fase 2. (Este es el punto de entrada del OCR futuro.)
- **Documentos largos**: nada de RAG ni chunking semántico. Hasta `PDF_MAX_PAGES_FULL`
  páginas se envía completo; por encima, se usa `page_range` si viene, o se truncan las
  primeras N páginas **con WARNING** (`truncated=True`). Nunca truncar en silencio.

---

## R8. Contratos de API estables y versionados

- Las rutas viven bajo `/v1`. Un **cambio incompatible** del contrato de
  request/response exige una **nueva versión** (`/v2`), no mutar `/v1`.
- Cambios **aditivos compatibles** (campos opcionales nuevos) sí pueden ir en `/v1`.
- El contrato de `field_metadata` con `confidence: null` se mantiene aunque en Fase 1
  no haya confidence: existe para Fase 2. No eliminarlo.
- `extraction_metadata` es parte del contrato: si agregas un campo, que sea opcional o
  con default para no romper consumidores.

---

## R9. Restricciones explícitas de Fase 1 (no violar sin decisión de arquitectura)

- **Sin background tasks**: procesamiento **síncrono**. (Timeout de Cloud Run ≥ 60 s.)
- **Sin autenticación**: la maneja el API Gateway / cliente aguas arriba.
- **Sin persistir PDFs**: procesar en memoria y descartar.
- **Sin base de datos**: los esquemas se cargan de disco en el lifespan.
- **Sin stubs vacíos**: si un módulo no tiene contrato definido, no se agrega.

---

## R10. Dependencias y despliegue

- `requirements.txt` con versiones **fijadas con `==`**. Nueva dependencia → justificar
  en comentario el porqué.
- El `Dockerfile` corre como usuario **no-root** y respeta la variable `PORT` de
  Cloud Run (default 8080). `.dockerignore` excluye `__pycache__`, `.env`, `.git`,
  `*.pyc`, `tests/`, `.venv`.
- Despliegue vía `deploy/deploy-cloudrun.sh`. La API key del LLM se inyecta como
  **secreto** (Secret Manager), nunca como variable de entorno en claro ni en la imagen.

---

## Checklist antes de cerrar un cambio

- [ ] Compila (`py_compile` de todos los módulos).
- [ ] Arranca el lifespan y `/v1/health` responde `ok`.
- [ ] Pipeline end-to-end probado con PDF real + LLM stub (ver AGENTS.md §5).
- [ ] Errores no exponen trazas ni raw LLM output; todos llevan `trace_id`.
- [ ] El log de finalización trae los campos numéricos de métricas.
- [ ] Config nueva en `core/config.py` **y** en `.env.example`.
- [ ] Comentarios en español, identificadores en inglés.
- [ ] No se rompió el contrato `/v1` (o se versionó).
- [ ] `requirements.txt` con `==` y sin dependencias injustificadas.
