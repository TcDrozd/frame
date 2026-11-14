"""End-to-end runs of the Pi Zero syncer against a local HTTP server.

`frame_sync.main()` is the whole program, so it is exercised as one: a real
manifest fetch (with ETag handling), real downloads, real files on disk. The
device is hand-deployed and mostly unreachable, and the property that must
never break is offline playback — a sync run that half-fails must not delete
the images the viewer is still showing. That is asserted here rather than on
the hardware.

apps/pi-zero-client is a directory of plain scripts, not a package, so the
modules are loaded by path with that directory on sys.path (which is what
systemd's `python3 /opt/frame/frame_sync.py` gives them on the device).
"""
import hashlib
import errno
import io
from PIL import Image


def tiny_image(color=0):
    output = io.BytesIO()
    Image.new("RGB", (3, 2), (color % 256, 50, 100)).save(output, "PNG")
    return output.getvalue()

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PI_ZERO = Path(__file__).resolve().parents[1] / "apps" / "pi-zero-client"
if str(PI_ZERO) not in sys.path:
    sys.path.insert(0, str(PI_ZERO))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, PI_ZERO / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


frame_cache = _load("frame_cache")
frame_sync = _load("frame_sync")


class _Handler(BaseHTTPRequestHandler):
    """Serves the manifest (ETag-aware) and the photo bodies behind it."""

    def do_GET(self):  # noqa: N802 - stdlib naming
        state = self.server.fixture
        state.setdefault("requests", []).append((self.path, self.headers.get("If-None-Match")))
        if self.path.startswith("/manifest.json"):
            if state.get("on_manifest"):
                state["on_manifest"]()
            body = json.dumps(state["manifest"]).encode()
            etag = '"%s"' % hashlib.md5(body).hexdigest()
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        key = self.path.lstrip("/").split("?", 1)[0]
        if "sig=expired" in self.path:
            self.send_response(403)
            self.end_headers()
            return
        if key in state["broken"]:
            self.send_response(500)
            self.end_headers()
            return
        body = state["bodies"].get(key)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        if key not in state.get("no_length", ()):
            self.send_header("Content-Length", str(state.get("lengths", {}).get(key, len(body))))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class SyncTestCase(unittest.TestCase):
    """Runs frame_sync.main() against a throwaway /opt/frame and a real socket."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.images = root / "images"
        self.staging = root / "staging"
        self.local_manifest = root / "manifest.json"
        self.state_file = root / ".sync_state.json"

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.fixture = {"manifest": {}, "bodies": {}, "broken": set()}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.tmp.cleanup)

        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"

        # Point the module's hardcoded /opt/frame paths at the temp dir.
        for name, value in (
            ("IMAGES_DIR", self.images),
            ("STAGING_DIR", self.staging),
            ("LOCAL_MANIFEST", self.local_manifest),
            ("STATE_FILE", self.state_file),
            ("MANIFEST_URL", f"{self.base}/manifest.json"),
            ("TIMEOUT", 10),
        ):
            patcher = unittest.mock.patch.object(frame_sync, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    # --- fixture helpers -------------------------------------------------

    def publish(self, keys, *, start_epoch=1788813977, broken=()):
        """Serve a schema-2 manifest for `keys`, each with distinct content."""
        photos = []
        bodies = {}
        for i, key in enumerate(keys):
            bodies[key] = tiny_image(i)
            photos.append(
                {
                    "id": key.rsplit("/", 1)[-1],
                    "key": key,
                    "url": f"{self.base}/{key}?sig=whatever",
                    "name": key.rsplit("/", 1)[-1].rsplit(".", 1)[0],
                    "bytes": len(bodies[key]),
                    "etag": "x",
                }
            )
        self.server.fixture["manifest"] = {
            "schema": 2,
            "version": "vtest",
            "generated_at": "2026-09-18T22:52:39Z",
            "mode": "sync",
            "slide_seconds": 1380,
            "start_epoch": start_epoch,
            "photos": photos,
        }
        self.server.fixture["bodies"] = bodies
        self.server.fixture["broken"] = set(broken)

    def cached(self):
        return {p.name for p in self.images.iterdir()} if self.images.exists() else set()

    def state(self):
        return json.loads(self.state_file.read_text()) if self.state_file.exists() else {}


class TestAcquisitionSafety(SyncTestCase):
    def test_bad_body_preserves_last_good_after_restart(self):
        for bad in (b"not an image", tiny_image()[:45], tiny_image()[:-12]):
            with self.subTest(body=bad):
                self.publish(["old.jpg"])
                frame_sync.main()
                previous = self.local_manifest.read_bytes()
                old_files = {p.name: p.read_bytes() for p in self.images.iterdir()}
                self.publish(["new.jpg"])
                self.server.fixture["bodies"]["new.jpg"] = bad
                frame_sync.main()
                self.assertEqual(self.local_manifest.read_bytes(), previous)
                for name, data in old_files.items():
                    self.assertEqual((self.images / name).read_bytes(), data)
                    with Image.open(self.images / name) as image:
                        image.load()

    def test_short_content_length_cannot_promote_even_decodable_image(self):
        self.publish(["old.jpg"])
        frame_sync.main()
        previous = self.local_manifest.read_bytes()
        self.publish(["new.jpg"])
        self.server.fixture["lengths"] = {"new.jpg": len(tiny_image()) + 100}
        frame_sync.main()
        self.assertEqual(self.local_manifest.read_bytes(), previous)

    def test_304_repairs_missing_and_corrupt_assets(self):
        self.publish(["a.jpg", "b.jpg"])
        frame_sync.main()
        manifest = json.loads(self.local_manifest.read_text())
        a, b = [self.images / frame_cache.local_name(p) for p in manifest["photos"]]
        a.unlink()
        b.write_bytes(b"corrupt")
        frame_sync.main()
        for path in (a, b):
            with Image.open(path) as image:
                image.verify()
        self.assertEqual(json.loads(self.local_manifest.read_text()), manifest)

    def test_expired_urls_refetch_unconditionally_once_and_recover(self):
        self.publish(["old.jpg"])
        frame_sync.main()
        previous = self.local_manifest.read_bytes()
        self.publish(["new.jpg"])
        candidate = self.server.fixture["manifest"]
        candidate["photos"][0]["url"] = f"{self.base}/new.jpg?sig=expired"
        checks = []
        def refresh_urls():
            checks.append(self.local_manifest.read_bytes())
            if len(checks) == 2:
                candidate["photos"][0]["url"] = f"{self.base}/new.jpg?sig=fresh"
        self.server.fixture["on_manifest"] = refresh_urls
        frame_sync.main()
        self.assertEqual(checks, [previous, previous])
        manifest_requests = [r for r in self.server.fixture["requests"] if r[0].startswith("/manifest")]
        self.assertIsNone(manifest_requests[-1][1])
        self.assertEqual(json.loads(self.local_manifest.read_text()), candidate)
        self.assertTrue(frame_sync.complete_window(candidate))

    def test_permanently_expired_urls_bound_retry_and_preserve_good_window(self):
        self.publish(["old.jpg"])
        frame_sync.main()
        previous = self.local_manifest.read_bytes()
        self.publish(["new.jpg"])
        self.server.fixture["manifest"]["photos"][0]["url"] = f"{self.base}/new.jpg?sig=expired"
        self.server.fixture["requests"] = []
        frame_sync.main()
        self.assertEqual(self.local_manifest.read_bytes(), previous)
        self.assertTrue(frame_sync.complete_window(json.loads(previous)))
        self.assertEqual(len([r for r in self.server.fixture["requests"] if r[0].startswith("/manifest")]), 2)

    def test_304_repairs_missing_local_manifest(self):
        self.publish(["a.jpg"])
        frame_sync.main()
        prior = self.local_manifest.read_bytes()
        self.local_manifest.unlink()
        frame_sync.main()
        self.assertEqual(self.local_manifest.read_bytes(), prior)
        self.assertIsNone(self.server.fixture["requests"][-1][1])

    def test_same_signature_repairs_missing_assets(self):
        self.publish(["a.jpg"])
        frame_sync.main()
        name = frame_cache.local_name(self.server.fixture["manifest"]["photos"][0])
        (self.images / name).unlink()
        state = self.state()
        state.pop("etag")
        self.state_file.write_text(json.dumps(state))
        frame_sync.main()
        self.assertTrue((self.images / name).exists())


class TestFreshSync(SyncTestCase):
    def test_downloads_window_and_records_state(self) -> None:
        keys = ["photos/a.jpg", "photos/01_scans/b.jpg"]
        self.publish(keys)

        frame_sync.main()

        self.assertEqual(self.cached(), frame_cache.wanted_names(
            self.server.fixture["manifest"]["photos"]))
        self.assertEqual(json.loads(self.local_manifest.read_text())["version"], "vtest")
        self.assertIn("etag", self.state())
        self.assertIn("sig", self.state())
        self.assertNotIn("last_failed", self.state())

    def test_bodies_land_in_the_right_files(self) -> None:
        self.publish(["photos/a.jpg"])
        frame_sync.main()
        name = frame_cache.local_name(self.server.fixture["manifest"]["photos"][0])
        self.assertEqual(
            (self.images / name).read_bytes(),
            self.server.fixture["bodies"]["photos/a.jpg"],
        )

    def test_unchanged_manifest_short_circuits_on_304(self) -> None:
        self.publish(["photos/a.jpg"])
        frame_sync.main()
        before = self.cached()

        frame_sync.main()  # ETag still matches -> 304

        self.assertEqual(self.cached(), before)

    def test_staging_is_left_clean(self) -> None:
        self.publish(["photos/a.jpg", "photos/b.jpg"])
        frame_sync.main()
        self.assertEqual(list(self.staging.iterdir()), [])


class TestCollidingBasenames(SyncTestCase):
    def test_same_basename_two_folders_both_cached_correctly(self) -> None:
        """The silent-wrong-photo bug: one file per key, each its own bytes."""
        keys = ["photos/IMG_0274.jpeg", "photos/04_phone_modern/IMG_0274.jpeg"]
        self.publish(keys)

        frame_sync.main()

        self.assertEqual(len(self.cached()), 2)
        for photo in self.server.fixture["manifest"]["photos"]:
            key = photo["key"]
            name = frame_cache.local_name(photo)
            self.assertEqual(
                (self.images / name).read_bytes(),
                self.server.fixture["bodies"][key],
                f"{key} got the wrong bytes",
            )


class TestPruning(SyncTestCase):
    def test_rotated_window_evicts_the_previous_one(self) -> None:
        self.publish(["photos/day1-a.jpg", "photos/day1-b.jpg"])
        frame_sync.main()
        day1 = self.cached()
        self.assertEqual(len(day1), 2)

        self.publish(["photos/day2-a.jpg", "photos/day2-b.jpg"])
        frame_sync.main()
        day2 = self.cached()

        self.assertEqual(len(day2), 2)
        self.assertEqual(day1 & day2, set())

    def test_legacy_id_named_files_are_cleaned_up(self) -> None:
        """Migration: a device's pre-existing basename cache is not kept forever."""
        self.images.mkdir(parents=True)
        (self.images / "a.jpg").write_bytes(tiny_image(1))
        (self.images / "stale-from-january.jpg").write_bytes(tiny_image(1))

        self.publish(["photos/a.jpg"])
        frame_sync.main()

        self.assertEqual(self.cached(), frame_cache.wanted_names(self.server.fixture["manifest"]["photos"]))

    def test_cache_stays_bounded_across_many_rotations(self) -> None:
        for day in range(6):
            self.publish([f"photos/day{day}-{n}.jpg" for n in range(3)])
            frame_sync.main()
        self.assertEqual(len(self.cached()), 3)


class TestPartialFailure(SyncTestCase):
    def test_failed_download_does_not_prune_the_previous_window(self) -> None:
        """Offline playback is the property that must never break."""
        self.publish(["photos/day1-a.jpg", "photos/day1-b.jpg"])
        frame_sync.main()
        day1 = self.cached()

        self.publish(
            ["photos/day2-a.jpg", "photos/day2-b.jpg"], broken=["photos/day2-b.jpg"]
        )
        frame_sync.main()

        self.assertTrue(day1 <= self.cached(), "previous window was deleted mid-failure")

    def test_failed_run_persists_neither_etag_nor_sig(self) -> None:
        """Both are early returns; keeping them would strand the failed photos."""
        self.publish(["photos/a.jpg", "photos/b.jpg"], broken=["photos/b.jpg"])
        frame_sync.main()

        state = self.state()
        self.assertNotIn("etag", state)
        self.assertNotIn("sig", state)
        self.assertEqual(state.get("last_failed"), 1)

    def test_next_run_retries_and_then_prunes(self) -> None:
        self.publish(["photos/a.jpg", "photos/b.jpg"], broken=["photos/b.jpg"])
        frame_sync.main()
        self.assertEqual(len(self.cached()), 1)

        self.server.fixture["broken"] = set()  # network came back
        frame_sync.main()

        self.assertEqual(
            self.cached(),
            frame_cache.wanted_names(self.server.fixture["manifest"]["photos"]),
        )
        self.assertNotIn("last_failed", self.state())

    def test_empty_manifest_leaves_the_cache_alone(self) -> None:
        self.publish(["photos/a.jpg"])
        frame_sync.main()
        before = self.cached()

        self.publish([])
        frame_sync.main()

        self.assertEqual(self.cached(), before)


class TestLastGoodPlayback(SyncTestCase):
    def restarted_viewer(self):
        viewer = _load("viewer")
        viewer.MANIFEST_FILE = self.local_manifest
        return viewer

    def test_failed_replacement_preserves_playable_manifest_after_restart(self):
        for broken in (["new/a.jpg"], ["new/a.jpg", "new/b.jpg"]):
            with self.subTest(broken=broken):
                self.publish(["old/a.jpg", "old/b.jpg"])
                frame_sync.main()
                prior = self.local_manifest.read_bytes()
                self.publish(["new/a.jpg", "new/b.jpg"], broken=broken)
                frame_sync.main()
                self.assertEqual(self.local_manifest.read_bytes(), prior)
                # A fresh viewer module has only persisted state, not viewer RAM.
                viewer = self.restarted_viewer()
                rebooted = viewer.load_manifest_if_changed()
                self.assertEqual(rebooted, json.loads(prior))
                for photo in rebooted["photos"]:
                    self.assertIsNotNone(viewer.resolve_image(self.images, photo))
                self.server.fixture["broken"].clear()
                frame_sync.main()
                self.assertEqual(json.loads(self.local_manifest.read_text()), self.server.fixture["manifest"])

    def test_schema_one_and_reordering_preserve_clock_across_restart(self):
        self.publish(["a.jpg", "b.jpg"])
        incoming = self.server.fixture["manifest"]
        incoming["schema"] = 1
        for photo in incoming["photos"]:
            photo.pop("key")
            photo.pop("etag")
        frame_sync.main()
        incoming["photos"].reverse()
        incoming["start_epoch"] += 7
        frame_sync.main()
        viewer = self.restarted_viewer()
        persisted = viewer.load_manifest_if_changed()
        self.assertEqual(persisted, incoming)
        self.assertEqual([p["id"] for p in persisted["photos"]], ["b.jpg", "a.jpg"])
        self.assertEqual(viewer.compute_sync_index(persisted["start_epoch"], 10, 2,
                                                  persisted["start_epoch"] + 10), 1)
        for photo in persisted["photos"]:
            self.assertIsNotNone(viewer.resolve_image(self.images, photo))

    def test_changed_etag_refreshes_same_key_and_failed_refresh_keeps_old_bytes(self):
        self.publish(["photos/a.jpg", "photos/b.jpg"])
        frame_sync.main()
        prior = json.loads(self.local_manifest.read_text())
        old = self.images / frame_cache.local_name(prior["photos"][0])
        before = old.read_bytes()
        incoming = self.server.fixture["manifest"]
        incoming["photos"][0]["etag"] = "changed"
        incoming["photos"][1]["etag"] = "changed"
        self.server.fixture["bodies"]["photos/a.jpg"] = tiny_image(200)
        self.server.fixture["broken"].add("photos/b.jpg")
        frame_sync.main()
        self.assertEqual(json.loads(self.local_manifest.read_text()), prior)
        self.assertEqual(old.read_bytes(), before)
        self.server.fixture["broken"].clear()
        frame_sync.main()
        current = json.loads(self.local_manifest.read_text())
        self.assertEqual((self.images / frame_cache.local_name(current["photos"][0])).read_bytes(), tiny_image(200))

    def test_photo_without_url_is_a_hole_not_a_frozen_update(self):
        # M4 deliberate contract change: this test used to assert that a
        # URL-less entry blocks promotion (treated as transient). Retrying a
        # manifest property every tick cannot fix it, so it is now a permanent
        # per-(key, etag) failure: the slot is kept as a hole and the rest of
        # the window promotes. A window with nothing playable still never does.
        self.publish(["old.jpg"])
        frame_sync.main()
        self.publish(["new.jpg"])
        incoming = self.server.fixture["manifest"]
        incoming["photos"].append({"id": "no-url.jpg"})
        frame_sync.main()
        self.assertEqual(json.loads(self.local_manifest.read_text()), incoming)
        viewer = self.restarted_viewer()
        rebooted = viewer.load_manifest_if_changed()
        self.assertEqual([viewer.resolve_image(self.images, p) is None for p in rebooted["photos"]],
                         [False, True])
        (record,) = self.state()["permanent_failures"].values()
        self.assertIn("URL", record["reason"])
        # Publisher fixes the entry (same identity, URL now present): no wait
        # for the backoff, it downloads on the next run.
        incoming["photos"][1].update({"url": f"{self.base}/no-url.jpg?sig=x"})
        self.server.fixture["bodies"]["no-url.jpg"] = tiny_image(9)
        frame_sync.main()
        self.assertIsNotNone(self.restarted_viewer().resolve_image(self.images, incoming["photos"][1]))
        self.assertEqual(self.state().get("permanent_failures"), {})

    def test_url_less_only_window_keeps_last_good(self):
        self.publish(["old.jpg"])
        frame_sync.main()
        prior = self.local_manifest.read_bytes()
        self.server.fixture["manifest"] = {"schema": 1, "version": "s1", "mode": "sync",
                                           "start_epoch": 1, "slide_seconds": 5,
                                           "photos": [{"id": "x.jpg"}, {"id": "y.jpg"}]}
        frame_sync.main()
        self.assertEqual(self.local_manifest.read_bytes(), prior)

    def test_promotion_is_atomic_and_failure_does_not_prune_old_window(self):
        self.publish(["old.jpg"])
        frame_sync.main()
        prior = self.local_manifest.read_bytes()
        self.publish(["new.jpg"])
        original = Path.replace
        observed = []
        def replace(path, target):
            if Path(target) == self.local_manifest:
                observed.append(path)
                self.assertEqual(self.local_manifest.read_bytes(), prior)
                json.loads(path.read_text())
                raise OSError("interrupted promotion")
            return original(path, target)
        with unittest.mock.patch.object(Path, "replace", replace):
            try:
                frame_sync.main()
            except OSError:
                pass
        self.assertTrue(observed, "manifest must be promoted using atomic replace")
        self.assertEqual(self.local_manifest.read_bytes(), prior)
        for photo in json.loads(prior)["photos"]:
            self.assertTrue((self.images / frame_cache.local_name(photo)).exists())


def image_bytes(fmt, size=(3, 2), noise=False, **save):
    output = io.BytesIO()
    if noise:
        import random
        rng = random.Random(7)
        image = Image.frombytes("RGB", size, bytes(rng.randrange(256) for _ in range(size[0] * size[1] * 3)))
    else:
        image = Image.new("RGB", size, (10, 200, 30))
    image.save(output, fmt, **save)
    return output.getvalue()


class PermanentFailureCase(SyncTestCase):
    """One undecodable entry must become a hole, never a frozen update loop."""

    def setUp(self):
        super().setUp()
        self.clock = [1_800_000_000.0]
        patcher = unittest.mock.patch.object(frame_sync, "_now", lambda: self.clock[0], create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def photo_requests(self, key):
        return [r for r in self.server.fixture.get("requests", []) if r[0].split("?", 1)[0] == "/" + key]

    def entry(self, key):
        return next(p for p in self.server.fixture["manifest"]["photos"] if p["key"] == key)

    def failures(self):
        return self.state().get("permanent_failures", {})


class TestPermanentFailureHoles(PermanentFailureCase):
    BAD_BODIES = {
        "unsupported TIFF": lambda: image_bytes("TIFF"),
        "unsupported BMP": lambda: image_bytes("BMP"),
        "truncated JPEG with honest length": lambda: image_bytes("JPEG", (64, 48), noise=True)[:-200],
        "garbage": lambda: b"\x00HEIC-ish body that no allowed decoder accepts",
    }

    def test_undecodable_entry_becomes_hole_and_rest_of_window_promotes(self):
        for label, body in self.BAD_BODIES.items():
            with self.subTest(label):
                folder = label.replace(" ", "_")
                self.publish([f"{folder}/a.jpg", f"{folder}/b.jpg", f"{folder}/c.jpg"])
                bad = f"{folder}/b.jpg"
                self.server.fixture["bodies"][bad] = body()
                frame_sync.main()
                incoming = self.server.fixture["manifest"]
                self.assertEqual(json.loads(self.local_manifest.read_text()), incoming)
                self.assertNotIn(frame_cache.local_name(self.entry(bad)), self.cached())
                for key in (f"{folder}/a.jpg", f"{folder}/c.jpg"):
                    self.assertIn(frame_cache.local_name(self.entry(key)), self.cached())
                self.assertEqual(len(self.failures()), 1, self.failures())
                self.assertEqual(list(self.staging.iterdir()), [])
                viewer = _load("viewer")
                viewer.MANIFEST_FILE = self.local_manifest
                rebooted = viewer.load_manifest_if_changed()
                # Slot order is authoritative: the hole stays a hole.
                self.assertEqual([viewer.resolve_image(self.images, p) is None for p in rebooted["photos"]],
                                 [False, True, False])

    def test_decompression_bomb_is_a_hole_not_a_freeze(self):
        self.publish(["a.jpg", "big.jpg"])
        self.server.fixture["bodies"]["big.jpg"] = image_bytes("PNG", (40, 40))
        with unittest.mock.patch.object(Image, "MAX_IMAGE_PIXELS", 1000):
            frame_sync.main()
        self.assertEqual(json.loads(self.local_manifest.read_text()), self.server.fixture["manifest"])
        self.assertEqual(len(self.failures()), 1)

    def test_transient_failure_still_blocks_promotion_alongside_a_hole(self):
        self.publish(["old.jpg"])
        frame_sync.main()
        prior = self.local_manifest.read_bytes()
        self.publish(["a.jpg", "bad.jpg", "net.jpg"], broken=["net.jpg"])
        self.server.fixture["bodies"]["bad.jpg"] = b"garbage"
        frame_sync.main()
        self.assertEqual(self.local_manifest.read_bytes(), prior)
        self.assertNotIn("etag", self.state())

    def test_all_slots_undecodable_keeps_last_good(self):
        self.publish(["old.jpg"])
        frame_sync.main()
        prior = self.local_manifest.read_bytes()
        self.publish(["bad1.jpg", "bad2.jpg"])
        self.server.fixture["bodies"].update({"bad1.jpg": b"x", "bad2.jpg": image_bytes("BMP")})
        frame_sync.main()
        self.assertEqual(self.local_manifest.read_bytes(), prior)
        for photo in json.loads(prior)["photos"]:
            self.assertIn(frame_cache.local_name(photo), self.cached())


class TestPermanentFailureBackoff(PermanentFailureCase):
    def bad_window(self):
        self.publish(["a.jpg", "bad.jpg"])
        self.server.fixture["bodies"]["bad.jpg"] = image_bytes("TIFF")

    def test_no_redownload_every_tick_then_exponential_retry(self):
        self.bad_window()
        frame_sync.main()
        self.assertEqual(len(self.photo_requests("bad.jpg")), 1)
        (record,) = self.failures().values()
        first_delay = record["retry_at"] - self.clock[0]
        self.assertGreater(first_delay, 0)
        # Several ticks: 304, same-signature, and a re-signed manifest.
        for tick in range(3):
            self.clock[0] += first_delay / 4
            if tick == 1:
                state = self.state(); state.pop("etag"); self.state_file.write_text(json.dumps(state))
            if tick == 2:
                self.server.fixture["manifest"]["generated_at"] = "2026-09-19T00:00:00Z"
            frame_sync.main()
        self.assertEqual(len(self.photo_requests("bad.jpg")), 1, "re-downloaded inside backoff")
        self.clock[0] = record["retry_at"] + 1
        frame_sync.main()
        self.assertEqual(len(self.photo_requests("bad.jpg")), 2, "backoff expiry must retry")
        (second,) = self.failures().values()
        self.assertEqual(second["attempts"], 2)
        self.assertGreater(second["retry_at"] - self.clock[0], first_delay)
        self.clock[0] = 10 ** 10
        for _ in range(12):
            frame_sync.main()
            (rec,) = self.failures().values()
            self.assertLessEqual(rec["retry_at"] - self.clock[0], 24 * 3600 + 1)
            self.clock[0] = rec["retry_at"] + 1

    def test_changed_etag_clears_the_record_and_downloads(self):
        self.bad_window()
        frame_sync.main()
        self.assertEqual(len(self.failures()), 1)
        entry = self.entry("bad.jpg")
        entry["etag"] = "fixed"
        self.server.fixture["bodies"]["bad.jpg"] = tiny_image(77)
        frame_sync.main()
        self.assertEqual(self.failures(), {})
        self.assertEqual((self.images / frame_cache.local_name(entry)).read_bytes(), tiny_image(77))
        self.assertEqual(json.loads(self.local_manifest.read_text()), self.server.fixture["manifest"])

    def test_oversized_download_is_aborted_and_becomes_a_hole(self):
        for no_length in (False, True):
            with self.subTest(no_length=no_length):
                self.publish([f"{no_length}/a.jpg", f"{no_length}/huge.jpg"])
                huge = f"{no_length}/huge.jpg"
                self.server.fixture["bodies"][huge] = image_bytes("PNG", (64, 64), noise=True)
                if no_length:
                    self.server.fixture["no_length"] = {huge}
                with unittest.mock.patch.object(frame_sync, "MAX_PHOTO_BYTES", 2000, create=True):
                    frame_sync.main()
                self.assertEqual(json.loads(self.local_manifest.read_text()), self.server.fixture["manifest"])
                self.assertNotIn(frame_cache.local_name(self.entry(huge)), self.cached())
                self.assertEqual(list(self.staging.iterdir()), [])
                self.assertEqual(len(self.failures()), 1)
                self.assertIn("size", next(iter(self.failures().values()))["reason"])

    def test_default_size_cap_is_about_fifty_megabytes(self):
        self.assertEqual(getattr(frame_sync, "MAX_PHOTO_BYTES", None), 50 * 1024 * 1024)


class TestResourceBounds(PermanentFailureCase):
    def test_memory_error_propagates_instead_of_counting_as_corruption(self):
        self.publish(["old.jpg"])
        frame_sync.main()
        prior = self.local_manifest.read_bytes()
        self.publish(["new.jpg"])
        from PIL import ImageFile
        with unittest.mock.patch.object(ImageFile.ImageFile, "load", side_effect=MemoryError):
            with self.assertRaises(MemoryError):
                frame_sync.main()
        self.assertEqual(self.local_manifest.read_bytes(), prior)
        self.assertEqual(self.failures(), {})

    def test_cache_stays_bounded_under_persistent_failure(self):
        self.publish([f"good/{n}.jpg" for n in range(3)])
        frame_sync.main()
        last_good = frame_cache.wanted_names(self.server.fixture["manifest"]["photos"])
        for day in range(5):
            keys = [f"day{day}/{n}.jpg" for n in range(3)]
            self.publish(keys, broken=[keys[-1]])
            frame_sync.main()
            current = frame_cache.wanted_names(self.server.fixture["manifest"]["photos"])
            self.assertTrue(last_good <= self.cached(), "last-good window was pruned")
            self.assertLessEqual(self.cached(), last_good | current)
        self.assertEqual(len(self.cached()), 5)

    def test_failure_prune_keeps_legacy_files_the_last_good_manifest_uses(self):
        self.images.mkdir(parents=True)
        (self.images / "a.jpg").write_bytes(tiny_image(1))
        legacy = {"schema": 2, "mode": "sync", "start_epoch": 1, "slide_seconds": 5,
                  "photos": [{"id": "a.jpg", "key": "photos/a.jpg", "etag": "x"}]}
        self.local_manifest.write_text(json.dumps(legacy))
        self.publish(["new/a.jpg", "new/b.jpg"], broken=["new/b.jpg"])
        frame_sync.main()
        self.assertIn("a.jpg", self.cached())
        self.assertEqual(json.loads(self.local_manifest.read_text()), legacy)

    def test_304_and_same_signature_paths_do_not_decode_unchanged_files(self):
        self.publish([f"photos/{n}.jpg" for n in range(4)])
        frame_sync.main()
        with unittest.mock.patch.object(frame_cache.Image, "open", wraps=Image.open) as opened:
            frame_sync.main()  # 304
        self.assertLessEqual(opened.call_count, 2, "at most one rolling re-validation per tick")
        state = self.state(); state.pop("etag"); self.state_file.write_text(json.dumps(state))
        with unittest.mock.patch.object(frame_cache.Image, "open", wraps=Image.open) as opened:
            frame_sync.main()  # same signature
        self.assertLessEqual(opened.call_count, 2)

    def test_rolling_revalidation_eventually_repairs_same_size_bitrot(self):
        self.publish([f"photos/{n}.jpg" for n in range(3)])
        frame_sync.main()
        manifest = json.loads(self.local_manifest.read_text())
        target = self.images / frame_cache.local_name(manifest["photos"][1])
        stat = target.stat()
        target.write_bytes(b"\x00" * stat.st_size)
        import os
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        for _ in range(4):
            frame_sync.main()
        self.assertEqual(target.read_bytes(), self.server.fixture["bodies"]["photos/1.jpg"])


def large_png_header(width, height):
    """Valid PNG header/IHDR claiming huge dimensions; a few dozen bytes."""
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(b"\x00" * 16)) + chunk(b"IEND", b""))


class SimulatedOOMKill(BaseException):
    """Stands in for SIGKILL: no `except Exception`/finally-style cleanup sees it
    as a decode error, so whatever was persisted before the decode survives."""


class TestOversizedAndCrashMarker(PermanentFailureCase):
    def test_oversized_non_jpeg_download_is_a_hole(self):
        self.publish(["a.jpg", "big.png"])
        self.server.fixture["bodies"]["big.png"] = large_png_header(8000, 6000)
        frame_sync.main()
        self.assertEqual(json.loads(self.local_manifest.read_text()), self.server.fixture["manifest"])
        (record,) = self.failures().values()
        self.assertIn("pixels", record["reason"])
        self.assertNotIn(frame_cache.local_name(self.entry("big.png")), self.cached())

    def test_marker_is_persisted_before_decode_and_cleared_after(self):
        self.publish(["a.jpg"])
        seen = []
        real = frame_sync.frame_cache.check_image

        def spy(path):
            seen.append(self.state().get("validating"))
            return real(path)
        with unittest.mock.patch.object(frame_sync.frame_cache, "check_image", spy):
            frame_sync.main()
        self.assertTrue(seen and all(seen), seen)
        self.assertEqual(seen[0]["fid"], frame_sync.failure_id(self.entry("a.jpg")))
        self.assertNotIn("validating", self.state())

    def test_two_decode_crashes_escalate_to_a_permanent_hole(self):
        self.publish(["old.jpg"])
        frame_sync.main()
        self.publish(["a.jpg", "killer.png"])
        killer = frame_cache.local_name(self.entry("killer.png"))
        real = frame_sync.frame_cache.check_image
        decoded = []

        def oom_on_killer(path):
            decoded.append(Path(path).name)
            if Path(path).name.startswith(killer):
                raise SimulatedOOMKill()
            return real(path)
        for run in range(2):
            with unittest.mock.patch.object(frame_sync.frame_cache, "check_image", oom_on_killer):
                with self.assertRaises(SimulatedOOMKill):
                    frame_sync.main()
            # The process "died": the marker is still on disk for the next boot.
            self.assertEqual(self.state()["validating"]["name"], killer, run)
            self.assertEqual(self.failures(), {}, "one crash must not record a hole yet" if run == 0 else "")
        decoded.clear()
        with unittest.mock.patch.object(frame_sync.frame_cache, "check_image", oom_on_killer):
            frame_sync.main()
        self.assertFalse(any(n.startswith(killer) for n in decoded), "escalated file decoded again")
        state = self.state()
        self.assertNotIn("validating", state)
        self.assertIn(frame_sync.failure_id(self.entry("killer.png")), state["permanent_failures"])
        self.assertEqual(json.loads(self.local_manifest.read_text()), self.server.fixture["manifest"])
        viewer = _load("viewer")
        viewer.MANIFEST_FILE = self.local_manifest
        self.assertEqual([viewer.resolve_image(self.images, p) is None
                          for p in viewer.load_manifest_if_changed()["photos"]], [False, True])

    def test_single_crash_then_success_clears_the_count(self):
        self.publish(["a.jpg"])
        name = frame_cache.local_name(self.entry("a.jpg"))
        fid = frame_sync.failure_id(self.entry("a.jpg"))
        self.state_file.write_text(json.dumps({"validating": {"name": name, "fid": fid}}))
        frame_sync.main()
        state = self.state()
        self.assertEqual(state.get("decode_crashes", {}), {})
        self.assertEqual(state.get("permanent_failures"), {})
        self.assertIn(name, self.cached())

    def test_rolling_revalidation_crashes_never_delete_a_playing_file(self):
        """M-a: markers left mid-re-validation (OOM kill, SIGTERM at shutdown,
        power cut) on a file the last-good manifest plays, whose recorded
        size/mtime still match. Deleting it would punch a hole into a show
        that was working; it is dropped from re-validation instead."""
        self.publish(["a.jpg", "b.jpg"])
        frame_sync.main()
        prior = self.local_manifest.read_bytes()
        entry = self.entry("b.jpg")
        name, fid = frame_cache.local_name(entry), frame_sync.failure_id(entry)
        body = (self.images / name).read_bytes()
        for _ in range(2):
            state = self.state()
            # What a run killed mid-re-validation of b leaves on disk: the
            # marker and the cursor already pointing at b.
            state["validating"] = {"name": name, "fid": fid}
            state["revalidate_cursor"] = name
            self.state_file.write_text(json.dumps(state))
            frame_sync.main()
        self.assertNotIn(fid, self.failures(), "a validated playing file is not a hole")
        self.assertIn(name, self.cached(), "playing file was deleted")
        self.assertEqual((self.images / name).read_bytes(), body)
        self.assertIn(name, self.state().get("validated", {}))
        self.assertEqual(self.local_manifest.read_bytes(), prior)
        self.assertEqual(self.state().get("decode_crashes", {}), {})
        # ...and it is never fully decoded again by the rolling re-validation.
        decoded = []
        real = frame_sync.frame_cache.check_image

        def spy(path):
            decoded.append(Path(path).name)
            return real(path)
        requests_before = len(self.photo_requests("b.jpg"))
        with unittest.mock.patch.object(frame_sync.frame_cache, "check_image", spy):
            for _ in range(4):
                frame_sync.main()
        self.assertNotIn(name, decoded)
        self.assertIn(frame_cache.local_name(self.entry("a.jpg")), decoded, "rotation continues for others")
        self.assertEqual(len(self.photo_requests("b.jpg")), requests_before)
        self.assertIn(name, self.cached())

    def test_crash_on_a_changed_last_good_file_still_escalates(self):
        """Size/mtime no longer match the record: the file is not the bytes
        that were validated, so the normal escalation (hole + delete) applies."""
        self.publish(["a.jpg", "b.jpg"])
        frame_sync.main()
        entry = self.entry("b.jpg")
        name, fid = frame_cache.local_name(entry), frame_sync.failure_id(entry)
        (self.images / name).write_bytes(b"\x00" * 7)  # different size: stamp mismatch
        state = self.state()
        # Second consecutive death: one already counted, marker still on disk.
        state["validating"] = {"name": name, "fid": fid}
        state["decode_crashes"] = {fid: 1}
        self.state_file.write_text(json.dumps(state))
        frame_sync.main()
        self.assertIn(fid, self.failures())
        self.assertNotIn(name, self.cached())
        self.assertNotIn(name, self.state().get("validated", {}))

    def test_wrong_clock_far_future_retry_is_treated_as_expired(self):
        # M3: a record written while the clock was far ahead must not pin a hole.
        self.publish(["a.jpg", "bad.jpg"])
        self.server.fixture["bodies"]["bad.jpg"] = image_bytes("TIFF")
        frame_sync.main()
        self.assertEqual(len(self.photo_requests("bad.jpg")), 1)
        state = self.state()
        (fid,) = state["permanent_failures"]
        state["permanent_failures"][fid]["retry_at"] = self.clock[0] + 30 * 24 * 3600
        self.state_file.write_text(json.dumps(state))
        self.server.fixture["bodies"]["bad.jpg"] = tiny_image(5)
        frame_sync.main()
        self.assertEqual(len(self.photo_requests("bad.jpg")), 2)
        self.assertEqual(self.failures(), {})


def progressive_jpeg(size=(6000, 4000)):
    """Real Pillow progressive 4:4:4 JPEG; 24 MP needs ~144 MB of coefficients."""
    output = io.BytesIO()
    Image.new("RGB", size, (30, 90, 160)).save(output, "JPEG", quality=80,
                                                progressive=True, subsampling=0)
    return output.getvalue()


class TestProgressiveJpegDownload(PermanentFailureCase):
    def test_large_progressive_jpeg_is_a_permanent_hole_not_a_decode(self):
        self.publish(["a.jpg", "prog.jpg"])
        self.server.fixture["bodies"]["prog.jpg"] = progressive_jpeg()
        frame_sync.main()
        self.assertEqual(json.loads(self.local_manifest.read_text()), self.server.fixture["manifest"])
        (record,) = self.failures().values()
        self.assertIn("progressive", record["reason"])
        self.assertNotIn(frame_cache.local_name(self.entry("prog.jpg")), self.cached())
        self.assertIn(frame_cache.local_name(self.entry("a.jpg")), self.cached())


class TestStatePersistence(PermanentFailureCase):
    """I2: a failed state write (ENOSPC/EIO) is not evidence of a corrupt photo."""

    def full_disk(self, when=lambda value: True):
        real = frame_sync.atomic_json

        def write(path, value):
            if Path(path) == self.state_file and when(value):
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, value)
        return unittest.mock.patch.object(frame_sync, "atomic_json", write)

    def test_rolling_revalidation_keeps_the_file_when_state_cannot_be_saved(self):
        self.publish(["a.jpg", "b.jpg"])
        frame_sync.main()
        before = self.cached()
        state = self.state()
        with self.full_disk(), self.assertRaises(frame_sync.StatePersistError):
            frame_sync.rolling_revalidate(state)
        self.assertEqual(self.cached(), before)

    def test_download_validation_with_unwritable_state_is_not_a_hole(self):
        frame_sync.ensure_dirs()
        self.publish(["a.jpg"])
        tmp = self.staging / "x.jpg"
        for label, when in (("marker", lambda v: "validating" in v),
                            ("clear", lambda v: "validating" not in v)):
            with self.subTest(label), self.full_disk(when):
                with self.assertRaises(frame_sync.StatePersistError):
                    frame_sync.download_to(tmp, self.entry("a.jpg")["url"],
                                           validate=lambda p: frame_sync.guarded_check({}, p, "x.jpg", "fid"))
            self.assertFalse(tmp.exists())
            self.assertEqual(list(self.staging.iterdir()), [])

    def test_main_aborts_without_promotion_prune_or_hole(self):
        self.publish(["old/a.jpg", "old/b.jpg"])
        frame_sync.main()
        last_good = self.local_manifest.read_bytes()
        old_files = self.cached()
        new_keys = ["new/a.jpg", "new/b.jpg"]
        self.publish(new_keys)
        new_names = {frame_cache.local_name(self.entry(k)) for k in new_keys}
        armed = {"on": False}

        def any_write(value):
            return True

        def during_download(value):
            return (value.get("validating") or {}).get("name") in new_names

        def clear_after_download(value):
            # Let the marker for a new file land, fail the write that clears it.
            marker = (value.get("validating") or {}).get("name")
            if marker in new_names:
                armed["on"] = True
                return False
            return armed["on"]

        for label, when in (("revalidation", any_write), ("download marker", during_download),
                            ("download clear", clear_after_download)):
            with self.subTest(label):
                armed["on"] = False
                before = self.state()
                with self.full_disk(when), self.assertRaises(SystemExit) as caught:
                    frame_sync.main()
                self.assertNotIn(caught.exception.code, (0, None))
                self.assertEqual(self.local_manifest.read_bytes(), last_good)
                self.assertTrue(old_files <= self.cached(), "last-good files were pruned")
                after = self.state()
                for field in ("etag", "sig", "last_sync"):
                    self.assertEqual(after.get(field), before.get(field), field)
                self.assertTrue(set(before.get("validated", {})) <= set(after.get("validated", {})))
                self.assertEqual(self.failures(), {})
        # Disk freed: the next run promotes normally.
        frame_sync.main()
        self.assertEqual(json.loads(self.local_manifest.read_text()), self.server.fixture["manifest"])
        self.assertTrue(new_names <= self.cached())
        self.assertEqual(self.failures(), {})

    def test_every_state_save_exits_cleanly_on_a_full_disk(self):
        """Early-return and end-of-run saves: StatePersistError -> exit 1, no traceback."""
        import urllib.error

        def full(label, setup=lambda: None):
            with self.subTest(label):
                setup()
                with unittest.mock.patch.object(frame_sync, "rolling_revalidate", lambda state: None), \
                        self.full_disk(), self.assertRaises(SystemExit) as caught:
                    frame_sync.main()
                self.assertEqual(caught.exception.code, 1)

        def http_error(code):
            def raise_it(*args, **kwargs):
                raise urllib.error.HTTPError("u", code, "x", {}, None)
            return raise_it

        def serve(value):
            self.server.fixture["manifest"] = value

        # Fresh device: nothing validated, so no crash-marker write precedes these.
        with unittest.mock.patch.object(frame_sync, "fetch_bytes", http_error(500)):
            full("HTTP error")
        with unittest.mock.patch.object(frame_sync, "fetch_bytes",
                                        unittest.mock.Mock(side_effect=urllib.error.URLError("offline"))):
            full("offline")
        with unittest.mock.patch.object(frame_sync, "fetch_bytes", lambda *a, **k: (b"{not json", None)):
            full("invalid JSON")
        full("invalid manifest", lambda: serve({"schema": 2, "photos": []}))
        self.publish(["nourl.jpg"])
        self.server.fixture["manifest"]["photos"][0].pop("url")
        full("end of run (after promote/prune)")
        self.assertFalse(self.local_manifest.exists(), "an all-hole window never promotes")
        # Same window already playable: 304 and unchanged-signature paths.
        self.publish(["a.jpg"])
        frame_sync.main()
        full("304, window verified")
        state = self.state()
        state.pop("etag")
        self.state_file.write_text(json.dumps(state))
        full("signature unchanged")
        # Disk freed: a normal run still works and nothing was damaged.
        frame_sync.main()
        self.assertEqual(json.loads(self.local_manifest.read_text()), self.server.fixture["manifest"])


class TestSystemdUnits(unittest.TestCase):
    def test_sync_service_is_the_preferred_oom_victim(self):
        unit = (PI_ZERO / "systemd" / "frame-sync.service").read_text()
        self.assertIn("OOMScoreAdjust=500", unit.splitlines())


class TestViewerResolution(unittest.TestCase):
    """The real viewer and Pillow decoder, with no framebuffer hardware."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.viewer = _load("viewer")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.images = Path(self.tmp.name)

    def test_render_decode_failure_keeps_framebuffer_and_recovers(self):
        good = self.images / "good.png"
        bad = self.images / "bad.png"
        good.write_bytes(tiny_image())
        bad.write_bytes(b"malformed image")
        fb = io.BytesIO(b"last displayed image")
        info = {"xres": 3, "yres": 2, "bpp": 32}
        self.assertFalse(self.viewer.render_safely(bad, info, fb, {}))
        self.assertEqual(fb.getvalue(), b"last displayed image")
        self.assertTrue(self.viewer.render_safely(good, info, fb, {}))
        self.assertNotEqual(fb.getvalue(), b"last displayed image")

    def test_bad_manifest_read_retains_last_good_and_retries(self):
        path = self.images / "manifest.json"
        good = {"mode": "sync", "start_epoch": 100, "slide_seconds": 10,
                "photos": [{"id": "a.jpg"}]}
        with unittest.mock.patch.object(self.viewer, "MANIFEST_FILE", path):
            self.viewer.manifest = None
            self.viewer.manifest_mtime = 0
            path.write_text(json.dumps(good))
            self.assertEqual(self.viewer.load_manifest_if_changed(), good)
            for invalid in ('{', '[]', '{"photos": [null]}',
                            '{"photos": [{"id":"a"}], "slide_seconds": "bad"}',
                            '{"photos": [{"id":"a"}], "start_epoch": NaN}'):
                path.write_text(invalid)
                self.assertEqual(self.viewer.load_manifest_if_changed(), good)
            path.unlink()
            self.assertEqual(self.viewer.load_manifest_if_changed(), good)
            path.write_text(json.dumps({**good, "start_epoch": 200}))
            self.assertEqual(self.viewer.load_manifest_if_changed()["start_epoch"], 200)

    def test_prefers_the_key_derived_name(self) -> None:
        entry = {"id": "a.jpg", "key": "photos/sub/a.jpg"}
        (self.images / frame_cache.local_name(entry)).write_bytes(tiny_image(2))
        (self.images / "a.jpg").write_bytes(tiny_image(1))

        found = self.viewer.resolve_image(self.images, entry)

        self.assertEqual(found.read_bytes(), tiny_image(2))

    def test_falls_back_to_the_legacy_name_before_the_first_sync(self) -> None:
        entry = {"id": "a.jpg", "key": "photos/sub/a.jpg"}
        (self.images / "a.jpg").write_bytes(tiny_image(1))

        found = self.viewer.resolve_image(self.images, entry)

        self.assertEqual(found.read_bytes(), tiny_image(1))

    def test_reads_pre_etag_hashed_cache_during_migration(self):
        entry = {"id": "a.jpg", "key": "photos/sub/a.jpg", "etag": "new"}
        old_name = frame_cache.local_name({"id": "a.jpg", "key": "photos/sub/a.jpg"})
        (self.images / old_name).write_bytes(tiny_image(3))
        self.assertEqual(self.viewer.resolve_image(self.images, entry).read_bytes(), tiny_image(3))

    def test_uncached_entry_returns_none_rather_than_another_photo(self) -> None:
        (self.images / "something-else.jpg").write_bytes(tiny_image(4))
        entry = {"id": "missing.jpg", "key": "photos/missing.jpg"}
        self.assertIsNone(self.viewer.resolve_image(self.images, entry))

    def test_unusable_entry_returns_none(self) -> None:
        for entry in ({}, None, "nope", {"id": ""}):
            self.assertIsNone(self.viewer.resolve_image(self.images, entry), entry)


if __name__ == "__main__":
    unittest.main()
