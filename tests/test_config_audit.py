#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import importlib
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAINT = os.path.join(HERE, "maintenance")
if MAINT not in sys.path:
    sys.path.insert(0, MAINT)

audit = importlib.import_module("config_audit")


class TempRootMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def touch(self, rel: str, content: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p


class SnapshotTests(TempRootMixin):
    def test_two_consecutive_changes(self):
        self.touch("config/server.properties", "v1")
        a = audit.ConfigAuditor(self.root)

        snap1 = a.snapshot_before_write("config/server.properties", "admin", "replace_config")
        self.touch("config/server.properties", "v2")  # 第一次改
        a.finalize(snap1["changeId"], "admin", "replace_config",
                   "config/server.properties", "verified")

        snap2 = a.snapshot_before_write("config/server.properties", "admin", "replace_config")
        (self.root / "config/server.properties").write_text("v3", encoding="utf-8")
        a.finalize(snap2["changeId"], "admin", "replace_config",
                   "config/server.properties", "verified")

        self.assertNotEqual(snap1["changeId"], snap2["changeId"])
        # 两次快照应各自落盘且内容不同
        snap_file1 = self.root / ".config-audit" / "files" / snap1["changeId"] / "config/server.properties"
        snap_file2 = self.root / ".config-audit" / "files" / snap2["changeId"] / "config/server.properties"
        self.assertEqual(snap_file1.read_text(encoding="utf-8"), "v1")
        self.assertEqual(snap_file2.read_text(encoding="utf-8"), "v2")

        self.assertTrue(a.verify_chain())

    def test_before_sha256_recorded(self):
        self.touch("a.txt", "hello")
        a = audit.ConfigAuditor(self.root)
        snap = a.snapshot_before_write("a.txt", "a", "op")
        import hashlib
        self.assertEqual(snap["beforeSha256"], hashlib.sha256(b"hello").hexdigest())


class HashChainTests(TempRootMixin):
    def test_chain_valid_and_detect_tamper(self):
        self.touch("c.txt", "x")
        a = audit.ConfigAuditor(self.root)
        snap = a.snapshot_before_write("c.txt", "a", "op")
        a.finalize(snap["changeId"], "a", "op", "c.txt", "verified")
        self.assertTrue(a.verify_chain())

        # 篡改第一条记录内容 => 链断裂
        ep = self.root / ".config-audit" / "events.jsonl"
        lines2 = ep.read_text(encoding="utf-8").splitlines()
        lines2[0] = lines2[0].replace('"actor": "a"', '"actor": "tampered"')
        ep.write_text("\n".join(lines2) + "\n", encoding="utf-8")
        self.assertFalse(a.verify_chain())

    def test_concurrent_appends_remain_linear(self):
        a = audit.ConfigAuditor(self.root)
        errors = []

        def worker(i):
            try:
                self.touch(f"config/c{i}.toml", f"value={i}\n")
                a.snapshot_before_write(f"config/c{i}.toml", f"u{i}", "replace")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertTrue(a.verify_chain())
        events = a._read_events()
        self.assertEqual([event["seq"] for event in events], list(range(1, 13)))


class FinalizeTests(TempRootMixin):
    def test_failed_records_error(self):
        self.touch("f.txt", "1")
        a = audit.ConfigAuditor(self.root)
        snap = a.snapshot_before_write("f.txt", "a", "op")
        r = a.finalize(snap["changeId"], "a", "op", "f.txt", "failed", error="boom")
        self.assertEqual(r["verified"], "failed")
        self.assertTrue(a.verify_chain())

    def test_finalize_requires_valid_verified(self):
        self.touch("g.txt", "1")
        a = audit.ConfigAuditor(self.root)
        snap = a.snapshot_before_write("g.txt", "a", "op")
        with self.assertRaises(audit.ConfigAuditError):
            a.finalize(snap["changeId"], "a", "op", "g.txt", "maybe")


class RollbackTests(TempRootMixin):
    def test_dry_run_preview_no_change(self):
        self.touch("r.txt", "v1")
        a = audit.ConfigAuditor(self.root)
        snap = a.snapshot_before_write("r.txt", "a", "op")
        self.touch("r.txt", "v2")

        preview = a.rollback(snap["changeId"], "a", dry_run=True)
        self.assertTrue(preview["dryRun"])
        self.assertFalse(preview["applied"])
        # 文件未被改动
        self.assertEqual((self.root / "r.txt").read_text(encoding="utf-8"), "v2")

    def test_dry_run_false_without_apply_is_still_preview(self):
        self.touch("r2.txt", "v1")
        a = audit.ConfigAuditor(self.root)
        snap = a.snapshot_before_write("r2.txt", "a", "op")
        self.touch("r2.txt", "v2")
        with self.assertRaises(audit.ConfigAuditError):
            a.rollback(snap["changeId"], "a", dry_run=False, apply=False)
        # 未被改动
        self.assertEqual((self.root / "r2.txt").read_text(encoding="utf-8"), "v2")

    def test_actual_rollback_applies_and_backs_up(self):
        self.touch("r3.txt", "v1")
        a = audit.ConfigAuditor(self.root)
        snap = a.snapshot_before_write("r3.txt", "a", "op")
        self.touch("r3.txt", "v2")
        a.finalize(snap["changeId"], "a", "op", "r3.txt", "verified")

        result = a.rollback(snap["changeId"], "a", dry_run=False, apply=True)
        self.assertTrue(result["applied"])
        self.assertEqual((self.root / "r3.txt").read_text(encoding="utf-8"), "v1")
        # pre-rollback 备份存在，内容是 v2
        backup = self.root / ".config-audit" / "files" / snap["changeId"] / "r3.txt.pre-rollback"
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(encoding="utf-8"), "v2")
        self.assertTrue(a.verify_chain())


class SecurityTests(TempRootMixin):
    def test_sensitive_file_rejected(self):
        self.touch("secrets/rcon-config.json", "pw")
        a = audit.ConfigAuditor(self.root)
        with self.assertRaises(audit.ConfigAuditError):
            a.snapshot_before_write("secrets/rcon-config.json", "a", "op")

    def test_path_escape_rejected(self):
        self.touch("ok.txt", "1")
        a = audit.ConfigAuditor(self.root)
        with self.assertRaises(audit.ConfigAuditError):
            a.snapshot_before_write("../escape.txt", "a", "op")
        with self.assertRaises(audit.ConfigAuditError):
            a.snapshot_before_write("/etc/passwd", "a", "op")

    def test_target_inside_audit_dir_rejected(self):
        self.touch("x.txt", "1")
        a = audit.ConfigAuditor(self.root)
        with self.assertRaises(audit.ConfigAuditError):
            a.snapshot_before_write(".config-audit/events.jsonl", "a", "op")

    def test_bad_change_id_rejected(self):
        self.touch("y.txt", "1")
        a = audit.ConfigAuditor(self.root)
        with self.assertRaises(audit.ConfigAuditError):
            a.snapshot_before_write("y.txt", "a", "op", change_id="../../evil")
        with self.assertRaises(audit.ConfigAuditError):
            a.snapshot_before_write("y.txt", "a", "op", change_id="a b")


class ModuleLevelApiTests(TempRootMixin):
    def test_module_functions_work(self):
        self.touch("m.txt", "1")
        snap = audit.snapshot_before_write(str(self.root), "m.txt", "a", "op")
        self.touch("m.txt", "2")
        audit.finalize(str(self.root), snap["changeId"], "a", "op", "m.txt", "verified")
        r = audit.rollback(str(self.root), snap["changeId"], "a", dry_run=False, apply=True)
        self.assertTrue(r["applied"])
        self.assertEqual((self.root / "m.txt").read_text(encoding="utf-8"), "1")


if __name__ == "__main__":
    unittest.main()
