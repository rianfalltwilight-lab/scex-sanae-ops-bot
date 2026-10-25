#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import gzip
import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

import ops_commands
import server_registry
from backup_verify import format_backup_verification, verify_backup_zip
from mod_inventory import format_inventory, scan_installed_mods
from ops_commands import dispatch_ops_command, match_ops_command
from nast_ops_sync import snapshot_age, sync_if_stale, sync_nast_snapshot
from ops_telemetry import (SOFT_NOISE, OpsTelemetry, _read_jsonl, build_snapshot_payload, decode_snapshot_export,
                           ingest_snapshot, write_snapshot_export)
from recipe_index import build_recipe_index, query_recipes, render_recipe_png


def write_jar(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            if not isinstance(value, bytes):
                value = value.encode("utf-8")
            archive.writestr(name, value)


class OpsFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = self.root / "server"
        (self.server / "mods").mkdir(parents=True)
        (self.server / "logs").mkdir()
        (self.server / "crash-reports").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def make_content_jar(self):
        mods_toml = '''modLoader="javafml"
[[mods]]
modId="testmagic"
version="1.2.3"
displayName="Test Magic"
[[dependencies.testmagic]]
modId="bookshelf"
mandatory=true
'''
        recipe = {"type": "minecraft:crafting_shaped", "pattern": ["AA", " A"],
                  "key": {"A": {"item": "minecraft:stone"}},
                  "result": {"id": "testmagic:wand", "count": 2}}
        langs = {"item.testmagic.wand": "测试法杖", "block.minecraft.stone": "石头"}
        path = self.server / "mods" / "testmagic-1.2.3.jar"
        write_jar(path, {
            "META-INF/neoforge.mods.toml": mods_toml,
            "data/testmagic/recipe/wand.json": json.dumps(recipe, ensure_ascii=False),
            "assets/testmagic/lang/zh_cn.json": json.dumps(langs, ensure_ascii=False),
        })
        return path

    def test_mod_inventory_reads_metadata_and_classifies(self):
        self.make_content_jar()
        data = scan_installed_mods(self.server, "legacy", "[怀旧]")
        self.assertEqual(1, data["count"])
        self.assertEqual("testmagic", data["mods"][0]["id"])
        self.assertEqual("Test Magic", data["mods"][0]["name"])
        self.assertEqual("玩法内容", data["mods"][0]["category"])
        rendered = format_inventory(data)
        self.assertTrue(rendered.startswith("[怀旧]"))
        self.assertIn("Test Magic", rendered)

    def test_recipe_index_query_and_png(self):
        self.make_content_jar()
        index = build_recipe_index(self.server, "legacy", "[怀旧]")
        self.assertEqual(1, index["recipe_count"])
        rows = query_recipes(index, "测试法杖")
        self.assertEqual("testmagic:wand", rows[0]["result"])
        out = self.root / "recipe.png"
        render_recipe_png(index, rows[0], out)
        self.assertGreater(out.stat().st_size, 1000)

    def test_backup_verification_quick_and_deep(self):
        backup = self.server / "backups" / "world.zip"
        backup.parent.mkdir()
        level = gzip.compress(b"NBT" + b"x" * 256)
        with zipfile.ZipFile(backup, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("world/level.dat", level)
            archive.writestr("world/region/r.0.0.mca", b"r" * 8192)
            archive.writestr("world/playerdata/test.dat", b"p" * 256)
        result = verify_backup_zip(backup, deep=True)
        self.assertTrue(result["ok"], result)
        self.assertGreater(result["deep_checked"], 0)
        self.assertIn("通过", format_backup_verification({"ok": True, "deep": True,
                                                         "results": [result]}, "[怀旧]"))

    def test_backup_missing_level_fails(self):
        backup = self.root / "bad.zip"
        with zipfile.ZipFile(backup, "w") as archive:
            archive.writestr("world/region/r.0.0.mca", b"x")
        result = verify_backup_zip(backup)
        self.assertFalse(result["ok"])
        self.assertTrue(any("level.dat" in error for error in result["errors"]))

    def test_error_digest_groups_noise_and_critical_cooldown(self):
        ops = OpsTelemetry("legacy", "[怀旧]", self.server, state_base=self.root / "state")
        far_a = "[12:00:00] [Worker/ERROR] [net.minecraft.Util/]: Detected setBlock in a far chunk [1, 2], pos: BlockPos{x=1,y=2,z=3}"
        far_b = "[12:00:01] [Worker/ERROR] [net.minecraft.Util/]: Detected setBlock in a far chunk [9, 8], pos: BlockPos{x=9,y=8,z=7}"
        far_c = "[12:00:02] [Worker/ERROR] [net.minecraft.Util/]: Detected setBlock in a far chunk [7, 6], pos: BlockPos{x=7,y=6,z=5}"
        self.assertEqual([], ops.observe_lines([far_a, far_b, far_c], now=1000,
                                               digest_interval=3600))
        self.assertEqual([], ops.observe_lines([], now=3000, digest_interval=3600))
        digest = ops.observe_lines([], now=4601, digest_interval=3600)
        self.assertEqual(1, len(digest))
        self.assertIn("3 条 ERROR，1 类", digest[0])
        self.assertIn("3× 远区块生成发生 setBlock", digest[0])
        self.assertTrue(digest[0].startswith("[怀旧]"))

        critical = "[12:30:00] [Server thread/FATAL] [Watchdog]: A single server tick took 60 seconds"
        first = ops.observe_lines([critical], now=3000, cooldown=3600)
        second = ops.observe_lines([critical], now=3200, cooldown=3600)
        third = ops.observe_lines([critical], now=6601, cooldown=3600)
        self.assertEqual(1, len(first))
        self.assertEqual([], second)
        self.assertEqual(1, len(third))
        self.assertIn("紧急", first[0])

    def test_unknown_errors_group_by_logger_and_one_off_stays_silent(self):
        ops = OpsTelemetry("nast", "[nast]", self.server, state_base=self.root / "state")
        rows = [
            "[1] [Worker/ERROR] [example.Mod/]: First failure for alpha",
            "[2] [Worker/ERROR] [example.Mod/]: Different failure for beta",
            "[3] [Worker/ERROR] [example.Mod/]: Third failure for gamma",
        ]
        self.assertEqual([], ops.observe_lines(rows, now=1000, digest_interval=3600))
        digest = ops.observe_lines([], now=4601, digest_interval=3600)
        self.assertEqual(1, len(digest))
        self.assertIn("3 条 ERROR，1 类", digest[0])
        single = OpsTelemetry("legacy", "[怀旧]", self.server,
                              state_base=self.root / "single-state")
        single.observe_lines([rows[0]], now=1000, digest_interval=3600)
        self.assertEqual([], single.observe_lines([], now=4601, digest_interval=3600))

    def test_timeline_postmortem_weekly(self):
        ops = OpsTelemetry("nast", "[nast]", self.server, state_base=self.root / "state")
        now = time.time()
        ops.record_event("join", "[nast] 玩家 Foo 加入服务器", occurred_at=now - 10)
        ops.observe_lines(["[Server thread/ERROR] [x]: boom 42"], now=now - 8)
        ops.record_event("crash", "[nast] 崩溃 crash-test.txt", occurred_at=now - 5)
        data = ops.timeline("1h", now)
        self.assertTrue(any(row["kind"] == "crash" for row in data["events"]))
        post = ops.incident_postmortem("1h", now)
        self.assertEqual("高", post["severity"])
        self.assertTrue(ops.format_postmortem(post).startswith("[nast]"))
        weekly = ops.weekly_report(now)
        self.assertIn("events", weekly)
        self.assertTrue(ops.format_weekly(weekly).startswith("[nast]"))

    def test_snapshot_roundtrip_and_rejects_wrong_server(self):
        source = OpsTelemetry("nast", "[nast]", self.server, state_base=self.root / "source")
        source.record_event("join", "[nast] 玩家 Foo 加入服务器")
        payload = build_snapshot_payload(source)
        target = self.root / "target"
        ingest_snapshot(target, payload, expected_server="nast")
        self.assertTrue((target / "nast" / "events.jsonl").is_file())
        payload["server"] = "legacy"
        with self.assertRaises(ValueError):
            ingest_snapshot(target, payload, expected_server="nast")

    def test_ascii_snapshot_export_and_injected_restricted_file_sync(self):
        source = OpsTelemetry("nast", "[nast]", self.server, state_base=self.root / "source")
        source.record_event("join", "[nast] 玩家 Foo 加入服务器")
        export = self.root / "ops-snapshot.json.gz.b64"
        sizes = write_snapshot_export(source, export)
        self.assertGreater(sizes["encoded_bytes"], 0)
        payload = decode_snapshot_export(export.read_text(encoding="ascii"))
        self.assertEqual("nast", payload["server"])
        calls = []

        def get_fn(endpoint, **params):
            calls.append((endpoint, params))
            return export.read_text(encoding="ascii")

        target = self.root / "target-sync"
        result = sync_nast_snapshot(target, get_fn=get_fn)
        self.assertTrue(result["ok"])
        self.assertEqual("/read", calls[0][0])
        self.assertEqual("ops-export/ops-snapshot.json.gz.b64", calls[0][1]["path"])
        self.assertTrue((target / "nast" / "events.jsonl").is_file())

    def test_sync_if_stale_uses_cache_and_refreshes_expired_snapshot(self):
        target = self.root / "target-stale"
        meta = target / "nast" / "snapshot-meta.json"
        meta.parent.mkdir(parents=True)
        meta.write_text(json.dumps({"received_at": 900.0}), encoding="utf-8")
        calls = []

        def should_not_run(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("fresh cache must not perform a cross-host read")

        cached = sync_if_stale(target, max_age=300, get_fn=should_not_run, now=1000.0)
        self.assertTrue(cached["cached"])
        self.assertEqual([], calls)
        self.assertEqual(100.0, snapshot_age(target, now=1000.0))

        source = OpsTelemetry("nast", "[nast]", self.server, state_base=self.root / "source-stale")
        source.record_event("join", "[nast] 玩家 Foo 加入服务器")
        export = self.root / "stale-export.b64"
        write_snapshot_export(source, export)

        def get_fn(endpoint, **params):
            calls.append((endpoint, params))
            return export.read_text(encoding="ascii")

        refreshed = sync_if_stale(target, max_age=50, get_fn=get_fn, now=1000.0)
        self.assertFalse(refreshed["cached"])
        self.assertEqual("/read", calls[-1][0])

    def test_command_routing_prefix_permissions_and_recipe_image(self):
        self.make_content_jar()
        registry = self.root / "servers.json"
        registry.write_text(json.dumps({"servers": [{
            "id": "legacy", "name": "Legacy", "prefix": "[怀旧]", "aliases": ["怀旧"],
            "enabled": True, "host": "127.0.0.1", "port": 1,
            "password_file": str(self.root / "unused"), "server_dir": str(self.server),
        }]}), encoding="utf-8")
        old_registry = server_registry.REGISTRY_PATH
        old_state = ops_commands.STATE_BASE
        old_image = ops_commands.IMAGE_WSL_DIR
        old_windows = ops_commands.IMAGE_WINDOWS_DIR
        try:
            server_registry.REGISTRY_PATH = str(registry)
            ops_commands.STATE_BASE = self.root / "ops-state"
            ops_commands.IMAGE_WSL_DIR = self.root / "images"
            ops_commands.IMAGE_WINDOWS_DIR = r"C:\ops-images"
            ops = OpsTelemetry("legacy", "[怀旧]", self.server,
                               state_base=ops_commands.STATE_BASE)
            ops.refresh_indexes(force=True)
            mods = dispatch_ops_command("!模组清单 [怀旧]", 1, 2, False)
            self.assertTrue(mods.text.startswith("[怀旧]"))
            recipe = dispatch_ops_command("[怀旧] !配方 测试法杖", 1, 2, False)
            self.assertTrue(recipe.text.startswith("[怀旧]"))
            self.assertTrue(recipe.image_path.endswith(".png"))
            denied = dispatch_ops_command("!时间线 [怀旧] 6h", 1, 2, False)
            self.assertIn("仅群主/管理员", denied.text)
            self.assertIsNotNone(match_ops_command("!周报 [怀旧]"))
        finally:
            server_registry.REGISTRY_PATH = old_registry
            ops_commands.STATE_BASE = old_state
            ops_commands.IMAGE_WSL_DIR = old_image
            ops_commands.IMAGE_WINDOWS_DIR = old_windows

    def test_jsonl_reader_uses_bounded_recent_tail(self):
        path = self.root / "history.jsonl"
        with path.open("w", encoding="utf-8") as stream:
            for index in range(5000):
                stream.write(json.dumps({"timestamp": index, "value": "x" * 80}) + "\n")
        rows = _read_jsonl(path, since=0, limit=50, max_bytes=4096)
        self.assertGreater(len(rows), 0)
        self.assertLessEqual(len(rows), 50)
        self.assertEqual(rows[-1]["timestamp"], 4999)
        self.assertGreater(rows[0]["timestamp"], 4900)

    def test_caused_by_is_not_classified_as_soft_stack_noise(self):
        self.assertIsNone(SOFT_NOISE.match("Caused by: java.lang.ClassNotFoundException: real.mod.Entry"))


if __name__ == "__main__":
    unittest.main()
