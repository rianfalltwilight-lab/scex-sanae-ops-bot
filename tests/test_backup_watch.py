#!/usr/bin/env python3
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["SCE_BOT_TEST_MODE"] = "1"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

w = importlib.import_module("backup_watch")


class BackupWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(w, "LOG_DIR", root),
            mock.patch.object(w, "STATE_PATH", root / "watch.json"),
            mock.patch.object(w, "PIPELINE_STATE_PATH", root / "pipeline.json"),
            mock.patch.object(w, "PIPELINE_LOCK_PATH", root / "pipeline.lock"),
            mock.patch.object(w, "WATCH_LOG_PATH", root / "watch.log"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def entries(*names):
        return [{"name": name, "size": 2 * 1024 * 1024, "mtime": i + 1} for i, name in enumerate(names)]

    def make_stable(self, entries):
        with mock.patch.object(w, "_backups", return_value=entries):
            for _ in range(w.STABLE_CHECKS):
                self.assertEqual(w.check_once(), "stabilizing")

    def test_first_run_only_establishes_baseline(self):
        with mock.patch.object(w, "_backups", return_value=self.entries("old.zip")), \
             mock.patch.object(w, "_spawn") as spawn:
            self.assertEqual(w.check_once(), "baseline")
        spawn.assert_not_called()
        self.assertEqual(json.loads(w.STATE_PATH.read_text())["handled"], ["old.zip"])

    def test_new_zip_uses_existing_pipeline_as_scheduled(self):
        w._write_json(w.STATE_PATH, {"initialized": True, "handled": ["old.zip"]})
        entries = self.entries("old.zip", "new.zip")
        self.make_stable(entries)
        with mock.patch.object(w, "_backups", return_value=entries), \
             mock.patch.object(w, "_pipeline_busy", return_value=False), \
             mock.patch.object(w, "_pipeline_already_reported", return_value=False), \
             mock.patch.object(w, "_spawn") as spawn:
            self.assertEqual(w.check_once(), "spawned")
        spawn.assert_called_once_with("new.zip")
        state = json.loads(w.STATE_PATH.read_text())
        self.assertEqual(state["dispatched"], "new.zip")
        self.assertNotIn("new.zip", state["handled"])

    def test_dispatched_zip_is_only_handled_after_pipeline_finishes(self):
        entries = self.entries("old.zip", "new.zip")
        w._write_json(w.STATE_PATH, {"initialized": True, "handled": ["old.zip"],
                                    "dispatched": "new.zip"})
        with mock.patch.object(w, "_backups", return_value=entries), \
             mock.patch.object(w, "_pipeline_busy", return_value=False), \
             mock.patch.object(w, "_pipeline_already_reported", return_value=True), \
             mock.patch.object(w, "_spawn") as spawn:
            self.assertEqual(w.check_once(), "completed")
        spawn.assert_not_called()
        state = json.loads(w.STATE_PATH.read_text())
        self.assertIn("new.zip", state["handled"])
        self.assertIsNone(state["dispatched"])

    def test_manual_pipeline_result_suppresses_duplicate(self):
        w._write_json(w.STATE_PATH, {"initialized": True, "handled": ["old.zip"]})
        entries = self.entries("old.zip", "new.zip")
        self.make_stable(entries)
        with mock.patch.object(w, "_backups", return_value=entries), \
             mock.patch.object(w, "_pipeline_busy", return_value=False), \
             mock.patch.object(w, "_pipeline_already_reported", return_value=True), \
             mock.patch.object(w, "_spawn") as spawn:
            self.assertEqual(w.check_once(), "deduplicated")
        spawn.assert_not_called()

    def test_busy_pipeline_leaves_zip_pending(self):
        w._write_json(w.STATE_PATH, {"initialized": True, "handled": ["old.zip"]})
        entries = self.entries("old.zip", "new.zip")
        self.make_stable(entries)
        with mock.patch.object(w, "_backups", return_value=entries), \
             mock.patch.object(w, "_pipeline_busy", return_value=True), \
             mock.patch.object(w, "_spawn") as spawn:
            self.assertEqual(w.check_once(), "busy")
        spawn.assert_not_called()
        self.assertNotIn("new.zip", json.loads(w.STATE_PATH.read_text())["handled"])

    def test_changing_size_resets_stability(self):
        w._write_json(w.STATE_PATH, {"initialized": True, "handled": ["old.zip"]})
        first = [{"name": "old.zip", "size": 2_000_000, "mtime": 1},
                 {"name": "new.zip", "size": 2_000_000, "mtime": 2}]
        changed = [{"name": "old.zip", "size": 2_000_000, "mtime": 1},
                   {"name": "new.zip", "size": 3_000_000, "mtime": 2}]
        with mock.patch.object(w, "_backups", side_effect=[first, first, changed]):
            self.assertEqual(w.check_once(), "stabilizing")
            self.assertEqual(w.check_once(), "stabilizing")
            self.assertEqual(w.check_once(), "stabilizing")
        self.assertEqual(json.loads(w.STATE_PATH.read_text())["candidate"]["stableChecks"], 0)


if __name__ == "__main__":
    unittest.main()
