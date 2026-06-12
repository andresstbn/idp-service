# Integración datak-app ↔ idp-service (schema `rut`)

Este documento describe los cambios pendientes en **datak-app** para consumir
`idp-service` en lugar de `extractDataFromRUTPdf()`. No implementa nada; es
la especificación para quien realice el trabajo.

---

## Contexto

`datak-app` actualmente extrae el RUT en el cliente (browser) usando
`app/composables/extractPDF.ts → extractDataFromRUTPdf()`, que usa coordenadas
X/Y de `pdfjs-dist`. `idp-service` reemplaza esa lógica con un extractor
server-side robusto.

**Restricción**: el reemplazo debe mantener compatibilidad con todo el código que
hoy consume `RutPdfSummary`, o documentar explícitamente qué cambia.

---

## 1. Autenticación GAE Standard → Cloud Run

### Configuración GCP (una sola vez, por ambiente)

```bash
# SERVICE_URL = URL completa del Cloud Run (ej: https://idp-service-xxx-uc.a.run.app)
# PROJECT = ID del proyecto GCP
# REGION  = región del Cloud Run

gcloud run services add-iam-policy-binding idp-service \
  --region=$REGION \
  --member="serviceAccount:$PROJECT@appspot.gserviceaccount.com" \
  --role="roles/run.invoker"
```

idp-service debe estar desplegado con `--no-allow-unauthenticated`.

### En el backend de datak-app (Node.js / Python)

Usar `google-auth-library` (Node) o `google-auth` (Python) para obtener un
identity token firmado con el service account de GAE Standard. La librería renueva
el token automáticamente; no se gestionan secretos.

```typescript
// Node.js (backend de datak-app en GAE Standard)
import { GoogleAuth } from 'google-auth-library';

const auth = new GoogleAuth();

async function getIdpToken(idpServiceUrl: string): Promise<string> {
  const client = await auth.getIdTokenClient(idpServiceUrl);
  const headers = await client.getRequestHeaders();
  return headers.Authorization; // "Bearer <token>"
}
```

El token se pasa en el header `Authorization: Bearer <token>` de cada request a
idp-service. Cloud Run lo valida automáticamente vía IAM.

---

## 2. Endpoint de idp-service para RUT

```
POST /v1/extract
Content-Type: multipart/form-data
Authorization: Bearer <identity_token>

file=<pdf_bytes>
schema_id=rut
project_id=<id_del_proyecto>
options={"page_range": [1, 1]}   ← solo Hoja 1, ahorra procesamiento
```

### Respuesta exitosa (200)

```jsonc
{
  "schema_id": "rut",
  "project_id": "...",
  "extraction_metadata": {
    "extractor_used": "dian_rut",
    "llm_provider": "none",
    "cost_usd_estimated": 0.0,
    // ...
  },
  "data": {
    "nit": "890900223",
    "dv": "7",
    "direccion_seccional": "Impuestos de Medellín",
    "person_type": "JUR",
    "id_type_label": "NIT",
    "razon_social": "CAMILO ALBERTO MEJIA & CIA S.A.S.",
    "nombre_comercial": "C.A. MEJIA Y CIA",
    "pais_nombre": "COLOMBIA",
    "departamento_nombre": "Antioquia",
    "ciudad_nombre": "Marinilla",
    "direccion": "VDA BELEN KM 38 200 M T AUT MED BOGOTA",
    "correo": "carolina.castrillon@camejia.com",
    "telefono": "4446767",
    "codigo_ciiu": "2599",
    "primer_apellido": null,
    "segundo_apellido": null,
    "primer_nombre": null,
    "otros_nombres": null
  },
  "field_metadata": { ... }
}
```

### Errores relevantes

| HTTP | Causa | Acción en datak-app |
|---|---|---|
| 400 `invalid_pdf` | PDF corrupto o sin texto nativo (imagen escaneada) | Mostrar error al usuario, solicitar ingreso manual |
| 400 `no_native_text` | PDF escaneado (sin texto embebido) | Ídem |
| 404 `schema_not_found` | `schema_id=rut` no registrado | Error de configuración |
| 422 `validation_error` | Campos `options` inválidos | Error de código |
| 500 | Error interno idp-service | Retry o fallback |

---

## 3. Mapeo del contrato de idp-service → `RutPdfSummary` actual

La función `extractDataFromRUTPdf()` retorna `RutPdfSummary`. idp-service retorna
campos con nombres diferentes y **sin** los IDs de catálogo (retorna texto plano).
El adaptador en datak-app hace el mapeo.

### Diferencias de contrato

| Campo en `RutPdfSummary` (actual) | Campo en `data` de idp-service | Diferencia |
|---|---|---|
| `id_number` | `nit` | Solo nombre |
| `person_type` | `person_type` | Igual: `"JUR"` \| `"NAT"` |
| `id_type` | `id_type_label` | idp retorna texto; datak-app mapea a ID con `ID_TYPES` |
| `first_name` | `razon_social` (JUR) / `primer_nombre` (NAT) | Separado por tipo |
| `second_name` | `otros_nombres` | Solo nombre |
| `first_surname` | `primer_apellido` | Solo nombre |
| `second_surname` | `segundo_apellido` | Solo nombre |
| `address` | `direccion` | Solo nombre |
| `email` | `correo` | Solo nombre |
| `country` | `pais_nombre` | ⚠️ datak-app debe mapear a ID con `COUNTRIES` |
| `department` | `departamento_nombre` | ⚠️ datak-app debe mapear a ID con `DEPARTMENTS` |
| `city` | `ciudad_nombre` | ⚠️ datak-app debe mapear a ID con `CITIES` |
| `phone` | `telefono` | Solo nombre |
| `activity_code` | `codigo_ciiu` | Solo nombre |
| `id_info`, `id_type_info`, etc. | *(no existen)* | Eran artefactos internos; se eliminan |

### Campos nuevos (sin equivalente en `RutPdfSummary`)

| Campo nuevo | Descripción |
|---|---|
| `dv` | Dígito de verificación del NIT. Antes se descartaba; ahora disponible |
| `nombre_comercial` | Campo 36. Antes no se extraía |
| `direccion_seccional` | Dirección seccional DIAN. Nuevo |

---

## 4. Adaptador propuesto en datak-app

Crear `app/composables/useRutIdp.ts` (o similar) que:

1. Sube el PDF al **backend de datak-app** (no directamente desde el browser a idp-service, para que el token de autenticación sea server-side).
2. El backend llama a `POST /v1/extract` con el token OIDC.
3. El backend recibe la respuesta y hace el mapeo de catálogos:
   - `pais_nombre` → `COUNTRIES.find(c => c.name.toLowerCase() === pais_nombre.toLowerCase())?.id`
   - `departamento_nombre` → ídem con `DEPARTMENTS`
   - `ciudad_nombre` → ídem con `CITIES`
   - `id_type_label` → `ID_TYPES.find(t => t.label === id_type_label)?.value ?? "NIT"`
4. Construye un objeto compatible con `RutPdfSummary` y lo retorna al frontend.

**Nota sobre el mapeo de catálogos**: los nombres en el RUT pueden tener ligeras
variaciones ortográficas (ej: `"Bogotá D.C."` vs `"Bogotá, D.C."`). El mapeo
`toLowerCase().includes()` o `startsWith()` es más robusto que igualdad exacta.

### Manejo de errores

Cuando idp-service retorna 400 (`invalid_pdf` o `no_native_text`):
- El frontend debe mostrar un mensaje claro: *"No se pudo leer el RUT automáticamente. Por favor ingresa los datos manualmente."*
- No silenciar el error ni reintentar automáticamente.

---

## 5. Estrategia de rollout

1. **Fase 1**: `useRutIdp.ts` llama a idp-service; si falla con 400/500, hace fallback a `extractDataFromRUTPdf()` (client-side). Permite comparar resultados en producción.
2. **Fase 2**: Cuando la tasa de éxito de idp-service es satisfactoria, eliminar el fallback y `extractDataFromRUTPdf()`.

El fallback en Fase 1 está justificado porque hay PDFs corruptos o escaneados (SERMANIN, GOOD GROUP en la muestra) que idp-service no puede procesar.

---

## 6. Personas naturales

El extractor `dian_rut` tiene implementación **tentativa** para personas naturales
(`_extract_natural_person_names`): sin PDFs de personas naturales en la muestra,
los índices de campos 31-34 no se validaron. Antes de habilitar personas naturales
en producción, obtener al menos 3 RUTs de personas naturales y ajustar el extractor.
