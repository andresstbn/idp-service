# Roadmap y próximos pasos

Estado de Fase 1 y plan hacia Fase 2+. Las decisiones de Fase 1 están pensadas para que
estos pasos no rompan la interfaz pública ni el orquestador.

---

## Estado actual — Fase 1 (completa)

✅ Endpoint `POST /v1/extract` (multipart) y `GET /v1/health`.
✅ Pipeline de 8 pasos con tiempos por paso y `trace_id` propagado.
✅ Extractor pymupdf → pdfplumber con criterio objetivo configurable.
✅ Detección de PDF sin texto nativo (gancho para OCR de Fase 2).
✅ Capa LLM abstracta e intercambiable (Anthropic, OpenAI) vía factory.
✅ Validación con `jsonschema` + reintentos con feedback.
✅ Normalización schema-driven (locale, fechas, números, arrays, objetos).
✅ Logs JSON como capa de métricas; errores sin trazas ni raw LLM output.
✅ Dockerfile no-root + script de despliegue a Cloud Run.

**Limitaciones conocidas de Fase 1**:
- Solo PDFs con texto nativo (los escaneados se rechazan con 400).
- Sin auth, sin base de datos, sin persistencia, procesamiento síncrono.
- `confidence` por campo siempre `null` (el contrato existe, no el dato).
- Costos del LLM estimados con tarifas hardcodeadas por provider.

---

## Fase 2 — OCR + visión multimodal

**Objetivo**: procesar documentos escaneados / sin texto nativo.

Plan de inserción **sin romper la interfaz**:
1. Nuevo extractor OCR (p. ej. Document AI, Tesseract, o visión multimodal del propio
   LLM) como **paso dentro de `pdf_extractor.py`**.
2. El gancho ya existe: hoy `NoNativeTextError` se lanza cuando no hay texto nativo; en
   Fase 2 ese punto **deriva al pipeline OCR** en vez de fallar.
3. `extraction_metadata.extractor_used` admite el nuevo valor (`"ocr"` / `"vision"`).
4. `document_processor.py` **no cambia**: sigue recibiendo un `ExtractionResult`.

**Confidence real por campo**:
- Poblar `field_metadata[campo].confidence` con la confianza del OCR/LLM.
- Cambiar `extraction_metadata.confidence_available` a `true`.
- El contrato ya está; solo se rellenan los valores.

---

## Mejoras transversales (priorizables)

### Observabilidad
- [ ] Integrar `trace_id` con **Cloud Trace** (header `X-Cloud-Trace-Context`).
- [ ] Métricas de tasa de fallback pymupdf→pdfplumber por `project_id` y `schema_id`
      (dashboards en Cloud Monitoring a partir de los logs ya existentes).
- [ ] Alertas sobre `cost_usd_estimated` agregado por proyecto.

### Robustez y rendimiento
- [ ] Caché de resultados por hash de (PDF + schema_id) para reprocesos idénticos.
- [ ] Límite de concurrencia / colas si el volumen lo exige (hoy es síncrono).
- [ ] Tarifas de LLM **configurables** (mover los precios hardcodeados a config o a un
      archivo de pricing por modelo).
- [ ] Soporte de más providers (Vertex AI / Gemini) reusando `BaseLLMProvider`.

### Contratos y multi-tenant
- [ ] **Rate limiting** real por `project_id` (el campo ya se captura para esto).
- [ ] **Autenticación**: hoy delegada al gateway; definir si se internaliza.
- [ ] Registro de schemas por proyecto (namespacing) si distintos clientes necesitan
      esquemas con el mismo nombre.
- [ ] Versionado explícito de schemas con migración asistida.

### Calidad
- [ ] Suite de tests automatizada (pytest) con: PDFs fixture, LLM stub, casos de
      normalización (union types, objetos anidados, locales), y rutas de error.
- [ ] CI que valide `requirements.txt` con instalación limpia y corra los tests.
- [ ] Validación de que cada schema en `schemas/` carga correctamente como test.

---

## Deuda técnica registrada

| Tema | Detalle | Dónde |
|---|---|---|
| Precios LLM hardcodeados | Tarifas por MTok embebidas en cada provider | `services/llm/*_provider.py` |
| Detección numérica sin locale | Heurística por posición de separador; ambigua en casos límite | `services/normalizer.py` `_autodetect_number` |
| Sin tests automatizados | La verificación es manual (ver AGENTS.md §5) | — |
| `requirements.txt` sin lock | Versiones fijadas a mano; falta resolución verificada en CI | `requirements.txt` |
