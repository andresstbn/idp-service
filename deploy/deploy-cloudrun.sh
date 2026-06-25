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

SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL}"
COMPONENT_NAME="idp-service"

notify_slack() {
    local status=$1
    local env=$2
    local branch
    branch=$(git -C "${PROJECT_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    local text=""
    
    if [ "$status" == "start" ]; then
        text="🚀 Iniciando despliegue de *${COMPONENT_NAME}*\n• Ambiente: \`${env}\`\n• Rama: \`${branch}\`"
    elif [ "$status" == "success" ]; then
        text="✅ Despliegue exitoso de *${COMPONENT_NAME}*\n• Ambiente: \`${env}\`\n• Rama: \`${branch}\`"
    fi

    if [ -n "$text" ]; then
        curl -s -X POST -H 'Content-type: application/json' --data "{\"text\":\"${text}\"}" "${SLACK_WEBHOOK_URL}" > /dev/null || true
    fi
}

# Directorio raíz del proyecto (un nivel por encima de deploy/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"

log() { printf '\n\033[1;34m▶ %s\033[0m\n' "$*"; }

# --- Cargar variables desde .env si existe ------------------------------------
if [ -f "${PROJECT_ROOT}/.env" ]; then
  log "Cargando variables de entorno desde .env..."
  while IFS= read -r line || [ -n "$line" ]; do
    line=$(echo "$line" | xargs)
    if [[ ! "$line" =~ ^# ]] && [[ "$line" == *=* ]]; then
      key="${line%%=*}"
      val="${line#*=}"
      if [ -z "${!key:-}" ]; then
        export "$key"="$val"
      fi
    fi
  done < "${PROJECT_ROOT}/.env"
fi

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

# Visión (Plan B)
VISION_PROVIDER="${VISION_PROVIDER:-}"
VISION_MODEL="${VISION_MODEL:-deepseek-chat}"

IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${SERVICE}"

# --- 1. Validaciones previas --------------------------------------------------
command -v gcloud >/dev/null || { echo "gcloud no está instalado"; exit 1; }
gcloud config set project "${PROJECT_ID}" >/dev/null

# Validaciones de llaves requeridas
if [[ "${LLM_PROVIDER}" == "anthropic" && -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "ERROR: LLM_PROVIDER=anthropic requiere ANTHROPIC_API_KEY configurada."
  exit 1
fi
if [[ "${LLM_PROVIDER}" == "openai" && -z "${OPENAI_API_KEY:-}" ]]; then
  echo "ERROR: LLM_PROVIDER=openai requiere OPENAI_API_KEY configurada."
  exit 1
fi
if [[ "${VISION_PROVIDER}" == "deepseek" && -z "${DEEPSEEK_API_KEY:-}" ]]; then
  echo "ERROR: VISION_PROVIDER=deepseek requiere DEEPSEEK_API_KEY configurada."
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
AUTH_FLAG="--no-allow-unauthenticated"
[[ "${ALLOW_UNAUTH}" == "true" ]] && AUTH_FLAG="--allow-unauthenticated"

# Definición de variables a inyectar dinámicamente en Cloud Run
IDP_KEYS=(
  LLM_PROVIDER
  LLM_MODEL
  ANTHROPIC_API_KEY
  OPENAI_API_KEY
  LLM_MAX_RETRIES
  VISION_PROVIDER
  VISION_MODEL
  DEEPSEEK_API_KEY
  VISION_MAX_PAGES
  VISION_RENDER_DPI
  SCHEMAS_DIR
  PDF_MIN_CHARS_PER_PAGE
  PDF_MAX_SIZE_MB
  PDF_MAX_PAGES_FULL
  LOG_LEVEL
  ENVIRONMENT
  SERVICE_VERSION
)

ENV_VARS_ARGS=""
for key in "${IDP_KEYS[@]}"; do
  val="${!key:-}"
  if [ -n "$val" ]; then
    if [ -z "$ENV_VARS_ARGS" ]; then
      ENV_VARS_ARGS="${key}=${val}"
    else
      ENV_VARS_ARGS="${ENV_VARS_ARGS},${key}=${val}"
    fi
  fi
done

log "Desplegando ${SERVICE} en Cloud Run (${REGION}) con variables del entorno"
notify_slack "start" "${ENVIRONMENT}"
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
  --set-env-vars="${ENV_VARS_ARGS}" \
  ${AUTH_FLAG}

# --- 6. Resultado -------------------------------------------------------------
URL="$(gcloud run services describe "${SERVICE}" --region="${REGION}" --format='value(status.url)')"
log "Despliegue completado"
notify_slack "success" "${ENVIRONMENT}"
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
