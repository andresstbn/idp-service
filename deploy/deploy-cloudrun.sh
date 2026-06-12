#!/usr/bin/env bash
#
# deploy-cloudrun.sh — Despliegue del IDP Service a Google Cloud Run.
#
# Fase 1 inicial: el LLM aún NO está en uso, así que se despliega con
# LLM_PROVIDER=mock. Esto permite levantar el servicio y ejercitar el pipeline
# completo (validación, extracción, normalización, contrato HTTP) sin API key,
# sin Secret Manager y sin service accounts adicionales.
#
# Cuando el LLM real esté listo, ver deploy/README-secret.md (o RULES §R10) para
# reintroducir Secret Manager y cambiar LLM_PROVIDER a anthropic/openai.
#
# Sigue las recomendaciones actuales de GCP:
#   - Artifact Registry (gcr.io está deprecado) con build por Cloud Build.
#   - Execution environment gen2, timeout >= 60s (pipeline síncrono).
#   - Sin acceso público por defecto (--no-allow-unauthenticated).
#
# Uso:
#   export PROJECT_ID=mi-proyecto      # o se usa el default de abajo
#   ./deploy/deploy-cloudrun.sh
#
# Variables de entorno reconocidas (con defaults):
#   PROJECT_ID           (datak-production) ID del proyecto GCP
#   REGION               (us-central1)
#   SERVICE              (idp-service)
#   REPOSITORY           (idp) repo de Artifact Registry
#   LLM_PROVIDER         (mock) mock | anthropic | openai
#   LLM_MODEL            (mock-model)
#   ENVIRONMENT          (production)
#   ALLOW_UNAUTH         (false) poner "true" para exponer públicamente (no recomendado)
#   MEMORY               (1Gi)
#   CPU                  (1)
#   TIMEOUT              (120) segundos; mínimo 60 por el procesamiento síncrono
#   CONCURRENCY          (8)
#   MIN_INSTANCES        (0)
#   MAX_INSTANCES        (2)
#
set -euo pipefail

# --- Resolución de configuración ---------------------------------------------
PROJECT_ID="${PROJECT_ID:-datak-production}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-idp-service}"
REPOSITORY="${REPOSITORY:-idp}"
LLM_PROVIDER="${LLM_PROVIDER:-mock}"
LLM_MODEL="${LLM_MODEL:-mock-model}"
ENVIRONMENT="${ENVIRONMENT:-production}"
ALLOW_UNAUTH="${ALLOW_UNAUTH:-false}"
MEMORY="${MEMORY:-1Gi}"
CPU="${CPU:-1}"
TIMEOUT="${TIMEOUT:-120}"
CONCURRENCY="${CONCURRENCY:-8}"
MIN_INSTANCES="${MIN_INSTANCES:-0}"
MAX_INSTANCES="${MAX_INSTANCES:-2}"

IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${SERVICE}"

# Directorio raíz del proyecto (un nivel por encima de deploy/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"

log() { printf '\n\033[1;34m▶ %s\033[0m\n' "$*"; }

# --- 1. Validaciones previas --------------------------------------------------
command -v gcloud >/dev/null || { echo "gcloud no está instalado"; exit 1; }
gcloud config set project "${PROJECT_ID}" >/dev/null

# Aviso si se intenta usar un provider real sin la complejidad de secretos.
if [[ "${LLM_PROVIDER}" != "mock" ]]; then
  echo "AVISO: LLM_PROVIDER=${LLM_PROVIDER} requiere inyectar la API key como secreto."
  echo "       Este script está simplificado para 'mock'. Reintroduce Secret Manager"
  echo "       antes de desplegar con un provider real (ver cabecera / RULES §R10)."
  exit 1
fi

# --- 2. Habilitar APIs necesarias --------------------------------------------
log "Habilitando APIs (run, cloudbuild, artifactregistry)"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com

# --- 3. Artifact Registry (idempotente) --------------------------------------
if ! gcloud artifacts repositories describe "${REPOSITORY}" \
      --location="${REGION}" >/dev/null 2>&1; then
  log "Creando repositorio de Artifact Registry: ${REPOSITORY}"
  gcloud artifacts repositories create "${REPOSITORY}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="Imágenes del IDP Service"
else
  log "Artifact Registry '${REPOSITORY}' ya existe"
fi

# --- 4. Build de la imagen con Cloud Build → Artifact Registry ----------------
GIT_SHA="$(git -C "${PROJECT_ROOT}" rev-parse --short HEAD 2>/dev/null || echo manual)"
TAG="${GIT_SHA}-$(date +%Y%m%d%H%M%S)"
log "Construyendo imagen ${IMAGE}:${TAG} con Cloud Build"
gcloud builds submit "${PROJECT_ROOT}" \
  --tag "${IMAGE}:${TAG}"

# --- 5. Despliegue a Cloud Run -----------------------------------------------
# Sin secretos: en modo mock no se necesita API key del LLM.
AUTH_FLAG="--no-allow-unauthenticated"
[[ "${ALLOW_UNAUTH}" == "true" ]] && AUTH_FLAG="--allow-unauthenticated"

log "Desplegando ${SERVICE} en Cloud Run (${REGION}) con LLM_PROVIDER=${LLM_PROVIDER}"
gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}:${TAG}" \
  --region="${REGION}" \
  --platform=managed \
  --execution-environment=gen2 \
  --memory="${MEMORY}" \
  --cpu="${CPU}" \
  --timeout="${TIMEOUT}" \
  --concurrency="${CONCURRENCY}" \
  --min-instances="${MIN_INSTANCES}" \
  --max-instances="${MAX_INSTANCES}" \
  --port=8080 \
  --set-env-vars="LLM_PROVIDER=${LLM_PROVIDER},LLM_MODEL=${LLM_MODEL},ENVIRONMENT=${ENVIRONMENT}" \
  ${AUTH_FLAG}

# --- 6. Resultado -------------------------------------------------------------
URL="$(gcloud run services describe "${SERVICE}" --region="${REGION}" --format='value(status.url)')"
log "Despliegue completado"
echo "  Servicio:  ${SERVICE}"
echo "  Imagen:    ${IMAGE}:${TAG}"
echo "  Provider:  ${LLM_PROVIDER} (sin LLM real)"
echo "  URL:       ${URL}"
echo
if [[ "${ALLOW_UNAUTH}" == "true" ]]; then
  echo "  Health:    curl ${URL}/v1/health"
else
  echo "  Health (requiere auth):"
  echo "    curl -H \"Authorization: Bearer \$(gcloud auth print-identity-token)\" ${URL}/v1/health"
fi
