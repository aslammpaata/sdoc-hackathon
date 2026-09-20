#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="sdoc-hackathon"
REGION="asia-southeast1"
REPO="sdoc-repo"
IMAGE_NAME="sdoc-api"
SERVICE_NAME="sdoc-api"
BUCKET="sdoc-hackathon-attachments"
# On Cloud Run the inbox is the static bundle uploaded to GCS, not the local Docker server.
INBOX_SOURCE="gs://${BUCKET}/dataset"

# Tag images by git commit so you always know exactly what code is live.
# Falls back to a timestamp if this isn't a git repo yet.
TAG="$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)"
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  TAG="${TAG}-dirty"   # uncommitted changes are included in the build
fi

IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${IMAGE_NAME}:${TAG}"

echo "==> Building $IMAGE"
docker build -t "$IMAGE" .

echo "==> Pushing to Artifact Registry"
docker push "$IMAGE"

echo "==> Deploying to Cloud Run"
gcloud run deploy "$SERVICE_NAME" \
  --image="$IMAGE" \
  --region="$REGION" \
  --platform=managed \
  --allow-unauthenticated \
  --timeout=1800 \
  --set-env-vars="GCP_PROJECT=${PROJECT_ID},GCS_BUCKET=${BUCKET},INBOX_SOURCE=${INBOX_SOURCE}"

echo "==> Live at:"
gcloud run services describe "$SERVICE_NAME" --region="$REGION" --format="value(status.url)"