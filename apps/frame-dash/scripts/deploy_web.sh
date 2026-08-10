#!/usr/bin/env bash
# Deploy the static SPA: sync web/ to the stack's web bucket and invalidate
# CloudFront so changes show immediately. Reads targets from stack outputs.
set -euo pipefail

STACK="${STACK:-frame-dash}"
REGION="${AWS_REGION:-us-east-1}"
PROFILE="${AWS_PROFILE:-frame-dash-deploy}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

out() {
  aws cloudformation describe-stacks \
    --stack-name "$STACK" --region "$REGION" --profile "$PROFILE" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

BUCKET="$(out WebBucketName)"
DIST_ID="$(out DistributionId)"
URL="$(out DashboardUrl)"

echo "==> Syncing $HERE/web -> s3://$BUCKET"
aws s3 sync "$HERE/web/" "s3://$BUCKET/" --delete --region "$REGION" --profile "$PROFILE"

echo "==> Invalidating CloudFront distribution $DIST_ID"
aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*" \
  --profile "$PROFILE" --query 'Invalidation.Id' --output text

echo "==> Done: $URL"
