# Frame Milestone A Safety Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Harden the existing AWS management platform and frame recovery before adding uploads or widening access.

**Architecture:** Preserve schema-1/2 device compatibility and the canonical AWS publisher. Introduce versioned drafts and immutable published snapshots, conditional editing and a recoverable serialized publication protocol. Keep production unchanged until isolated staging and explicit deployment approval.

**Tech Stack:** Python/boto3/unittest, vanilla JS/Node test runner, AWS SAM, DynamoDB/S3/Lambda, Pillow native client.

## Pre-flight and boundaries

- Baseline `7c045ef`; branch `milestone-a/safety-foundation`. Preserve pre-existing untracked `.claude-state` and `apps/.claude-state`.
- No live AWS writes, deploy, publish, device restart, secret reads, push or remote PR creation in this implementation pass.
- Follow repository CLAUDE.md. No old publisher/portal changes beyond necessary documentation.
- TDD: write regression, run and record failure against current behavior, implement minimal fix, rerun focused and full suites. Never fake production results.
- Independent file ownership permits parallel implementation. Do not commit until parent has coordinated review; do not edit another lane's files.
- Gates: pre-flight baseline; revision gate for spec review then code-quality review; escalation for infrastructure changes; abort deployment on any production-target ambiguity.

## Lane 1: Dashboard state and rendering safety

**Files:** `apps/frame-dash/web/**`; new Node tests under `apps/frame-dash/tests/web/`; optional package.json within frame-dash for test invocation.

1. Add failing DOM-level tests for quote-bearing playlist names/epochs and photo filenames: no injected attributes or executable script.
2. Fix attribute rendering with safe DOM assignment or explicitly correct attribute escaping; avoid introducing a framework/build requirement.
3. Add failing test: change name/duration/mode/epoch then reorder/remove; preserve every unsaved edit. Maintain one working draft, dirty-state warning and accessible controls.
4. Remove save side effects from preview and from cancelled Publish. Coordinate payload contract with backend lane; do not work around backend publication semantics with UI-only fixes.
5. Add tests for accumulated folder pagination and route/error behavior where safely scoped.
6. Run Node tests against actual source modules with mocked network/DOM, label fixtures explicitly. No production token use. Record RED/GREEN commands.

## Lane 2: Last-good playback state

**Files:** `apps/client/app.js`, `apps/pi-zero-client/**`, root `tests/test_frame*`, new browser cache tests under root `tests/`.

1. Add failing browser-cache regression: replacement downloads fail; old manifest/cache remain playable and same-manifest retry is not suppressed.
2. Stage cache/manifest promotion, prune only after full usable acquisition, preserve authoritative indices for sync mode and last-good state on failure.
3. Add Pi failing tests: total/partial replacement failure preserves old local manifest; restart can resolve old playable photos; interrupted manifest write cannot replace valid state.
4. Atomic manifest promotion, defensive viewer parsing, explicit missing-asset retries. Handle same-key changed ETag without breaking key-derived naming/backwards compatibility.
5. Retain manifest schema/order/epoch contract and avoid new runtime dependencies. Run full root suite and new browser tests.

## Lane 3: API, drafts, concurrency and publishing

**Files:** `apps/frame-dash/src/**`, Python tests in `apps/frame-dash/tests/` excluding tests/web; no template edits without parent coordination.

1. Add handler-level failing tests for malformed JSON/types/settings/name/key/expiry. Strict 4xx validation; preserve exact S3 keys and choose explicit duplicate rejection. Bound payload serialized size as well as count.
2. Version playlist edits. Conditional writes and 409 on stale updates; update tests/FakeTable to enforce conditions, including concurrent delete/reorder/append. Document compatibility policy for legacy unversioned clients; fail closed for unsafe edits rather than silently defeating conflict control.
3. Persist immutable published snapshot/revision distinct from draft. Scheduler uses snapshot only; saving/previewing active draft never changes publication. Support non-mutating preview of submitted draft payload. Publish must bind the reviewed revision and intentionally preserve or restart timing.
4. Replace ambiguous activation metadata reuse; validate selections; returning to automatic must have explicit timing and preserve frozen snapshot until next successful publication.
5. Design and implement serialized, recoverable publication using a shared cross-function ownership mechanism with fencing/conditional checks or a single worker. Do not claim ordinary expiring lock + unconditional S3 PUT prevents stale overwrite. If safe protocol requires template/worker changes, document exact contract and ask parent to integrate rather than weakening guarantees.
6. Test partial failure at each S3/pointer write, stale scheduler versus explicit publish, duplicate retry, draft edit while refreshing and initial migration. No live migration. Preserve schema and stable refresh epoch.
7. Run full dashboard Python suite. Provide migration/cutover notes and remaining infrastructure prerequisites honestly.

## Lane 4: Staging and automated safeguards

**Files:** `apps/frame-dash/template.yaml`, sam config/deploy scripts, new `tools/` safety helpers and CI workflows, staging/deployment docs; no src/web/consumer edits.

1. Add genuinely isolated staging configuration: independent stack/table, nonproduction photo storage and manifest key, schedule isolation, independent Cognito domain. Resolve globally fixed bucket/resource names and hard-coded production origin/config generation where necessary through coordination.
2. Add fail-closed preflight: ordinary staging actions refuse production bucket/key/table/legacy key; production deploy requires explicit intent. Do not repoint the live stack to a dev key.
3. Add local automated tests for configuration validation and production-target rejection. Validate SAM template locally; no cloud deploy.
4. Add CI for Python/Node tests and static/template validation, with no secret-bearing write/deploy steps. Coordinate exact Node commands after lane 1/2.
5. Document AWS resource creation approval gate, staging end-to-end acceptance, recoverability and production rollback. CSP changes may be added here but coordinate actual JS compatibility; do not assume CSP replaces escaping fixes.

## Parent integration and reviews

1. Read implementations and independently rerun tests; compare behavior to requirements.
2. Spec compliance review first. Resolve gaps, then independent quality/security review. Final integration review covers complete cross-lane contract.
3. Verify no git whitespace errors; all root/dashboard/Node tests pass and SAM local validation succeeds or report exact tooling blocker.
4. Update commands/docs for revised APIs, migration and safety gates. Record implemented versus pending; Milestone A is not complete until staging acceptance is actually exercised after approval.
5. Commit only explicit scoped files once reviews pass. Do not push/deploy without authorization.

## Acceptance criteria

- Malicious names/filenames remain inert; item operations preserve working form edits.
- Save/preview/cancel cannot change published show; refresh uses immutable published revision.
- Stale edits and jobs cannot silently overwrite newer intent; failures are recoverable and status truthful.
- Failed acquisition and reboot preserve a playable last-good show on both consumers.
- Staging cannot touch production resources; automatic checks cover adversarial paths.
- No claim of live staging deployment, hardware verification or Milestone A completion without corresponding execution evidence.
