# Milestone A: isolated staging and deployment approvals

**Status:** infrastructure is locally validated, not provisioned. No AWS API call,
resource creation, deploy, or production write was performed for this change.
Cloud acceptance remains pending. Never use a dev manifest key on `frame-dash`
as a substitute for staging.

## Targets and invariants

| Boundary | Production (unchanged) | Staging (not yet created) |
|---|---|---|
| Stack | `frame-dash` | `frame-dash-staging` |
| Playlist table | `frame-dash-playlists` | `frame-dash-staging-playlists` |
| Photo bucket (pre-existing, not managed by SAM) | `trevor-shared-photo-stream` | `frame-dash-staging-photos-084683516815` |
| Manifest key | `manifest.json` | `staging/manifest.json` |
| Legacy key | Persisted production value | Empty; no dual write |
| Cognito domain prefix | `frame-dash-tcd` | `frame-dash-staging-084683516815` |
| Web bucket | `frame-dash-web-084683516815` | `frame-dash-staging-web-084683516815` |
| Schedule | Persisted production cadence (audit: every 2h) | Separate schedule, `DISABLED` |
| Auto selection | Persisted production mode/window (audit: window/50) | `off`/50 |
| Signing/SSM | Persisted production parameters | Presign only; no signing public key; separate unused SSM path |
| Deploy profile | `frame-dash-deploy` | `frame-dash-staging-deploy` (prerequisite) |

Names for SAM-owned buckets/tables/functions/schedules derive from the stack.
The explicitly pre-created staging photo bucket also incorporates its stack name.
Staging Lambda policies reference only its photo bucket/table, never the production
photo bucket. The template's staging Rules independently reject production bucket,
manifest, legacy, domain, signing and scheduling settings. The offline validator
requires the *complete exact* staging parameter set and the expected stack/profile/
region; missing parameters cannot silently inherit production/default values.

`default` and `prod` remain production aliases. Their sole override remains
`ManifestKey=manifest.json`; **other production parameters carry forward**. Adding
`Environment` and `ScheduleState` uses production/ENABLED defaults for the existing
stack, not a staging cutover. Before any future production update, read the live
parameters and inspect the change set; confirm the expected currently-enabled
schedule separately. A paused production schedule must not be inadvertently enabled.

## Offline checks (safe now)

Run from the repository root with Python 3.12 and Node 24 LTS. Use a scratch
virtualenv with `boto3` and `apps/frame-dash/src/requirements.txt` installed if needed.
No real credentials are needed. The unittest runner blocks non-loopback socket
connections and refuses zero discovered or skipped tests.

```bash
python3 tools/safety/run_unittests.py tools/safety/tests
python3 tools/safety/run_unittests.py tests
python3 tools/safety/run_unittests.py apps/frame-dash/tests
python3 tools/safety/deploy_targets.py --env staging
npm ci --ignore-scripts --prefix apps/frame-dash
npm run test:web --prefix apps/frame-dash
node --test tests/test_browser_cache.js
for script in apps/frame-dash/scripts/*.sh tools/deploy/*.sh; do bash -n "$script"; done
# Avoid loading a local AWS profile at all during SAM checks.
printf 'version = 0.1\n' > "$TMPDIR/frame-offline-sam.toml"
AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null \
  AWS_EC2_METADATA_DISABLED=true SAM_CLI_TELEMETRY=0 \
  sam validate --lint --template-file apps/frame-dash/template.yaml \
  --config-file "$TMPDIR/frame-offline-sam.toml" --region us-east-1
AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null \
  AWS_EC2_METADATA_DISABLED=true SAM_CLI_TELEMETRY=0 \
  sam build --template-file apps/frame-dash/template.yaml \
  --config-file "$TMPDIR/frame-offline-sam.toml" \
  --build-dir "$TMPDIR/frame-safety-build" --no-cached
```

SAM resolves Python 3.12 ARM64 artifacts; a successful build is not an actual
Lambda invocation. Do not deploy a hand-built macOS native dependency tree.

Publication IAM integration local evidence: Python 3.12 safety runner **17 tests
passed at that stage; current counts are recorded in `docs/milestone-a-ci.md`** (including six publication IAM/CDN contracts), exact staging target
validation passed, `sam validate --lint` passed, and credential-free SAM 1.161.0
Python 3.12 ARM64 build succeeded. The built artifact bundled boto3 **1.44.3**;
its S3 PutObject model includes both `IfMatch` and `IfNoneMatch`. `git diff --check`
passed. Artifacts were moved into Hermes scratch; no deploy/cloud invocation ran.
These results do not prove real role authorization or Linux Lambda execution.

## Publication IAM and cutover boundary

Both Lambda roles now have `s3:GetObject`/`s3:PutObject` on the configured
canonical `${ManifestKey}` and `${ManifestKey}.publications/*`. There is no
`DeleteObject`, ACL-write, or photo-prefix write grant. The existing conditional
legacy-key grant is preserved for production parameter compatibility, **not** as
permission to enable dual-key publishing: the new backend rejects a distinct
`LegacyManifestKey`. Staging Rules require it empty.

Both roles retain `DynamoDBCrudPolicy` for the stack-local playlist table and an
explicit table-ARN-only `PutItem`/`ConditionCheckItem` publication statement.
Local expansion using installed SAM CLI **1.161.0**'s policy-template processor
confirmed its `DynamoDBCrudPolicy` already includes both actions, plus GetItem,
DeleteItem, Scan, Query, UpdateItem, BatchWriteItem, BatchGetItem and DescribeTable
(on the table and its indexes). The explicit grant makes the protocol dependency
visible without relying on that template's future contents. DynamoDB transactions
use underlying item actions; no invented `dynamodb:TransactWriteItems` grant is
needed. Receipts remain in the same table and must not have TTL/cleanup applied.

### Missing canonical HEAD versus access denial

`ListPhotos` stays restricted to `${PhotosPrefix}*`; a separate `ListBucket`
statement allows **only exact `${ManifestKey}` prefix listing**, not bucket-wide
enumeration or a wildcard manifest prefix. This is not proof of missing-object
HEAD visibility: `HeadObject` does not submit a listing prefix, and a scoped
`s3:prefix` condition may not authorize S3's missing-key disclosure. Do not loosen
this to unconditional ListBucket or `StringEqualsIfExists` merely to turn a 403
into a 404; that can broaden enumeration.

The backend only treats genuine 404/NoSuchKey/NotFound as absence; AccessDenied
remains an error with zero publication writes at that boundary. **An empty bucket
is therefore not yet a guaranteed first-publish path under these scoped grants.**
Before first publication, an approved operator must either:

- Deliberately bootstrap a valid canonical manifest in the isolated bucket,
  verify each Lambda role can HEAD/read it, then exercise conditional replacement;
  never overwrite an existing canonical object as bootstrap. Agree its consumer
  contents and migration semantics before writing, and record that bootstrap is
  a separate approved S3 write, not part of these offline changes; or
- Prove actual role-based absent HEAD returns 404 in isolated staging, or approve
  a separately reviewed backend exact-key list probe that distinguishes absent
  objects from denied HEAD without interpreting generic 403 as absence.

A scoped listing grant alone must not be reported as a verified solution for
absent HEAD. No bucket-wide ListBucket grant was added. Also test missing-photo
behavior; the same HEAD/list distinction can affect renderer skip-missing logic.

### Remaining production cutover assumptions (not implemented or verified here)

- Inventory and quiesce/drain **all** old API, scheduled, CLI and other writers
  before new publication starts. Old unconditional writers cannot safely coexist
  with the CAS protocol. Preserve a paused schedule during the change-set review.
- Clear `LegacyManifestKey` (or make it identical to canonical) only after resolving
  every consumer of the old key. No atomic two-key mirror is implemented.
- The photo bucket is pre-existing and **no photo bucket policy is managed by
  this template**. There is no implemented bucket-policy fence preventing all
  out-of-band writers, deletion or version restore. Review/approve that boundary
  separately, including administrators, legacy roles, lifecycle expiry, replication
  and recovery tooling. Conditional-write enforcement must account for staging
  writes and approved bootstrap; do not assume Lambda grants deny other identities.
- Keep `${ManifestKey}.publications/*` private: immutable snapshots contain signed
  URLs. The template now puts a signed-only `*.publications/*` CloudFront behavior
  before unsigned `m/*`, so its CDN does not expose staged bytes anonymously.
  Existing direct-S3 public/bucket policies and other distributions still require
  audit: no photo bucket policy is added here. Staging's public-access block remains
  a provisioning prerequisite, not a locally verified cloud fact.
- Retain referenced staged bytes and durable receipts; no lifecycle/GC/TTL or
  out-of-band staging mutation may invalidate pending recovery. Versioning alone
  does not prevent deletion/restore or unconditional overwrites.
- Bundle `src/requirements.txt`'s conditional-PUT-capable SDK; verify the actual
  Linux ARM64 artifact, not the ambient Lambda SDK. Legacy active state lacking an
  immutable snapshot requires an approved reviewed publish or explicit clear;
  scheduler migration-required no-op does not refresh its expiring URLs.
- Obtain separate cloud approval and exercise real role IAM (including denied
  production access), canonical present/absent authorization, 412/409 CAS races,
  concurrent API/scheduler recovery after S3 success/DDB failure, lost replies,
  legacy migration and consumer compatibility. Offline tests/lint/build do not
  establish any of these live properties.

## Approval 1: staging prerequisites (not executed)

This approval permits **creating billable AWS resources**, writing a new bucket's
configuration, and subsequently creating roles/functions/auth/CDN/database objects.
The later deployment identity necessarily has write/delete and IAM PassRole
capabilities within its reviewed scope. A read-mostly alternative is to stop after
the offline checks and have an operator review/provision these prerequisites.

1. Approve the exact account `084683516815`, region `us-east-1`, resource names,
   cost, fixture data, and least-privilege identity. Do not reuse production
   credentials when a staging permission fails. The existing production
   `deploy-policy.json` is **not** a staging policy: its stack/web permissions
   target production and some service permissions are broad. Do not attach it
   unchanged or just broaden it. IAM policy/trust review and provisioning are a
   separate approval; never inspect/export secret credentials.
2. Create a dedicated `frame-dash-staging-deploy` identity/profile with permissions
   for the staging stack, SAM artifact storage, staging web bucket, staging
   resource roles, the staging photo bucket preflight, and required creation APIs.
   Restrict IAM PassRole to staging-created service roles; forbid production S3,
   DynamoDB, IAM role and SSM access (including an explicit production-resource
   deny/permissions boundary). Review service creation APIs that require `*`.
   Use the same production-resource deny boundary for staging execution roles if
   organization policy requires defense against later policy expansion. Do not
   create signing secrets; presign staging needs none. OIDC is preferred for a
   later CI deployment identity, but is not enabled in this milestone.
3. An approved operator checks caller account and bucket availability. A 403 is
   **not** evidence that a bucket is absent; stop on ambiguous ownership/errors.

Read-only prerequisite commands, only after permission to call AWS:

```bash
aws sts get-caller-identity --profile sunflower-dev --region us-east-1
aws s3api head-bucket --bucket frame-dash-staging-photos-084683516815 \
  --expected-bucket-owner 084683516815 --profile sunflower-dev --region us-east-1
```

Only after confirming the bucket is absent and approving its creation, execute
these exact commands **individually, checking each result**. `us-east-1` uses no
LocationConstraint. If the global name is unavailable, stop and review a consistent
config/template/validator change; never substitute a production bucket.

```bash
aws s3api create-bucket --bucket frame-dash-staging-photos-084683516815 \
  --object-ownership BucketOwnerEnforced --profile sunflower-dev --region us-east-1
aws s3api put-public-access-block --bucket frame-dash-staging-photos-084683516815 \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true \
  --expected-bucket-owner 084683516815 --profile sunflower-dev --region us-east-1
aws s3api put-bucket-encryption --bucket frame-dash-staging-photos-084683516815 \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' \
  --expected-bucket-owner 084683516815 --profile sunflower-dev --region us-east-1
aws s3api put-bucket-versioning --bucket frame-dash-staging-photos-084683516815 \
  --versioning-configuration Status=Enabled --expected-bucket-owner 084683516815 \
  --profile sunflower-dev --region us-east-1
aws s3api get-public-access-block --bucket frame-dash-staging-photos-084683516815 \
  --expected-bucket-owner 084683516815 --profile sunflower-dev --region us-east-1
aws s3api get-bucket-encryption --bucket frame-dash-staging-photos-084683516815 \
  --expected-bucket-owner 084683516815 --profile sunflower-dev --region us-east-1
aws s3api get-bucket-versioning --bucket frame-dash-staging-photos-084683516815 \
  --expected-bucket-owner 084683516815 --profile sunflower-dev --region us-east-1
```

Keep this bucket private. No public bucket policy, no production bucket replication,
no `sync` from production, no real family photos copied without separate approval.
Use a small operator-approved synthetic/nonpersonal image as the only initial
fixture (`photos/fixture.jpg`); fixture upload is another approved write. There
is no staging bucket teardown/emptying command here. Versioned objects cost money
and require a separately approved retention/deletion plan.

## Approval 2: deploy isolated staging (not executed)

Unset `STACK`, `AWS_PROFILE`, `AWS_REGION`, `AWS_DEFAULT_REGION`, and
`SAM_CONFIG_ENV`; wrappers reject ambiguous overrides. Run from the repo root:

```bash
FRAME_ENV=staging bash apps/frame-dash/scripts/deploy_stack.sh --apply
```

The wrapper validates offline before AWS calls, checks caller account and bucket
ownership, builds from source, and asks for SAM change-set confirmation. Review
all changes; only `frame-dash-staging` may be affected. Stop if production resources
appear. It never rewrites `samconfig` or production parameters.

For static hosting, do **not** upload the checked-in production `web/js/config.js`
to staging. Copy the web tree into a scratch staging-only tree, set its four CONFIG
literal values (`apiUrl`, `cognitoDomain`, `clientId`, `manifestUrl`) from that exact
staging stack's outputs, review it, then explicitly approve:

```bash
FRAME_ENV=staging FRAME_STAGING_WEB_DIR="$TMPDIR/frame-staging-web" \
  bash apps/frame-dash/scripts/deploy_web.sh --apply
```

The web wrapper validates actual stack ARN/account/parameters/bucket/output shapes
and all four SPA config values before `s3 sync --delete` or invalidation. No source
files are rewritten. The staging manifest remains private; use an authenticated
S3 read or a deliberately short-lived presigned manifest URL for a smoke test.
Never point any real production frame at staging.

## Production and server-runtime gates

After a *separate production approval*, review live state, snapshot parameters/
manifest/control-plane records using the existing backup workflow, and inspect the
change set before confirming. These commands are **not** staging setup commands:

```bash
FRAME_ENV=prod FRAME_PRODUCTION_INTENT=frame-dash \
  bash apps/frame-dash/scripts/deploy_stack.sh --apply
FRAME_ENV=prod FRAME_PRODUCTION_INTENT=frame-dash \
  bash apps/frame-dash/scripts/deploy_web.sh --apply
# Separate server-runtime approval: rsync --delete, sudo, VERSION append.
FRAME_PRODUCTION_INTENT=frame-runtime bash tools/deploy/deploy_client.sh --apply
FRAME_PRODUCTION_INTENT=frame-runtime bash tools/deploy/deploy_portal.sh --apply
```

A bare wrapper invocation fails before AWS/sudo/rsync. The raw SAM/AWS CLIs can
bypass shell gates; these are operator safety rails, **not an IAM security boundary**.
Do not use bare `sam deploy`, `sam sync`, or the obsolete CLAUDE.md suggestion to
repoint the existing production stack at `manifest.dev.json` for staging.

## Pending cloud acceptance (required before calling this deployed)

- Verify ownership, bucket public-access block/encryption/versioning, scoped IAM
  including an actual denied production-photo read using the staging identity.
- Confirm staging stack outputs, separate table/pool/domain/web bucket and Lambda
  runtime IAM. Read back all staging parameters and the schedule's `DISABLED` state.
- Create a staging-only Cognito user through the approved secure admin workflow;
  verify login/logout/PKCE, unauthorized API denial, browsing, playlists, dry-run,
  one explicit fixture publish, and S3 manifest contents. Ensure no production
  bucket ARN appears in staging resource policies or Lambda environment.
- Verify CloudFront has deployed, staging SPA uses staging endpoints, and check
  production manifest/control-plane versions have not changed from the approved
  preflight snapshot. Do not publish to production as a smoke test.
- Record the evidence and outstanding permissions, not just deployment exit codes.

## CSP / response-header decision

No enforcing CSP is added in this milestone. Existing buildless ES modules,
inline style/handler behavior, cross-origin API/photo URLs and Cognito redirects
need a UI-owned inventory and real login/upload/browser regression coverage.
A blanket `default-src 'self'` would break that behavior; `unsafe-inline` would
hide the hardening gap. Next step is a staging-only **report-only** response-header
policy with deliberate `connect-src`/`img-src`/`form-action` and no bearer-token
logging, then tighten after browser evidence. Any custom CloudFront response
headers policy also needs a reviewed deploy-IAM permission addition; the existing
policy does not grant its management APIs. Do not broaden IAM silently.
