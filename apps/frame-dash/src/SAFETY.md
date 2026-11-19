# Backend safety contract and cutover prerequisites

This implementation is local only. It does not migrate data, deploy resources,
or establish that any production/staging IAM policy or bucket configuration is
correct. Exercise the protocol against isolated staging before cutover.

## API contracts for the UI lane

- New drafts start at integer `revision: 1`. Legacy unversioned drafts read as
  `revision: 0` without a write. The first conditional edit upgrades them to 1.
- `PUT /api/playlists/{id}` requires `expected_revision` and accepts partial
  `name`, `settings`, and full replacement `items`. `DELETE` requires JSON body
  `{ "expected_revision": <integer> }`. Conflicts return 409; missing or wrongly
  typed revisions return 400. Old unversioned mutation clients fail closed.
- Settings are `{mode: "sync"|"inventory", slide_seconds: <integer 1..86400>,
  start_epoch: "now"|<integer 0..253402300799>}`. **Numeric epoch strings are not
  accepted: form input must be converted to an integer by the UI.** No coercion
  of booleans, numeric strings, fractional durations, nulls or containers.
- Names are nonblank strings up to 256 UTF-8 bytes. Keys remain byte-for-byte
  equivalent strings: no stripping, slash normalization or URL decoding.
  They must be under PHOTOS_PREFIX, not folder markers, at most 1024 UTF-8 bytes,
  and have no control characters. Duplicates are rejected. Draft count is capped
  at 1000 and serialized size at 120000 bytes; request bodies at 150000 bytes.
- `POST /api/playlists/{id}/publish` preview accepts
  `{dry_run:true, draft:{name,settings,items}}`. Omit draft to preview saved data.
  Preview has **zero DynamoDB or S3 writes**, including no recovery writes.
- Actual publish accepts `{expected_revision:<integer>, restart:true,
  request_id:<optional 32 lowercase hex chars>}`. Save first; actual publish
  never accepts a submitted draft. Revision binding is checked atomically with
  reservation, not just in the API's earlier read. Explicit publish intentionally
  resolves `now` again; an explicit numeric epoch remains that numeric epoch.
- A stable `request_id` makes retries idempotent, including after newer publishes
  or edits. Generate it once per confirmed publish and retain it across ambiguous
  network failures. Without one, each request is a deliberately new publish.
  Never reuse an ID for different intent. Durable receipts are not TTL'd.
- Non-null `PUT /api/active` returns 409: activation must go through reviewed
  publish, not switch a mutable draft into scheduler input. Null hands selection
  to automatic mode with fresh timing on its next successful publish. Previous
  snapshot/epoch/version remain separately recorded until that publication.
- Deleting or saving a draft never deletes/changes its published snapshot.
  Refreshes use snapshot data and its frozen resolved epoch, not the draft.
- Status exposes `pending_publication` and observed manifest `publication_id`;
  when pending, the canonical S3 bytes may already be new while the committed
  DynamoDB pointer still describes the previous generation. Do not label pending
  as a successfully completed publication.
- Status also exposes `manifest_observation`: `{state:"present"}`,
  `{state:"missing"}` (genuine 404 only), or `{state:"error", code}` where code
  is one of `access_denied`, `throttled`, `unavailable`, `dependency_error`.
  Raw AWS messages are never returned. A permission failure or outage must not
  be rendered as "not found". `manifest_head` is null unless present.
- 409 means stale state only: a plain conditional failure, or a transaction
  cancelled solely by `ConditionalCheckFailed` reasons. Throughput, contention
  (`TransactionConflict`), validation and server cancellations are dependency
  errors (500), never "reload and retry".
- Scheduler recovery finishes a pending reservation with its exact staged bytes,
  then reloads authoritative state and publishes a fresh fenced generation in
  the same invocation, so an outage longer than the presigned URL lifetime does
  not leave expired URLs live. The result reports `recovered`,
  `recovered_version` and `refreshed`; if the fresh follow-up fails, the run
  raises explicitly instead of reporting success.

## Publication protocol and safety argument

1. Render exact reviewed data; reject an empty usable result. Stage immutable
   bytes under `${ManifestKey}.publications/<random-id>.json` using IfNoneMatch.
   Manifest `schema` and photo fields are unchanged; version gains a unique
   request-ID suffix to prevent ETag ABA from same-second identical renders.
2. A DynamoDB transaction reserves CONFIG/active against the exact state token,
   requires no pending reservation and no prior receipt, and checks the reviewed
   draft revision. Failure leaves only an inert orphan staged object.
3. Canonical PUT uses `IfMatch` on the recorded prior ETag, or `IfNoneMatch: *`
   for an absent object. Canonical object metadata records `publication-id`.
4. A transaction conditionally replaces the pending pointer with the immutable
   snapshot and stores a permanent receipt. Publication success is reported only
   after this commit (or a receipt proving it already committed).

Reservations do not expire or transfer. Any caller may finish the **same**
reserved bytes; scheduler recovery takes precedence over generating a new job.
After S3 success/pointer failure, recovery observes publication-id and finishes
DynamoDB without rerendering/restarting/rewriting S3. A paused older writer holds
only the old ETag; after a later generation commits, its conditional PUT cannot
replace that generation. Its conditional pointer completion cannot replace a
newer pointer either. Tests interleave a paused writer, another finisher and a
newer publish, for both absent-object and existing-ETag initial states.

The safety argument assumes all writes use this protocol, staging bytes and
receipts are retained, and canonical objects are not deleted/restored out of
band. IAM/bucket controls and operational cutover must enforce those assumptions.
There is no ordinary expiring lock and no unconditional canonical PUT fallback.

## Exact parent infrastructure/integration work (not performed here)

- Both API and scheduled roles need s3:GetObject/s3:PutObject on **both**
  `${ManifestKey}` and `${ManifestKey}.publications/*`, in the configured bucket.
  Keep staged documents private: they contain signed photo URLs. No DeleteObject
  grant is needed. Protect canonical/staging keys against out-of-band overwrites,
  deletion, lifecycle expiry and version restore while referenced by pending state.
- DynamoDB role permissions must cover transactional Put and ConditionCheck on
  the existing playlist table (underlying PutItem/ConditionCheckItem actions),
  in addition to existing CRUD/read/query permissions. Publication receipts use
  `pk=PUBLICATION`; no new table or index is required. Do not TTL these receipts.
- Missing canonical-object HEAD must produce a genuine 404, not an ambiguous
  AccessDenied. S3 can return 403 for absent keys without suitable ListBucket
  permission. Provision appropriate read/list visibility or bootstrap the
  canonical staging key deliberately. Never interpret a generic 403 as absence.
- Bundle the SDK from src/requirements.txt: tested local boto3 1.43.26 supports
  both conditional PutObject fields. Do not depend on unknown runtime SDKs.
- Set LEGACY_MANIFEST_KEY empty or equal to canonical. Distinct dual-key publish
  is deliberately rejected: two independently polled keys cannot be atomically
  switched by this protocol. Resolve any consumers of the legacy key before
  enabling writes; do not silently fall back to unsafe mirroring.
- Quiesce/drain **all** old API/scheduled/CLI writers before enabling this code;
  deny unconditional canonical writes where feasible via bucket policy. The old
  and new writers cannot safely coexist. No reserved-concurrency-one worker is
  required for this CAS protocol, but old writers invalidate its guarantees.
- Legacy active pointers lacking an immutable snapshot are not inferred from a
  current draft. Scheduler returns a migration-required no-op. An approved,
  reviewed explicit publish (or explicit clear for auto selection) is required
  before their URLs expire. No live migration was attempted.
- Staging must exercise concurrent API/scheduler runs, IAM, actual S3 412/409
  behavior, failure recovery, legacy cutover and consumer compatibility. Unit
  fakes and SDK serialization tests are not a substitute for that acceptance.

## Local verification

Run from repo root:

```
apps/frame-dash/.venv/bin/python -m unittest discover -s apps/frame-dash/tests -v
```

Initial added safety regressions were RED (missing revision, coercion accepted,
non-string names returning 500). Additional repeated-clear regression was RED
before preserving the prior frozen show across repeated clears. The suite covers
strict handler 4xx behavior, preview zero writes, conditional edits/deletes,
concurrent append/reorder/delete, draft edits during refresh, stale scheduler
render races, immutable snapshots, initial legacy handling, all publication
write boundaries, lost acknowledgements, permanent request receipts and stale
S3 writers. AWS operations are faked/stubbed; no cloud API calls are made.
