# legacy/

Archived v1-generation source. **Reference only — never deploy from here.**

| Path | Origin | Pinned at |
| --- | --- | --- |
| `v1/shared-photo-frame/` | `TcDrozd/shared-photo-frame` (private) | tag `client-v2.0.0` = `e99fc55` (2025-12-29) |
| `shared-photo-frame-dash/` | `TcDrozd/shared-photo-frame-dash` (private) | vendored at monorepo init, commit not recorded |

Both are plain files. Nothing here is a submodule, and nothing here should
become one.

## The v1 / v2 naming trap

The commit vendored under `v1/` is tagged **`client-v2.0.0`** upstream. Those
are two different version namespaces:

- **Upstream's "client v2"** — the appliance-grade rewrite of the old
  standalone app, the last thing that repo shipped.
- **This monorepo's "v1"** — the whole pre-monorepo *generation*, of which
  upstream's client v2 is the final state.

So `legacy/v1/` holding a `v2.0.0` tag is correct, not a mistake. The v1
runtime still on the wall at `/opt/shared-photo-frame/` is this code.

## Redaction

`v1/shared-photo-frame/docs/manifest.json` is a captured sample whose 50
presigned URLs embedded an AWS access key id (`AKIA…`) in their query strings.
Those query strings were replaced with `?REDACTED` when this was vendored;
everything else in the file is verbatim. The signatures had already expired
2025-12-27, so nothing was ever grantable — but the key id itself does not
belong in this repo.

> The unredacted copy still exists in the upstream repo's history. If that key
> has not been rotated or deactivated, that is worth doing independently of
> this repo.

For a current manifest example use
`apps/frame-dash/tests/fixtures/golden_manifest.json`, which is schema 2 and
pinned by a test. The file here is schema 1 and is kept for historical shape
only.

## How this directory got fixed

The monorepo's initial commit (`0d9d255`, "Initial monorepo… Preserves v1
source for reference") did not preserve the v1 source. `git add` on a
directory that still contained its own `.git/` recorded a **gitlink** — a bare
`160000` pointer to commit `e99fc55` in another repo — instead of the files.
Because `git submodule add` was never run, no `.gitmodules` was written, so it
was not even a resolvable submodule: every clone produced an empty directory,
silently, and `git status` stayed clean.

That entry was removed and replaced with the actual files from the tag it
pointed at. The upstream repo and its tag remain the canonical freeze; this is
a copy for self-containment, not a migration.

**If you archive another repo here, copy the files.** Do not `git add` a
directory that contains a `.git/`.
