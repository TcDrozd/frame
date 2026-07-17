# frame-dash

Serverless dashboard for the shared photo frame: browse the S3 photo library,
build **playlists** (named, ordered photo sequences), and publish one as the
`manifest.json` that the display clients poll. The frozen client contract
(schema-1 manifest, see `apps/client/README.md`) is preserved exactly.

## Architecture

```
Browser SPA (S3 + CloudFront, vanilla JS, no build step)
   │  Cognito Hosted UI (code + PKCE) → JWT
   ▼
HTTP API (API Gateway v2, Cognito JWT authorizer, CORS)
   │
   ▼
API Lambda (src/api) ──► DynamoDB (playlists + active pointer)
   │                          ▲
   │ render + presign         │ reuses frozen resolved_start_epoch
   ▼                          │
photo bucket ◄── Scheduled Lambda (src/scheduled, EventBridge rate(2 hours))
trevor-shared-photo-stream/manifest[.dev].json  ◄── clients poll (public read)
```

- **Playlists** live in DynamoDB; the ordered key list *is* the playback order.
- **Publish** renders the playlist into the manifest with presigned GET URLs
  (6h requested expiry) and writes it to the manifest key.
- **Scheduled re-publish** refreshes presigned URLs every 2 hours, reusing the
  `resolved_start_epoch` frozen at the first publish so all frames keep their
  lockstep playback position. Only an explicit publish restarts the show.
- The photo bucket pre-exists and is **not** managed by this stack; the stack
  only gets least-privilege IAM against it.

## Accounts / credentials

Everything deploys to the account that owns the photo bucket
(**084683516815**). `samconfig.toml` pins `profile = "frame-dash-deploy"` — a
dedicated IAM user whose least-privilege policy is versioned at
`deploy-policy.json` (managed policy `frame-dash-deploy` in IAM). It can
manage only `frame-dash-*`-named resources and cannot read photos or touch
other roles. If the policy needs a new permission, edit `deploy-policy.json`
and publish it with an admin profile:

```bash
aws iam create-policy-version \
  --policy-arn arn:aws:iam::084683516815:policy/frame-dash-deploy \
  --policy-document file://deploy-policy.json --set-as-default \
  --profile sunflower-dev --region us-east-1
```

(IAM keeps max 5 versions; delete old ones with `delete-policy-version` if it
fills up.)

## Deploy

```bash
cd apps/frame-dash
sam build && sam deploy            # dev: publishes to manifest.dev.json
./scripts/deploy_web.sh            # sync SPA + CloudFront invalidation
```

After a stack change that alters outputs, refresh `web/js/config.js`:

```bash
aws cloudformation describe-stacks --stack-name frame-dash \
  --profile sunflower-dev --region us-east-1 --query 'Stacks[0].Outputs'
```

## Users

No self-signup. Create users (they get a password-change prompt on first
hosted-UI login unless you set `--permanent`):

```bash
aws cognito-idp admin-create-user --user-pool-id <UserPoolId> \
  --username someone@example.com --profile sunflower-dev --region us-east-1
aws cognito-idp admin-set-user-password --user-pool-id <UserPoolId> \
  --username someone@example.com --password '<password>' --permanent \
  --profile sunflower-dev --region us-east-1
```

## Tests

From the repo root (stdlib unittest; needs boto3 — `apps/frame-dash/.venv` has it):

```bash
apps/frame-dash/.venv/bin/python -m unittest discover -s apps/frame-dash/tests -v
```

## Dev vs prod manifest

The stack parameter `ManifestKey` defaults to **`manifest.dev.json`** so a
deploy can never clobber the live manifest by accident. Point a test client at
it with the client's query param:

```
index.html?manifest=https://trevor-shared-photo-stream.s3.us-east-1.amazonaws.com/manifest.dev.json
```

### Production cutover runbook

1. `sam deploy --config-env prod` — only override is `ManifestKey=manifest.json`.
2. Open the dashboard, publish the chosen playlist, confirm on Status.
3. Watch a real frame pick it up (clients poll hourly; or reload one).
4. Retire whatever previously wrote `manifest.json` (old publisher cron /
   publisher-api), so two writers never fight over the key.
5. Optionally remove `manifest.dev.json` from the bucket policy's public-read
   statement when dev testing is done.

Note: the bucket policy allows public `s3:GetObject` on `manifest.json` and
`manifest.dev.json` only (clients fetch unauthenticated); photo objects stay
private and are only reachable through presigned URLs. The bucket has
`BlockPublicPolicy` enabled, so editing that policy requires temporarily
lifting that flag (see git history / Phase 3 notes).

## Future: uploads

Designed-for but not built: a `POST /api/uploads/presign` route (port
`presign_post` + `build_object_key` from
`apps/portal/app/services/s3_service.py`), one extra `s3:PutObject` statement
on `${PhotosPrefix}*` in `template.yaml` (marked with a comment), an Upload
view in the SPA, and a CORS rule on the photo bucket for the dashboard origin
(browser POSTs go straight to S3).
