# Offline native-browser cache acceptance

Run from the repository root:

```sh
python3 tests/browser-native/run.py
# Optional: select an already-installed Chromium binary and scratch artifacts.
FRAME_BROWSER_EXECUTABLE='/path/to/chrome' python3 tests/browser-native/run.py \
  --artifacts "$HOME/.hermes/cache/scratch/frame-native-acceptance"
```

Requires Python 3.10+ and the Python `playwright` package plus an installed Chromium browser. No installation/download is performed by this harness. Tested locally with Python 3.13, Playwright 1.53.0 and installed Google Chrome 157.0.8103.51. Browser selection: explicit environment path, installed Playwright Chromium, then macOS Google Chrome. Missing dependencies/browser or launch failure fail the command rather than skip or substitute a fake.

## What executes

The runner reads the current `apps/client/app.js` once, records its SHA256, and serves its complete source except the final DOM-ready `init()` invocation. The exact excluded startup tail is recorded in `evidence.json`; an unrecognized boundary fails closed. `bridge.js` opens the production DB and exposes the **actual** `syncCache`, normalization, snapshot and decoder probes. No cache algorithm, IndexedDB database, transaction, request, fetch, Blob, image decoder, or clock is replaced. Startup slideshow/timers are deliberately outside this cache acceptance scope.

A stdlib HTTP server bound only to `127.0.0.1` serves the test page, production source and explicit controlled HTTP fixtures. The tiny red/blue 1x1 PNGs are generated using PNG chunks, CRCs and zlib; corruption retains a PNG signature and image MIME but has an undecodable body. These are test fixtures, not production responses. No repository config, manifest, secret, private service or production endpoint is read or contacted.

Five native-browser checks:

1. A successful replacement download plus a controlled HTTP 503 leaves all old photo bytes, metadata, signature and manifest untouched, including after a real page reload. Retrying a complete replacement updates bytes and prunes the retired photo.
2. Browser-native decoding accepts the valid image and rejects corruption; production `syncCache` rejects the corrupt replacement and retains persisted old state.
3. The production older-browser image-element decoder fallback also accepts valid images and rejects corruption. Only feature availability (`createImageBitmap`) is disabled, not the fallback decoder.
4. A native read/write promotion transaction is explicitly aborted **after** two native photo puts, a prune/delete, and all four metadata puts have individually succeeded. Request-success event evidence precedes `IDBTransaction.abort()`. Reload verifies exact rollback of overwritten bytes, inserted bytes, pruning, manifest, signature and timestamps; a subsequent retry succeeds. This tests a real native abort, **not real disk exhaustion or a quota exception**.
5. An external-fetch probe is blocked by Playwright routing before networking.

Each test gets an isolated browser context; reload reopens IndexedDB within the same context/origin. These assertions prove persistence across page reload, not browser-process restart, power loss, disk durability, hardware acceptance or full kiosk boot.

## Offline controls

- Python audit hook rejects nonloopback socket connections.
- Browser HTTP routing permits only the exact local fixture origin; all other requests abort before networking.
- Service workers are blocked and WebSockets closed.
- Chromium is launched with background networking/component updates/sync disabled, a dead **loopback** proxy for nonloopback HTTP(S), QUIC disabled, and external DNS resolution mapped to failure.

These controls make this harness offline and fail closed for tested browser requests. They are not an OS-wide firewall or a sandbox for arbitrary hostile browser-native code. For a stronger CI boundary, execute the runner in a preprovisioned network-isolated container/runner with loopback available and no credentials.

## Results and CI

The command requires exactly five executed, unskipped passing checks and exits nonzero for failures/errors. It prints the browser version, production-source hash and artifacts location. Default output is under `~/.hermes/cache/scratch/frame-browser-native-*`; an explicit `--artifacts` path is created as requested. Artifacts include one Playwright trace ZIP per test and `evidence.json` with transaction events, baseline/reloaded photo bytes and metadata, blocked URLs and unittest totals. Traces contain only local fixture data. On assertion failure, evidence written before the failed assertion remains available.

CI integration is recommended as a **separate native-browser acceptance job**, alongside rather than replacing existing unit/fake tests. Preprovision/pin Python, Playwright and Chromium in a credential-free image; execute this command without downloads or nonloopback networking; preserve scratch artifacts on failure; set a bounded job timeout. This directory intentionally does not edit shared CI workflow files. The existing machine's unpinned installed Chrome is useful local evidence, not a reproducibly provisioned CI image.
