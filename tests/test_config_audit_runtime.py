#!/usr/bin/env python3
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAINT = os.path.join(HERE, "maintenance")
if MAINT not in sys.path:
    sys.path.insert(0, MAINT)

runtime = importlib.import_module("config_audit_runtime")


class RuntimeAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.target = self.root / "config" / "sample.toml"
        self.target.parent.mkdir()
        self.target.write_text("value = 1\n", encoding="utf-8")
        self.audit = runtime.Auditor(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_finalize_and_valid_chain(self):
        change_id = self.audit.snapshot("config/sample.toml", "replace_config", "count=1")
        self.target.write_text("value = 2\n", encoding="utf-8")
        self.audit.finalize(change_id, "config/sample.toml", "replace_config", "verified")
        status = self.audit.status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["eventCount"], 2)
        self.assertEqual(status["changes"][0]["changeId"], change_id)

    def test_tamper_is_detected(self):
        self.audit.snapshot("config/sample.toml", "replace_config")
        events = self.root / ".config-audit" / "events.jsonl"
        events.write_text(events.read_text(encoding="utf-8").replace("mcfile-api", "tampered"), encoding="utf-8")
        self.assertFalse(self.audit.status()["ok"])
        with self.assertRaises(runtime.AuditError):
            self.audit.snapshot("config/sample.toml", "replace_config")

    def test_sensitive_and_escape_paths_rejected(self):
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "token.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(runtime.AuditError):
            self.audit.snapshot("../outside", "x")
        with self.assertRaises(runtime.AuditError):
            self.audit.snapshot("secrets/token.txt", "x")


if __name__ == "__main__":
    unittest.main()
