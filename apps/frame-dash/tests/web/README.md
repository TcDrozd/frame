# Dashboard DOM safety regressions

The SPA remains vanilla JS with no build step or runtime dependencies. Tests import
its actual ES modules into jsdom and mock the API boundary (no AWS, Cognito, or
production authentication). The router test uses a synthetic local token and
forbids network fetches. Injection assertions check the parsed DOM directly, with
no CSP masking unsafe attributes.

Run from `apps/frame-dash` (Node 24 LTS, matching CI's pinned 24.11.1; jsdom 27 requires Node 20.19+):

```sh
npm ci --ignore-scripts
npm run test:web
```

## Recorded red/green evidence

Before changing production source, `npm run test:web` returned 9 tests, 2 passing,
7 failing. Failures reproduced truncated quoted input data, reset settings after
reorder/remove, preview issuing PUT, cancelled publication issuing PUT, saving
before confirmation, no dirty unload warning, and missing browse revision.
The initial folder test inadvertently repeated the first folder on page two;
correcting the fixture to a distinct second folder and running
`node --test --test-name-pattern='folder pagination' tests/web/safety.test.js`
returned 1 failure: actual `[B]` versus expected `[A, B]`.

After implementation and expanded coverage, `npm run test:web` returned **18
passing, 0 failing** at that stage (current counts: see `docs/milestone-a-ci.md`). Coverage includes quoted stored names/start epochs/image URLs,
accessible item controls, retained settings, dirty navigation/unload warnings,
nonmutating previews, cancelled/confirmed publish ordering, conflicts at both save
and publish, missing revisions, revision-bound deletion and append, folder-only
pagination/deduplication, delayed view responses, the actual app hash router, and
HTTP 409 propagation.

## Backend contract used by the UI

- GET playlist and successful PUT return a complete playlist with integer
  `revision`, `name`, `settings`, and ordered `items`.
- PUT playlist includes `expected_revision`; conflicts return HTTP 409 and make no
  update. Browse append fetches the current playlist and sends its revision, never
  silently retries/rebases a conflict, and retains selection on failure.
- DELETE playlist includes JSON `{expected_revision}` (aligned with the backend
  safety lane). The API transport now accepts a DELETE body.
- Preview POST `/api/playlists/:id/publish` sends
  `{dry_run:true, expected_revision, draft:{name,settings,items}}`. It never PUTs,
  must not persist anything, and returns `manifest` plus `skipped_keys`.
- Real publication is confirmed **before** PUT; only the reviewed snapshot is
  saved, then POST sends `{dry_run:false, restart:true, expected_revision}` using
  the exact PUT response revision. A concurrent edit between PUT and POST must
  return 409 rather than publish a different revision. Successful response fields:
  `version`, `photo_count`, `skipped_keys`.
- Activation uses this same confirmed publication flow, not the old unsafe
  `PUT /api/active` activation path. Clearing active still PUTs
  `{playlist_id:null}` and does not discard the working draft.

## Limits / remaining integration checks

These are Node DOM regressions, not a real-browser end-to-end test against a
running backend. Native beforeunload dialog wording/appearance and keyboard/
screen-reader ergonomics still need browser acceptance testing. The API is mocked
except for a fetch-stubbed transport test; backend atomicity, draft nonpersistence,
and publish fencing need the backend lane's own tests. Network-ambiguous publication
results require an operator status check; the UI deliberately does not automatically
retry. Drafts are in memory only; accepting navigation or closing the tab discards
them. No deployment or live AWS writes were performed.
