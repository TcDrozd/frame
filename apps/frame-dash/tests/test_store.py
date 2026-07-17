import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shared import store  # noqa: E402


class FakeTable:
    """Minimal in-memory stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self):
        self.items = {}

    @staticmethod
    def _key(key):
        return (key["pk"], key["sk"])

    def put_item(self, Item):
        self.items[(Item["pk"], Item["sk"])] = dict(Item)
        return {}

    def get_item(self, Key):
        item = self.items.get(self._key(Key))
        return {"Item": dict(item)} if item else {}

    def delete_item(self, Key):
        self.items.pop(self._key(Key), None)
        return {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues):
        # Supports only the single "SET playlist_id = :pid" expression store.py uses.
        assert UpdateExpression == "SET playlist_id = :pid"
        item = self.items.setdefault(self._key(Key), {"pk": Key["pk"], "sk": Key["sk"]})
        item["playlist_id"] = ExpressionAttributeValues[":pid"]
        return {}

    def query(self, KeyConditionExpression, ExclusiveStartKey=None):
        # store.py only ever queries by pk equality.
        pk = KeyConditionExpression._values[1]
        matches = [dict(v) for (p, _), v in self.items.items() if p == pk]
        return {"Items": matches}


class TestPlaylistCrud(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()

    def test_create_defaults(self):
        pl = store.create_playlist(self.table, "  Family Favorites  ")
        self.assertEqual(pl["name"], "Family Favorites")
        self.assertEqual(pl["items"], [])
        self.assertEqual(pl["settings"]["mode"], "sync")
        self.assertEqual(pl["settings"]["start_epoch"], "now")
        self.assertTrue(pl["id"])
        self.assertNotIn("pk", pl)

    def test_create_requires_name(self):
        with self.assertRaises(ValueError):
            store.create_playlist(self.table, "   ")

    def test_get_roundtrip(self):
        pl = store.create_playlist(self.table, "Trip")
        fetched = store.get_playlist(self.table, pl["id"])
        self.assertEqual(fetched["name"], "Trip")
        self.assertIsNone(store.get_playlist(self.table, "nope"))

    def test_update_items_full_replace_preserves_order(self):
        pl = store.create_playlist(self.table, "Trip")
        keys = ["photos/c.jpg", "photos/a.jpg", "photos/b.jpg"]
        updated = store.update_playlist(self.table, pl["id"], items=keys)
        self.assertEqual(updated["items"], keys)
        reordered = list(reversed(keys))
        updated = store.update_playlist(self.table, pl["id"], items=reordered)
        self.assertEqual(updated["items"], reordered)

    def test_update_settings_merges_and_validates(self):
        pl = store.create_playlist(self.table, "Trip")
        updated = store.update_playlist(self.table, pl["id"], settings={"slide_seconds": 60})
        self.assertEqual(updated["settings"]["slide_seconds"], 60)
        self.assertEqual(updated["settings"]["mode"], "sync")
        with self.assertRaises(ValueError):
            store.update_playlist(self.table, pl["id"], settings={"mode": "party"})

    def test_update_missing_returns_none(self):
        self.assertIsNone(store.update_playlist(self.table, "nope", name="x"))

    def test_item_cap(self):
        pl = store.create_playlist(self.table, "Big")
        too_many = [f"photos/{i}.jpg" for i in range(store.MAX_ITEMS + 1)]
        with self.assertRaises(ValueError):
            store.update_playlist(self.table, pl["id"], items=too_many)

    def test_list_marks_active(self):
        a = store.create_playlist(self.table, "A")
        b = store.create_playlist(self.table, "B")
        store.set_active(self.table, b["id"])
        summaries = store.list_playlists(self.table)
        by_id = {s["id"]: s for s in summaries}
        self.assertFalse(by_id[a["id"]]["is_active"])
        self.assertTrue(by_id[b["id"]]["is_active"])

    def test_delete_clears_active_pointer(self):
        pl = store.create_playlist(self.table, "A")
        store.set_active(self.table, pl["id"])
        store.delete_playlist(self.table, pl["id"])
        self.assertIsNone(store.get_active(self.table))
        self.assertIsNone(store.get_playlist(self.table, pl["id"]))


class TestActivePointer(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()

    def test_record_publish_freezes_epoch(self):
        store.record_publish(
            self.table,
            "pid1",
            manifest_key="manifest.dev.json",
            version="v20260610-120000Z",
            resolved_start_epoch=1750000000,
        )
        active = store.get_active(self.table)
        self.assertEqual(active["playlist_id"], "pid1")
        self.assertEqual(active["resolved_start_epoch"], 1750000000)
        self.assertEqual(active["last_version"], "v20260610-120000Z")

    def test_record_publish_inventory_has_no_epoch(self):
        store.record_publish(
            self.table, "pid1", manifest_key="m.json", version="v1", resolved_start_epoch=None
        )
        active = store.get_active(self.table)
        self.assertNotIn("resolved_start_epoch", active)


class TestJsonable(unittest.TestCase):
    def test_decimal_conversion(self):
        data = {"a": Decimal("5"), "b": Decimal("1.5"), "c": [Decimal("2")], "d": {"e": Decimal("3")}}
        out = store.to_jsonable(data)
        self.assertEqual(out, {"a": 5, "b": 1.5, "c": [2], "d": {"e": 3}})
        self.assertIsInstance(out["a"], int)
        self.assertIsInstance(out["b"], float)


if __name__ == "__main__":
    unittest.main()
