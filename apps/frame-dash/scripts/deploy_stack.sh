#!/usr/bin/env bash
# Explicitly invoked only; this script creates/updates AWS resources.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
ENV="${FRAME_ENV:-prod}"
python3 "$ROOT/tools/safety/deploy_targets.py" --env "$ENV"
if [[ "$#" != 1 || "$1" != --apply ]]; then
  echo 'Refused: --apply is required after approval; no automatic deployment.' >&2
  exit 2
fi
if [[ -n "${STACK:-}${AWS_PROFILE:-}${AWS_REGION:-}${AWS_DEFAULT_REGION:-}${SAM_CONFIG_ENV:-}${AWS_CONFIG_FILE:-}${AWS_SHARED_CREDENTIALS_FILE:-}${AWS_ENDPOINT_URL:-}${AWS_ENDPOINT_URL_S3:-}${AWS_ENDPOINT_URL_CLOUDFORMATION:-}${AWS_CA_BUNDLE:-}" ]]; then
  echo 'Refused: unset target overrides; reviewed samconfig is authoritative.' >&2
  exit 2
fi
PROFILE=frame-dash-deploy
if [[ "$ENV" == staging ]]; then PROFILE=frame-dash-staging-deploy; fi
ACCOUNT="$(aws sts get-caller-identity --profile "$PROFILE" --region us-east-1 --query Account --output text)"
[[ "$ACCOUNT" == 084683516815 ]] || { echo 'Refused: wrong AWS account' >&2; exit 2; }
if [[ "$ENV" == staging ]]; then
  aws s3api head-bucket --bucket frame-dash-staging-photos-084683516815 \
    --expected-bucket-owner 084683516815 --region us-east-1 --profile "$PROFILE"
fi
cd "$HERE"
# Build from source, never trust an old .aws-sam template from another target.
sam build --template-file template.yaml --config-env "$ENV"
sam deploy --template-file .aws-sam/build/template.yaml --config-env "$ENV" --confirm-changeset
