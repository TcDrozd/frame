# Shared Photo Frame — Pi Client

**Status:** Vendored, not re-verified on hardware
**Role:** Raspberry Pi kiosk host for `apps/client`

---

## Purpose

`apps/client` is the slideshow. This directory is the **appliance that runs
it** on a Raspberry Pi wired to a TV or monitor: X + Openbox + fullscreen
Chromium on boot, plus a small webhook so the displayed page can be changed
remotely without a keyboard.

The split matters. The Pi has no idea what a manifest is — it points Chromium
at a URL and keeps it pointed there. All the frame logic (fetch → cache →
play, offline recovery, synchronized playback) lives in `apps/client` and is
identical to what the Android tablets run under Fully Kiosk. This is a second
*host* for the same client, not a second client.

## Provenance

Vendored from the private repo **`TcDrozd/pi-dash`** @ `00c69ad`
(2025-09-16), which was written as a generic homelab TV kiosk with Frigate as
its default page. It was never part of this monorepo — see the history note at
the bottom.

Vendored as plain files rather than a submodule, deliberately: `legacy/v1/`
already demonstrates how a stray gitlink silently archives nothing.

## Files

```
install.sh                      one-shot installer, run on the Pi
kiosk-webhook.py                Flask API to change the displayed URL
requirements.txt                flask
openbox-autostart          →    ~kiosk/.config/openbox/autostart
url.txt.example            →    /var/lib/kiosk/url.txt
.env.example               →    /etc/kiosk/kiosk.env
systemd/
  kiosk-browser.service    →    X + Openbox + Chromium, Restart=always
  kiosk-webhook.service    →    the Flask API
  kiosk-url.path           →    watches /var/lib/kiosk/url.txt
  kiosk-url.service        →    restarts the browser when it changes
```

## Runtime model

`/var/lib/kiosk/url.txt` is the single piece of state. Openbox's autostart
reads it at launch and hands it to Chromium; `kiosk-url.path` watches it and
restarts `kiosk-browser.service` whenever it changes. The webhook's only job
is to write that file.

```
POST /kiosk/set  {"url": "..."}   X-Kiosk-Token: <token>   → writes url.txt
GET  /kiosk/get                                            → reads url.txt
```

Writes are rejected unless the URL matches a prefix in `ALLOW_PREFIXES`
(LAN/tailnet hosts only). The list is `http://`-only on purpose — the frame
client is served over plain HTTP on the home server, and admitting arbitrary
`https://` would turn the Pi into an open redirect for anyone who gets the
token. Add a prefix there if you ever serve the client over TLS.

## Install

```bash
# on the Pi, from a checkout of this repo
apps/pi-client/install.sh
```

Idempotent where it counts: it will not overwrite an existing
`/etc/kiosk/kiosk.env` or `/var/lib/kiosk/url.txt`. On a first run it
generates a token and prints where it landed.

```bash
# point it at the frame
curl -X POST http://<pi>:5000/kiosk/set \
  -H "X-Kiosk-Token: $KIOSK_TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"http://frame.local/"}'

systemctl status kiosk-browser kiosk-webhook kiosk-url.path
journalctl -u kiosk-browser -f
```

To repoint the *manifest* rather than the page, use the client's own
mechanism — `http://frame.local/?manifest=<url>` persists to localStorage
(see `apps/client/README.md`). The Pi layer stays out of it.

## Changed from upstream

Four fixes were applied while vendoring; upstream `pi-dash` still has all of
them. Diff against `00c69ad` if you want to see exactly what moved.

1. **`kiosk-webhook.py` had no entrypoint.** No `app.run()` and no WSGI
   server, but `kiosk-webhook.service` runs it with bare `python`, so the
   process defined a Flask app, exited 0, and — with `Restart=always` /
   `RestartSec=1` — restart-looped forever. Added a `__main__` block with
   `KIOSK_BIND` / `KIOSK_PORT` overrides.
2. **`kiosk-webhook.service` had an install script inside it.** Everything
   after a stray `EOF` was leftover heredoc from the setup instructions,
   including the only copy of `kiosk-url.path`. Truncated the unit; recovered
   `kiosk-url.path` as a real file; the rest became `install.sh`.
3. **The token was inline** as `Environment=KIOSK_TOKEN=CHANGE_ME`. Now
   `EnvironmentFile=/etc/kiosk/kiosk.env`, mode 0640 root:kiosk, so the secret
   is not world-readable in the unit.
4. **Defaults point at the frame**, not Frigate — `url.txt.example` and the
   autostart fallback, plus `http://frame.local` / `http://frame.lan` in
   `ALLOW_PREFIXES`.

`install.sh` is a reconstruction of a script that only survived as fragments,
and none of this has been re-run on a fresh Pi since vendoring. Treat the
first install as a shakedown.

## History

This never lived in the monorepo. Verified by enumerating every path in every
commit across all branches and grepping full history for
`raspberr|openbox|chromium|kiosk-browser|xinit` — zero hits. It was always a
separate repo, which is why it reads as generic infrastructure that happens to
be able to display a photo frame.
