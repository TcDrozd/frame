#!/usr/bin/env bash
# Explicit sync --delete and CloudFront invalidation. No implicit production.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
ENV="${FRAME_ENV:-prod}"
python3 "$ROOT/tools/safety/deploy_targets.py" --env "$ENV"
if [[ "$#" != 1 || "$1" != --apply ]]; then
  echo 'Refused: --apply required after approval.' >&2
  exit 2
fi
if [[ -n "${STACK:-}${AWS_PROFILE:-}${AWS_REGION:-}${AWS_DEFAULT_REGION:-}${AWS_CONFIG_FILE:-}${AWS_SHARED_CREDENTIALS_FILE:-}${AWS_ENDPOINT_URL:-}${AWS_ENDPOINT_URL_S3:-}${AWS_ENDPOINT_URL_CLOUDFORMATION:-}${AWS_CA_BUNDLE:-}" ]]; then
  echo 'Refused: unset target overrides; reviewed samconfig is authoritative.' >&2
  exit 2
fi
STACK=frame-dash
PROFILE=frame-dash-deploy
if [[ "$ENV" == staging ]]; then STACK=frame-dash-staging; PROFILE=frame-dash-staging-deploy; fi
# Never upload checked-in production web config to staging. Create and review
# a staging-only web tree outside the repo; this script does not rewrite source.
SOURCE="$HERE/web"
if [[ "$ENV" == staging ]]; then
  SOURCE="${FRAME_STAGING_WEB_DIR:?Set FRAME_STAGING_WEB_DIR to a reviewed staging-only web tree}"
  [[ -d "$SOURCE" && "$SOURCE" != "$HERE/web" ]] || { echo 'Refused: unsafe staging web source' >&2; exit 2; }
fi
TMP="$(mktemp "${TMPDIR:?TMPDIR must point at configured scratch}/frame-stack.XXXXXX")"
trap 'unlink "$TMP"' EXIT
aws cloudformation describe-stacks --stack-name "$STACK" --region us-east-1 --profile "$PROFILE" --output json > "$TMP"
TARGETS="$(python3 "$ROOT/tools/safety/deploy_targets.py" --env "$ENV" --stack-json "$TMP")"
{ IFS= read -r BUCKET; IFS= read -r DIST_ID; IFS= read -r URL; } <<< "$TARGETS"
# Validate local SPA config against this exact stack before uploading.
python3 "$ROOT/tools/safety/check_web_config.py" "$SOURCE" "$TMP"
aws s3 sync "$SOURCE/" "s3://$BUCKET/" --delete --region us-east-1 --profile "$PROFILE"
aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths '/*' --profile "$PROFILE" --query 'Invalidation.Id' --output text
printf 'Done: %s\n' "$URL"
