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
- **Publish** renders the playlist into the manifest and writes it to the
  manifest key.
- **Photo URLs** come in two flavours, chosen by the `PhotoUrlMode` parameter.
  `presign` (template default) = S3 presigned GETs, 6h requested expiry, but
  really capped by the Lambda role session — hence the 2-hourly refresh.
  `cloudfront` = CloudFront signed URLs from our own RSA key pair
  (`shared/signing.py`, private key in SSM), ~90-day TTLs with no role-session
  cap, so the schedule can drop to `rate(7 days)`. The whole CloudFront side of
  the template (distribution, key group, OAC) is conditional on
  `CloudFrontPublicKeyPem` being non-empty.
- **Scheduled re-publish** refreshes the URLs on `ScheduleExpression`
  (template default every 2 hours), reusing the `resolved_start_epoch` frozen
  at the first publish so all frames keep their lockstep playback position.
  Only an explicit publish restarts the show.
- **Auto-publish** (`AutoPublishMode=window`, default `off`): when no curated
  playlist is active, each scheduled run instead publishes an automatic
  selection — `AutoWindowSize` photos interleaved across top-level folders
  (`shared/auto_select.py`, ported from the v1 publisher), membership rotated
  by a deterministic daily shuffle so URL refreshes within a day never churn
  photos. The auto pointer freezes its own `resolved_start_epoch`, so daily
  rotation doesn't restart playback either. A curated publish always takes
  precedence; "Clear active" in the dashboard hands control back to auto on
  the next run. In this mode the schedule is also the freshness cadence —
  keep it daily-or-better even after the CloudFront cutover.
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
sam build && sam deploy            # writes the LIVE manifest.json - see below
./scripts/deploy_web.sh            # sync SPA + CloudFront invalidation
```

`samconfig.toml` pins only `ManifestKey`, the profile and the region. Every
other stack parameter (`PhotoUrlMode`, `AutoPublishMode`, `AutoWindowSize`,
`ScheduleExpression`, `CloudFrontPublicKeyPem`, `LegacyManifestKey`) is left to
`sam deploy`, which carries unspecified parameters forward from the existing
stack — so **the repo does not record what the live stack is actually
running**. Check before you assume, especially before changing signing or
schedule behaviour:

```bash
aws cloudformation describe-stacks --stack-name frame-dash \
  --profile sunflower-dev --region us-east-1 --query 'Stacks[0].Parameters'
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

## Manifest key: this stack is production

`samconfig.toml` `[default]` pins `ManifestKey=manifest.json`, so a plain
`sam deploy` publishes straight to the manifest the frames poll. The SAM
template's own default is still `manifest.dev.json` — a pre-cutover safety net
that keeps an unconfigured deploy of the bare template off the live key — but
samconfig overrides it. `[prod]` is now an identical alias for `[default]`,
kept only so the old cutover command still works.

To test without touching the live show, either publish with `dry_run` (renders
the manifest and returns it without writing anything), or deploy against the
dev key explicitly and flip back afterwards:

```bash
sam deploy --parameter-overrides ManifestKey=manifest.dev.json
```

Point a test client at the dev key with the client's query param:

```
index.html?manifest=https://trevor-shared-photo-stream.s3.us-east-1.amazonaws.com/manifest.dev.json
```

### Production cutover (done)

The cutover happened in `8091a8d`; the steps are kept here as the record of
what it involved.

1. `ManifestKey=manifest.json`, now pinned in samconfig `[default]` rather
   than reached through a separate config env.
2. Publish the chosen playlist from the dashboard, confirm on Status, and
   watch a real frame pick it up (clients poll hourly; or reload one).
3. Retire every other writer so two can never fight over the key:
   `publisher-api` was deleted, the portal's publish path removed, and the
   08:00Z cron that drove it disabled on the server.
   `tools/publish_manifest.py` survives as a break-glass copy only.

Still outstanding, optional: `manifest.dev.json` can come out of the bucket
policy's public-read statement once dev testing is finished for good.

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
