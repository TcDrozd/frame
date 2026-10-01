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
import importlib.util
import json
import sys
import tempfile
import threading
import types
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
        if self.path.startswith("/manifest.json"):
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
        self.send_header("Content-Length", str(len(body)))
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
            bodies[key] = f"photo-body-{key}".encode() * (i + 1)
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
        name = frame_cache.local_name({"key": "photos/a.jpg"})
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
        for key in keys:
            name = frame_cache.local_name({"key": key})
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
        (self.images / "a.jpg").write_bytes(b"legacy")
        (self.images / "stale-from-january.jpg").write_bytes(b"legacy")

        self.publish(["photos/a.jpg"])
        frame_sync.main()

        self.assertEqual(self.cached(), {frame_cache.local_name({"key": "photos/a.jpg"})})

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


class TestViewerResolution(unittest.TestCase):
    """The viewer half of the naming agreement.

    viewer.py imports Pillow, which is an apt package on the device and not a
    test dependency here; only the framebuffer paths need it, so it is stubbed
    to let the pure path-resolution logic be imported and exercised.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if "PIL" not in sys.modules:
            image_mod = types.ModuleType("PIL.Image")
            image_mod.Image = type("Image", (), {})  # annotation target only
            image_mod.LANCZOS = 1
            pil = types.ModuleType("PIL")
            pil.Image = image_mod
            sys.modules["PIL"] = pil
            sys.modules["PIL.Image"] = image_mod
            cls.addClassCleanup(sys.modules.pop, "PIL.Image", None)
            cls.addClassCleanup(sys.modules.pop, "PIL", None)
        cls.viewer = _load("viewer")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.images = Path(self.tmp.name)

    def test_prefers_the_key_derived_name(self) -> None:
        entry = {"id": "a.jpg", "key": "photos/sub/a.jpg"}
        (self.images / frame_cache.local_name(entry)).write_bytes(b"new")
        (self.images / "a.jpg").write_bytes(b"legacy")

        found = self.viewer.resolve_image(self.images, entry)

        self.assertEqual(found.read_bytes(), b"new")

    def test_falls_back_to_the_legacy_name_before_the_first_sync(self) -> None:
        entry = {"id": "a.jpg", "key": "photos/sub/a.jpg"}
        (self.images / "a.jpg").write_bytes(b"legacy")

        found = self.viewer.resolve_image(self.images, entry)

        self.assertEqual(found.read_bytes(), b"legacy")

    def test_uncached_entry_returns_none_rather_than_another_photo(self) -> None:
        (self.images / "something-else.jpg").write_bytes(b"x")
        entry = {"id": "missing.jpg", "key": "photos/missing.jpg"}
        self.assertIsNone(self.viewer.resolve_image(self.images, entry))

    def test_unusable_entry_returns_none(self) -> None:
        for entry in ({}, None, "nope", {"id": ""}):
            self.assertIsNone(self.viewer.resolve_image(self.images, entry), entry)


if __name__ == "__main__":
    unittest.main()
