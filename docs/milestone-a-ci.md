# Offline GitHub Actions gates (phase 1)

Workflow: [`.github/workflows/offline-safety.yml`](../.github/workflows/offline-safety.yml).
It runs on pull requests, `main` pushes, and manual dispatch. It does **not** deploy,
provision, publish manifests, create GitHub environments, change branch protection,
or configure GitHub/AWS settings. It needs no secrets, OIDC permission, or valid
AWS credentials. Installing dependencies and SAM build wheels needs package-network
access; "offline" means no live AWS/application calls. Python test sockets are
restricted to loopback; SDK tests use Stubber/mocks and dummy identifiers.

## Reviewable required-check candidates

- **Python 3.12 / root offline regressions** — all root Python tests including
  cache/sync/S3 mapping (and the browser test wrapper). Node is pinned here too.
- **Python 3.12 / dashboard offline regressions** — dashboard API, publication,
  store, scheduled refresh, renderer and auto-selection tests. Separate discovery
  avoids module-name collisions with root suites.
- **Node 24 LTS / actual-source browser and dashboard regressions** — executes
  `tests/test_browser_cache.js` against the real browser client script, and
  `apps/frame-dash/tests/web/*.test.js` through the UI lane's package script.
  `npm ci --ignore-scripts` honors the lockfile. Missing paths fail, not skip.
- **Python 3.12 / native Chromium offline acceptance** — a fifth, independent
  gate running `tests/browser-native/run.py` with Playwright **1.53.0** and its
  bundled Chromium on Ubuntu 24.04. A separate provisioning step runs
  `python -m pip install playwright==1.53.0` and
  `python -m playwright install --with-deps chromium` with package-network access.
  The actual acceptance run serves generated fixtures on loopback only: Python
  nonloopback sockets are rejected, browser request routing denies external URLs,
  and Chromium uses a dead loopback proxy with loopback bypass and restricted DNS.
  AWS identifiers are cleared and credential/config files point to `/dev/null`.
  Native IndexedDB rollback/reload, failed acquisition, corrupt-image decoding
  (including the Image-element fallback), retry/pruning, and external-request
  blocking are exercised against production client functions, not an IndexedDB
  mock. Startup invocation is excluded by the harness; this is not full kiosk E2E.
  Missing harness/bridge/browser/dependency or zero/skipped tests fail; an additional
  evidence-summary check requires nonzero execution and no skips/errors/failures.
  Evidence is written under `$RUNNER_TEMP/frame-browser-native`. On failure only,
  the workflow uploads **only** `evidence.json` and `test_*.zip` Playwright traces,
  containing generated fixture images/state and source snapshots, never real photos,
  AWS data or credentials. Hidden files are excluded and retention is **3 days**.
  `actions/upload-artifact` v4.6.2 is pinned to upstream commit
  `ea165f8d65b6e75b540449e92b4886f43607fa02` (resolved read-only from its tag).
- **Deployment guards / SAM lint and ARM64 build** — fail-closed target/script
  tests, shell parsing, whitespace check, CloudFormation/SAM lint, Python 3.12
  ARM64 packaging. Uses an empty scratch SAM config so a runner does not need
  the production-named AWS profile just to validate/build.

The unittest runner rejects zero tests and any skipped tests. No workflow-level
path filters can silently omit a required check. Job timeouts are bounded;
superseded runs cancel. Workflow token permissions are `contents: read` and
checkout does not retain credentials. There is no `pull_request_target` and no
execution under a privileged PR context. Runtime parity is Python 3.12; Python
3.14 may be added as an optional compatibility matrix only after these baseline
gates are stable. Node is pinned to `24.11.1` (LTS line), not the runner default.

SAM lint provides template/static-infrastructure checks, **not** a complete
security audit. This phase does not claim dependency-vulnerability scans, IAM
policy simulation, Lambda emulation, full browser E2E, Cognito auth verification,
or cloud acceptance. Package/action version updates and a full security scanner
baseline should be reviewed separately rather than masking existing findings.

## Discuss before enabling enforcement

1. Review the proposed check names and trigger/cancellation behavior; observe a
   real GitHub run after an explicitly approved push/PR. Local output is not a
   remote Actions result.
2. Decide branch-protection/review rules with the owner. No settings are changed
   by this work. Prefer making these checks required after a successful run.
3. Decide dependency/action update ownership. All workflow actions are pinned to
   immutable commit SHAs resolved from the upstream major tags; review updates
   explicitly rather than reverting to floating action references.
4. Evaluate OIDC with repository/ref/environment trust restrictions. Any later
   staging deployment job must use a protected GitHub **staging environment**, an
   environment-approved short-lived role restricted to staging, and the validated
   target contract. Never execute untrusted PR code with cloud permissions.
5. Only after [staging prerequisites and acceptance](milestone-a-staging.md), add
   an explicitly approved staging-deploy/smoke-test workflow. Production remains
   a separate manually approved environment/intent gate with backups and rollback
   evidence; no automatic deployment on merge is proposed here.

Local verification and cloud limitations are documented in the staging runbook.
No claim is made that this workflow has run on GitHub yet. The native gate's
Ubuntu bundled-Chromium acceptance remains unverified until an approved remote
run; local macOS Chrome results are not Linux CI evidence. Workflow regression
coverage checks its install/run separation, pinned versions, fail-closed summary,
credential isolation and narrow failure-artifact allowlist.

## Local verification record (2026-11-14)

Last full local run of every gate, from the repository root on macOS, before
the first commit of this branch. This is **local evidence, not a GitHub
Actions run**; no remote run has happened yet.

| Gate | Command | Result |
| --- | --- | --- |
| Root Python 3.12 | `python tools/safety/run_unittests.py tests` | 75 OK, 0 skipped |
| Dashboard Python 3.12 | `python tools/safety/run_unittests.py apps/frame-dash/tests` | 101 OK |
| Deployment/workflow guards | `python tools/safety/run_unittests.py tools/safety/tests` | 19 OK |
| Dashboard DOM (Node 24) | `npm run test:web --prefix apps/frame-dash` | 30 pass |
| Browser cache (Node 24) | `node --test tests/test_browser_cache.js` | 23 pass |
| Native acquisition | `python tools/safety/run_unittests.py tests/browser-native` | 1 OK |
| Native harness | `python tests/browser-native/run.py` | exactly 5 OK |
| Staging target | `python tools/safety/deploy_targets.py --env staging` | validated |
| Whitespace | whole-tree `git diff --check` (excluding `*.md`, `legacy/`) | clean |
| SAM | `sam validate --lint` + Python 3.12 ARM64 `sam build` | valid / succeeded |

Not yet verified: native Chromium on GitHub's `ubuntu-24.04` runner (local runs
used installed Google Chrome 157 on macOS); `actionlint` (not installed locally);
any live AWS behaviour. Treat the first remote run as the real check of the
workflow file itself.
