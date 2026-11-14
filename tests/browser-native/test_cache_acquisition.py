"""Native-browser acquisition checks: truncated bodies and native IDB abort.

Complements run.py: real Chromium decoding of malformed and Content-Length-
truncated downloads (including truncated baseline/progressive JPEGs through
BOTH the createImageBitmap and HTMLImageElement decoder paths), permanent
failures promoted as holes, migration from the v1 IndexedDB layout, plus a
native ConstraintError abort after a successful photo put, verified against
persisted IndexedDB after a fresh boot. Runs in the native-browser CI gate via
tools/safety/run_unittests.py, which refuses skips; an unavailable browser is
a failure.
"""
from pathlib import Path
import contextlib
import io
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from PIL import Image


def _launch_target(playwright):
    """Same selection order as run.py: explicit path, bundled, macOS Chrome."""
    explicit = os.environ.get("FRAME_BROWSER_EXECUTABLE")
    if explicit:
        return {"executable_path": explicit}
    bundled = Path(playwright.chromium.executable_path)
    if bundled.exists():
        return {"executable_path": str(bundled)}
    mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if mac.exists():
        return {"executable_path": str(mac)}
    raise RuntimeError(f"No installed Chromium found (bundled: {bundled}); no downloads attempted")


# Same offline hardening as run.py: no background/component traffic, and any
# non-loopback host fails resolution before it can leave the machine.
HARDENING_ARGS = [
    '--disable-background-networking', '--disable-component-update', '--disable-sync',
    '--no-first-run', '--disable-default-apps', '--disable-quic',
    '--proxy-server=http://127.0.0.1:9',
    '--proxy-bypass-list=127.0.0.1;localhost;[::1]',
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost',
]


def noise_jpeg(size=(256, 192), **save):
    import random
    rng = random.Random(11)
    image = Image.frombytes("RGB", size, bytes(rng.randrange(256) for _ in range(size[0] * size[1] * 3)))
    output = io.BytesIO()
    image.save(output, "JPEG", quality=90, **save)
    return output.getvalue()


class TestNativeAcquisition(unittest.TestCase):
    def test_native_browser_decoder_and_idb_restart_rollback(self):
        # Required in this gate: a missing dependency is a failure, never a skip.
        from playwright.sync_api import sync_playwright
        png = io.BytesIO()
        Image.new("RGB", (3, 2), "red").save(png, "PNG")
        image = png.getvalue()
        baseline, progressive = noise_jpeg(), noise_jpeg(progressive=True)
        jpegs = {
            "/full.jpg": baseline,
            "/full-progressive.jpg": progressive,
            # Honest Content-Length: only the decoder can notice these.
            "/truncated70.jpg": baseline[:int(len(baseline) * 0.7)],
            "/truncated50-progressive.jpg": progressive[:int(len(progressive) * 0.5)],
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                body = (b"<!doctype html><title>local cache test</title>" if path == "/"
                        else b"malformed image" if path == "/bad.png"
                        else jpegs[path] if path in jpegs
                        else image[:40] if path == "/truncated.png" else image)
                self.send_response(200)
                self.send_header("Content-Type", "text/html" if path == "/" else
                                 "image/jpeg" if path in jpegs else "image/png")
                self.send_header("Content-Length", str(len(body) + (100 if self.path == "/short.png" else 0)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        source = (Path(__file__).resolve().parents[2] / "apps/client/app.js").read_text()
        with sync_playwright() as playwright, contextlib.ExitStack() as cleanup:
            browser = playwright.chromium.launch(headless=True, args=HARDENING_ARGS,
                                                 **_launch_target(playwright))
            cleanup.callback(browser.close)
            context = browser.new_context()
            # Local-only harness: never contact configured publisher/cloud URLs.
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
            page = context.new_page()

            def boot(target=None):
                target = target or page
                target.goto(base)
                # Load unmodified production source after DOMContentLoaded while
                # suppressing automatic init/timers, then open real native IDB.
                target.evaluate("Object.defineProperty(document, 'readyState', {get: () => 'loading'})")
                target.add_script_tag(content=source)
                target.evaluate("async () => { db = await openDB(); sleep = async () => {}; scheduleEarlyRefetch = () => {}; }")

            def manifest(keys, version="1"):
                return {"schema": 2, "version": version, "mode": "sync", "start_epoch": 100,
                        "slide_seconds": 10, "photos": [{"id": key, "key": key,
                        "etag": version, "url": base + "/" + key} for key in keys]}

            # Migration: a v1-layout database written by the previous client
            # (photos keyed by cid, no staging store) must keep playing after
            # the production upgrade, and must accept the next sync.
            legacy_page = context.new_page()
            legacy_page.goto(base)
            legacy_page.evaluate("""async ([url, version]) => {
                const blob = await (await fetch(url)).blob();
                await new Promise((resolve, reject) => {
                    const req = indexedDB.open('SharedPhotoFrameDB', 1);
                    req.onupgradeneeded = () => {
                        const d = req.result;
                        d.createObjectStore('photos', {keyPath: 'id'}).createIndex('ts', 'ts', {unique: false});
                        d.createObjectStore('meta', {keyPath: 'key'});
                    };
                    req.onsuccess = () => {
                        const d = req.result, t = d.transaction(['photos', 'meta'], 'readwrite');
                        t.objectStore('photos').put({id: 'legacy/a.png', blob, mime: 'image/png', etag: version, ts: 1});
                        t.objectStore('meta').put({key: 'manifest', value: {version, startEpoch: 100, slideSeconds: 10,
                            mode: 'sync', photos: [{id: 'a.png', cid: 'legacy/a.png', etag: version, url}]}});
                        t.oncomplete = () => { d.close(); resolve(); };
                        t.onerror = () => reject(t.error);
                    };
                    req.onerror = () => reject(req.error);
                });
            }""", [base + "/legacy/a.png", "v1"])
            boot(legacy_page)
            self.assertEqual(legacy_page.evaluate("async () => await buildPlayOrder(await metaGet('manifest'))"),
                             ["legacy/a.png"])
            self.assertTrue(legacy_page.evaluate("raw => syncCache(normalizeManifest(raw))",
                            {**manifest(["b.png"], "v2"), "photos": [
                                {"id": "a.png", "key": "legacy/a.png", "etag": "v1", "url": base + "/legacy/a.png"},
                                *manifest(["b.png"], "v2")["photos"]]})["changed"])
            self.assertEqual(legacy_page.evaluate("async () => await buildPlayOrder(await metaGet('manifest'))"),
                             ["legacy/a.png", "b.png"])
            legacy_page.evaluate("db.close(); indexedDB.deleteDatabase('SharedPhotoFrameDB')")
            legacy_page.close()

            def sync(value):
                return page.evaluate("raw => syncCache(normalizeManifest(raw))", value)

            boot()
            old = manifest(["good.png", "second.png"])
            self.assertTrue(sync(old)["changed"])
            before = page.evaluate("async () => await metaGet('manifest')")
            for key in ("bad.png", "truncated.png", "short.png"):
                self.assertFalse(sync(manifest([key], "2"))["changed"])
                self.assertEqual(page.evaluate("async () => await metaGet('manifest')"), before)
            # A permanently undecodable slot is a hole; the rest promotes and
            # the slot index stays authoritative.
            holed = sync(manifest(["good.png", "bad.png", "second.png"], "h"))
            self.assertTrue(holed["changed"], holed)
            self.assertEqual(holed["permanent"], 1)
            self.assertEqual(page.evaluate("async () => await buildPlayOrder(await metaGet('manifest'))"),
                             ["good.png", None, "second.png"])
            self.assertTrue(sync(old)["changed"])

            def decoder_rejects_truncated_jpegs(path_label):
                # Fresh failure records so every decode below really runs.
                page.evaluate("localStorage.clear()")
                for key in ("full.jpg", "full-progressive.jpg"):
                    accepted = page.evaluate("async url => { await validateImageBlob(await (await fetch(url)).blob()); return true; }",
                                             base + "/" + key)
                    self.assertTrue(accepted, f"{path_label}: valid {key}")
                for key in ("truncated70.jpg", "truncated50-progressive.jpg"):
                    with self.subTest(decoder=path_label, fixture=key):
                        outcome = sync(manifest([key], path_label))
                        self.assertFalse(outcome["changed"], outcome)
                        self.assertEqual(outcome.get("permanent"), 1, outcome)
                        self.assertEqual(page.evaluate("async () => await metaGet('manifest')"), before)
                for key in ("bad.png", "truncated.png"):
                    outcome = sync(manifest([key], path_label + "-png"))
                    self.assertFalse(outcome["changed"], outcome)
                    self.assertEqual(outcome.get("permanent"), 1, outcome)

            decoder_rejects_truncated_jpegs("createImageBitmap")
            # Also exercise the real HTMLImageElement decoder fallback, not only
            # createImageBitmap. Both a valid image and rejection must work.
            page.evaluate("window.savedDecoder = window.createImageBitmap; window.createImageBitmap = undefined")
            decoder_rejects_truncated_jpegs("HTMLImageElement")
            self.assertTrue(sync(manifest(["fallback.png"], "2"))["changed"])
            page.evaluate("() => { window.createImageBitmap = window.savedDecoder; }")
            self.assertTrue(sync(old)["changed"])
            # Produce a native ConstraintError only AFTER a real photo put has
            # succeeded, forcing IDB to roll back bytes, metadata and deletions.
            page.evaluate("""() => {
                window.originalPut = IDBObjectStore.prototype.put;
                window.successfulPuts = 0;
                IDBObjectStore.prototype.put = function(row) {
                    const req = originalPut.call(this, row);
                    if (this.name === 'photos') {
                        const store = this;
                        req.addEventListener('success', () => {
                            successfulPuts++;
                            store.add(row); // duplicate primary key: asynchronous abort
                        });
                    }
                    return req;
                };
            }""")
            outcome = page.evaluate("""async raw => {
                try { await syncCache(normalizeManifest(raw)); return 'unexpected success'; }
                catch (error) { return error.name; }
                finally { IDBObjectStore.prototype.put = originalPut; }
            }""", manifest(["replacement.png"], "3"))
            self.assertEqual(outcome, "ConstraintError")
            self.assertGreater(page.evaluate("successfulPuts"), 0)
            page.evaluate("db.close()")
            boot()  # discard all in-memory state; read persistent native IDB
            persisted = page.evaluate("async () => await metaGet('manifest')")
            self.assertEqual(persisted, before)
            self.assertEqual(page.evaluate("async () => await buildPlayOrder(await metaGet('manifest'))"), ["good.png", "second.png"])
            self.assertTrue(page.evaluate("""async () => {
                const rows = await photoGetAll();
                if (rows.length !== 2) return false;
                for (const row of rows) await validateImageBlob(row.blob);
                return true;
            }"""))
            self.assertTrue(sync(manifest(["replacement.png"], "3"))["changed"])


class TestNativeRollbackTolerance(unittest.TestCase):
    """I1: a database written by a NEWER build (higher version) must not brick
    this build, and this build must not block a newer build's upgrade."""

    def test_newer_db_version_keeps_playing_and_yields_to_upgrades(self):
        from playwright.sync_api import sync_playwright
        png = io.BytesIO()
        Image.new("RGB", (3, 2), "blue").save(png, "PNG")
        image = png.getvalue()
        jpeg = noise_jpeg((64, 48))
        app0_end = 4 + int.from_bytes(jpeg[4:6], "big")
        padded = jpeg[:app0_end] + b"\x00\x00\x00" + jpeg[app0_end:]  # Pillow accepts this

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                body = (b"<!doctype html><title>rollback test</title>" if path == "/"
                        else padded if path == "/padded.jpg" else image)
                self.send_response(200)
                self.send_header("Content-Type", "text/html" if path == "/" else "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        source = (Path(__file__).resolve().parents[2] / "apps/client/app.js").read_text()
        with sync_playwright() as playwright, contextlib.ExitStack() as cleanup:
            browser = playwright.chromium.launch(headless=True, args=HARDENING_ARGS,
                                                 **_launch_target(playwright))
            cleanup.callback(browser.close)
            context = browser.new_context()
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base + "/") else route.abort())
            page = context.new_page()
            page.goto(base)
            # A future build left the DB at version 3 (same stores + its rows).
            page.evaluate("""async url => {
                const blob = await (await fetch(url)).blob();
                await new Promise((resolve, reject) => {
                    const req = indexedDB.open('SharedPhotoFrameDB', 3);
                    req.onupgradeneeded = () => {
                        const d = req.result;
                        d.createObjectStore('photos', {keyPath: 'id'}).createIndex('ts', 'ts', {unique: false});
                        d.createObjectStore('meta', {keyPath: 'key'});
                        d.createObjectStore('staging', {keyPath: 'sid'});
                    };
                    req.onsuccess = () => {
                        const d = req.result, t = d.transaction(['photos', 'meta'], 'readwrite');
                        t.objectStore('photos').put({id: 'k/a.png', blob, mime: 'image/png', etag: 'e', ts: 1});
                        t.objectStore('meta').put({key: 'manifest', value: {version: 'future', startEpoch: 100,
                            slideSeconds: 10, mode: 'sync', photos: [{id: 'a.png', cid: 'k/a.png', etag: 'e', url}]}});
                        t.oncomplete = () => { d.close(); resolve(); };
                        t.onerror = () => reject(t.error);
                    };
                    req.onerror = () => reject(req.error);
                });
            }""", base + "/a.png")
            page.evaluate("Object.defineProperty(document, 'readyState', {get: () => 'loading'})")
            page.add_script_tag(content=source)
            opened = page.evaluate("""async () => {
                try { db = await openDB(); return db.version; }
                catch (error) { return 'fatal: ' + error.name + ': ' + error.message; }
            }""")
            self.assertEqual(opened, 3)
            self.assertEqual(page.evaluate("async () => await buildPlayOrder(await metaGet('manifest'))"), ["k/a.png"])
            self.assertTrue(page.evaluate("""async url => {
                await validateImageBlob(await (await fetch(url)).blob()); return true; }""", base + "/padded.jpg"))
            # A newer build in another tab upgrades: this page must close its
            # handle (onversionchange) rather than block the upgrade forever.
            other = context.new_page()
            other.goto(base)
            outcome = other.evaluate("""() => new Promise((resolve, reject) => {
                let blocked = false;
                const timer = setTimeout(() => resolve('hung'), 5000);
                const req = indexedDB.open('SharedPhotoFrameDB', 4);
                req.onblocked = () => { blocked = true; };
                req.onsuccess = () => { clearTimeout(timer); req.result.close(); resolve(blocked ? 'blocked' : 'ok'); };
                req.onerror = () => { clearTimeout(timer); reject(req.error); };
            })""")
            self.assertEqual(outcome, "ok")
