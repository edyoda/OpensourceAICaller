#!/usr/bin/env bash
# Deploy to Cloud Run. Secrets live in Secret Manager, never in the image.
#
#   ./scripts/deploy.sh secrets   # one-time: push .env values to Secret Manager
#   ./scripts/deploy.sh           # build + deploy
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=${PROJECT:-$(gcloud config get-value project)}
REGION=${REGION:-asia-south1}      # Mumbai: closest to Sarvam and Exotel
SERVICE=${SERVICE:-edyoda-call-agent}
MIN_INSTANCES=${MIN_INSTANCES:-1}  # 0 saves money but cold-starts a live call

# Exotel settings come from .env (never hardcode account details in the repo).
set -a; [ -f .env ] && source .env; set +a
: "${EXOTEL_SID:?set EXOTEL_SID in .env}"
: "${EXOTEL_CALLER_ID:?set EXOTEL_CALLER_ID in .env}"
: "${EXOTEL_APP_ID:?set EXOTEL_APP_ID in .env}"
EXOTEL_SUBDOMAIN=${EXOTEL_SUBDOMAIN:-api.exotel.com}
EXOTEL_ALLOWED_ACCOUNTS=${EXOTEL_ALLOWED_ACCOUNTS:-$EXOTEL_SID}
LANGUAGE_CODE=${LANGUAGE_CODE:-en-IN}
TTS_SPEAKER=${TTS_SPEAKER:-ritu}

secrets() {
  set -a; source .env; set +a
  for name in SARVAM_API_KEY ANTHROPIC_API_KEY EXOTEL_API_KEY EXOTEL_API_TOKEN; do
    value="${!name}"
    [ -z "$value" ] && { echo "skip $name (unset)"; continue; }
    if gcloud secrets describe "$name" --project "$PROJECT" >/dev/null 2>&1; then
      printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- --project "$PROJECT"
    else
      printf '%s' "$value" | gcloud secrets create "$name" --data-file=- --project "$PROJECT"
    fi
    echo "stored $name"
    # The Cloud Run runtime SA needs read access per secret, or the revision
    # fails to start with "Permission denied on secret".
    gcloud secrets add-iam-policy-binding "$name" --project "$PROJECT" \
      --member "serviceAccount:$(gcloud projects describe "$PROJECT" \
        --format 'value(projectNumber)')-compute@developer.gserviceaccount.com" \
      --role roles/secretmanager.secretAccessor >/dev/null
  done
}

deploy() {
  gcloud run deploy "$SERVICE" \
    --source . \
    --project "$PROJECT" \
    --region "$REGION" \
    --allow-unauthenticated \
    --min-instances "$MIN_INSTANCES" \
    --max-instances 5 \
    --concurrency 20 \
    --cpu 1 --memory 512Mi \
    --timeout 3600 \
    --set-secrets "SARVAM_API_KEY=SARVAM_API_KEY:latest,ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,EXOTEL_API_KEY=EXOTEL_API_KEY:latest,EXOTEL_API_TOKEN=EXOTEL_API_TOKEN:latest" \
    --set-env-vars "EXOTEL_SID=${EXOTEL_SID},EXOTEL_SUBDOMAIN=${EXOTEL_SUBDOMAIN},EXOTEL_CALLER_ID=${EXOTEL_CALLER_ID},EXOTEL_APP_ID=${EXOTEL_APP_ID},EXOTEL_ALLOWED_ACCOUNTS=${EXOTEL_ALLOWED_ACCOUNTS},LANGUAGE_CODE=${LANGUAGE_CODE},TTS_SPEAKER=${TTS_SPEAKER}"

  url=$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
        --format 'value(status.url)')
  echo
  echo "service:  $url"
  echo "voicebot: ${url/https:/wss:}/ws/exotel?sample-rate=16000"
}

case "${1:-deploy}" in
  secrets) secrets ;;
  deploy)  deploy ;;
  *) echo "usage: deploy.sh [secrets|deploy]"; exit 1 ;;
esac
