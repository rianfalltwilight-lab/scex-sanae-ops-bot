#!/usr/bin/env python3
import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MC_OPS = ROOT / "mc-ops"
sys.path.insert(0, str(MC_OPS))
POSTMORTEM_PATH = ROOT / "skills/minecraft-server-ops/references/incident_postmortem.py"
postmortem = None
if POSTMORTEM_PATH.exists():
    SPEC = importlib.util.spec_from_file_location("incident_postmortem", POSTMORTEM_PATH)
    postmortem = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(postmortem)
# The reference module belongs to the external skills tree, absent on this host.
needs_postmortem = unittest.skipIf(postmortem is None, "external incident_postmortem.py not installed")

from ops_contract import normalize_log_fingerprint


class FingerprintTests(unittest.TestCase):
    def test_normalization_removes_volatile_values(self):
        raw = "[10:12:00] [Server thread/ERROR] player 192.168.1.20 uuid 123e4567-e89b-12d3-a456-426614174000 failed at 1234567890"
        self.assertEqual(
            normalize_log_fingerprint(raw),
            "player <ip> uuid <uuid> failed at <timestamp>",
        )

    @needs_postmortem
    def test_window_validation(self):
        self.assertEqual(postmortem.parse_window("6h"), 21600)
        self.assertEqual(postmortem.parse_window("7d"), 604800)
        with self.assertRaises(ValueError):
            postmortem.parse_window("8d")


@needs_postmortem
class PostmortemTests(unittest.TestCase):
    def test_report_builds_internal_timeline_and_redacts(self):
        now = 200000.0
        sources = {
            "lagRecorder": {"exitCode": 0, "data": {"ok": True, "phase": "incident", "incidentId": "x", "lag_events": [{"at": now - 5, "line": "[x] ERROR 10.0.0.1 tick 123"}]}},
            "diagnosis": {"exitCode": 0, "data": {"ok": True, "latest_crash": None, "log_error_lines": ["ERROR at 10.0.0.1 id 42", "ERROR at 10.0.0.2 id 99"]}},
            "backup": {"exitCode": 0, "data": {"ok": True}},
            "configAudit": {"exitCode": 0, "data": {"ok": True}},
        }
        report = postmortem.build_report("1h", now, sources)
        self.assertTrue(report["ok"])
        self.assertEqual(report["timeline"][0]["kind"], "lag")
        self.assertIn("<ip>", report["timeline"][0]["detail"])
        self.assertEqual(report["errorFingerprints"][0]["count"], 2)
        self.assertTrue(report["readOnly"])

    def test_partial_source_is_explicit(self):
        sources = {name: {"exitCode": 0, "data": {"ok": True}} for name in ("lagRecorder", "diagnosis", "backup", "configAudit")}
        sources["backup"] = {"exitCode": 1, "data": {"ok": False}}
        report = postmortem.build_report("1h", 200000.0, sources)
        self.assertFalse(report["ok"])
        self.assertEqual(report["partialSources"], ["backup"])

    def test_old_crash_is_not_an_active_finding(self):
        now = 200000.0
        sources = {
            "lagRecorder": {"exitCode": 0, "data": {"ok": True, "phase": "healthy", "lag_events": []}},
            "diagnosis": {"exitCode": 0, "data": {"ok": True, "latest_crash": {"name": "old.txt", "mtime": now - 7200}, "log_error_lines": []}},
            "backup": {"exitCode": 0, "data": {"ok": True}},
            "configAudit": {"exitCode": 0, "data": {"ok": True}},
        }
        report = postmortem.build_report("1h", now, sources)
        self.assertNotIn("crash", [finding["kind"] for finding in report["findings"]])
        self.assertEqual(report["timeline"], [])


if __name__ == "__main__":
    unittest.main()
