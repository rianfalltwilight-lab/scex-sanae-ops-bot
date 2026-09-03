import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from player_bindings import PlayerBindings


class PlayerBindingTests(unittest.TestCase):
    def test_bind_rebind_persist_and_unbind(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "binds.json")
            store = PlayerBindings(path)
            self.assertEqual(store.bind("1", "Steve")[0], True)
            self.assertEqual(store.bind("2", "Steve")[0], False)
            self.assertEqual(store.bind("1", "Alex")[0], True)
            restored = PlayerBindings(path)
            self.assertEqual(restored.get_qq("1")["name"], "Alex")
            self.assertEqual(restored.unbind("1")[0], True)
            self.assertIsNone(restored.get_qq("1"))

    def test_name_validation_and_seen_gate(self):
        store = PlayerBindings("/tmp/not-used-binds.json", require_seen=True)
        self.assertFalse(store.bind("1", "bad name")[0])
        self.assertFalse(store.bind("1", "Steve", seen=False)[0])


if __name__ == "__main__":
    unittest.main()
