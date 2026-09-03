#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_SRC = os.path.join(HERE, "src")
if os.path.isdir(PACKAGE_SRC):
    HERE = PACKAGE_SRC
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from ops_contract import (append_jsonl, command_result, event, onebot_success,
                          read_incremental_lines, render_command_result)


class ContractTests(unittest.TestCase):
    def test_event_id_is_stable_for_same_payload(self):
        a = event("player_joined", {"player": "Steve"}, occurred_at=1)
        b = event("player_joined", {"player": "Steve"}, occurred_at=1)
        self.assertEqual(a["event_id"], b["event_id"])
        self.assertEqual(a["type"], "player_joined")

    def test_command_result_has_machine_state_and_human_renderer(self):
        result = command_result("list", state="success", output="0/20")
        self.assertTrue(result["request_id"].startswith("req-"))
        self.assertIn("0/20", render_command_result(result, "在线"))

    def test_onebot_requires_explicit_success(self):
        self.assertEqual(onebot_success('{"status":"ok","retcode":0}')[0], True)
        self.assertEqual(onebot_success('{"status":"failed","retcode":0}')[0], False)
        self.assertEqual(onebot_success('{"status":"ok"}')[0], False)
        self.assertEqual(onebot_success("not-json")[0], False)

    def test_jsonl_event_stream_is_parseable(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.jsonl"
            append_jsonl(path, event("player_left", {"player": "Steve"}, occurred_at=1))
            data = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(data["type"], "player_left")

    def test_incremental_reader_keeps_unterminated_line(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "latest.log"
            path.write_bytes(b"first\nsecond")
            lines, state, ok = read_incremental_lines(path, {})
            self.assertTrue(ok)
            self.assertEqual(lines, ["first"])
            self.assertEqual(state["partial"], "second")
            path.write_bytes(b"first\nsecond\nthird")
            lines, state, ok = read_incremental_lines(path, state)
            self.assertEqual(lines, ["second"])
            self.assertEqual(state["partial"], "third")

    def test_incremental_reader_resets_on_rotation(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "latest.log"
            path.write_bytes(b"old\n")
            _, state, _ = read_incremental_lines(path, {})
            path.write_bytes(b"new\n")
            lines, _, _ = read_incremental_lines(path, state)
            self.assertEqual(lines, ["new"])

if __name__ == "__main__":
    unittest.main()
