"""Cache naming and pruning for the Pi Zero client.

The device is hand-deployed and hard to reach, and both failures these cover
are silent from the couch — a wrong photo pinned in the cache forever, or an
SD card filling up over months — so they are pinned down here instead.

apps/pi-zero-client is a directory of plain scripts, not a package: the
modules are loaded by path so the tests run from the repo root like the rest.
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

PI_ZERO = Path(__file__).resolve().parents[1] / "apps" / "pi-zero-client"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, PI_ZERO / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


frame_cache = _load("frame_cache")


class TestLocalName(unittest.TestCase):
    def test_same_basename_different_folders_get_different_names(self) -> None:
        """The bug this module exists for: basenames collide, keys do not."""
        a = frame_cache.local_name({"id": "IMG_0274.jpeg", "key": "photos/IMG_0274.jpeg"})
        b = frame_cache.local_name(
            {"id": "IMG_0274.jpeg", "key": "photos/04_phone_modern/IMG_0274.jpeg"}
        )
        self.assertNotEqual(a, b)
        self.assertTrue(a.endswith("_IMG_0274.jpeg"))
        self.assertTrue(b.endswith("_IMG_0274.jpeg"))

    def test_name_is_stable_across_url_refreshes(self) -> None:
        """Presigned URLs are re-signed every publish; the cache must not churn."""
        key = "photos/01_scans/2008/SCAN0060.JPG"
        first = frame_cache.local_name({"id": "SCAN0060.JPG", "key": key, "url": "https://x/a?sig=1"})
        second = frame_cache.local_name({"id": "SCAN0060.JPG", "key": key, "url": "https://x/a?sig=2"})
        self.assertEqual(first, second)

    def test_schema_1_entry_falls_back_to_id(self) -> None:
        name = frame_cache.local_name({"id": "SCAN0060.JPG"})
        self.assertIsNotNone(name)
        self.assertEqual(name, frame_cache.local_name({"id": "SCAN0060.JPG"}))

    def test_key_wins_over_id(self) -> None:
        keyed = frame_cache.local_name({"id": "x.jpg", "key": "photos/a/x.jpg"})
        id_only = frame_cache.local_name({"id": "x.jpg"})
        self.assertNotEqual(keyed, id_only)

    def test_unusable_entries_return_none(self) -> None:
        for entry in ({}, {"id": ""}, {"id": "   ", "key": ""}, None, "nope"):
            self.assertIsNone(frame_cache.local_name(entry))

    def test_name_can_never_escape_the_images_directory(self) -> None:
        hostile = [
            {"key": "../../etc/passwd"},
            {"key": "photos/../../../root/.ssh/authorized_keys"},
            {"id": "/etc/shadow"},
            {"key": "photos/a/"},
            {"id": ".."},
        ]
        for entry in hostile:
            name = frame_cache.local_name(entry)
            self.assertIsNotNone(name, entry)
            self.assertNotIn("/", name, entry)
            self.assertNotIn("\\", name, entry)
            self.assertNotIn("..", name, entry)
            resolved = (Path("/opt/frame/images") / name).resolve()
            self.assertEqual(resolved.parent, Path("/opt/frame/images"))

    def test_extension_survives_a_very_long_basename(self) -> None:
        long_name = "x" * 300 + ".jpeg"
        name = frame_cache.local_name({"key": f"photos/{long_name}"})
        self.assertTrue(name.endswith(".jpeg"), name)
        self.assertLess(len(name), 80)

    def test_non_ascii_basename_is_transliterated_to_safe_chars(self) -> None:
        name = frame_cache.local_name({"key": "photos/café ☕.jpg"})
        self.assertTrue(name.isascii(), name)
        self.assertTrue(name.endswith(".jpg"), name)


class TestLegacyName(unittest.TestCase):
    def test_returns_id_unchanged(self) -> None:
        self.assertEqual(frame_cache.legacy_name({"id": "IMG_0274.jpeg"}), "IMG_0274.jpeg")

    def test_refuses_path_separators_and_dots(self) -> None:
        for pid in ("../x.jpg", "a/b.jpg", "a\\b.jpg", ".", "..", ""):
            self.assertIsNone(frame_cache.legacy_name({"id": pid}), pid)


class TestPruneDir(unittest.TestCase):
    def test_removes_unclaimed_files_and_reports_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "keep.jpg").write_bytes(b"a" * 10)
            (root / "drop.jpg").write_bytes(b"b" * 25)
            (root / "also-drop.jpg").write_bytes(b"c" * 5)

            removed, freed = frame_cache.prune_dir(root, {"keep.jpg"})

            self.assertEqual(removed, 2)
            self.assertEqual(freed, 30)
            self.assertEqual({p.name for p in root.iterdir()}, {"keep.jpg"})

    def test_leaves_subdirectories_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / "sub" / "inner.jpg").write_bytes(b"x")

            removed, freed = frame_cache.prune_dir(root, set())

            self.assertEqual((removed, freed), (0, 0))
            self.assertTrue((root / "sub" / "inner.jpg").exists())

    def test_missing_directory_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(frame_cache.prune_dir(Path(tmp) / "nope", set()), (0, 0))

    def test_empty_keep_set_clears_the_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.tmp").write_bytes(b"1")
            (root / "b.tmp").write_bytes(b"2")
            self.assertEqual(frame_cache.prune_dir(root, set()), (2, 2))
            self.assertEqual(list(root.iterdir()), [])


class TestWantedNames(unittest.TestCase):
    def test_skips_unusable_entries(self) -> None:
        names = frame_cache.wanted_names(
            [{"key": "photos/a.jpg"}, {}, {"id": "b.jpg"}, {"id": ""}]
        )
        self.assertEqual(len(names), 2)

    def test_empty_and_none_are_empty_sets(self) -> None:
        self.assertEqual(frame_cache.wanted_names([]), set())
        self.assertEqual(frame_cache.wanted_names(None), set())


if __name__ == "__main__":
    unittest.main()
