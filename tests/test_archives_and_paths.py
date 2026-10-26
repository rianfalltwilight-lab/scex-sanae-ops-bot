"""Reply archive, chat-archive retention, log timestamps, test-mode paths, script encodings."""
import io
import os
import re
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import runtime_paths  # noqa: E402
from chat_archive import ARCHIVE_TZ, ChatArchive, prune_day_files  # noqa: E402
from social_lite import SocialLite  # noqa: E402


class ReplyArchiveTests(unittest.TestCase):
    def test_reply_archive_uses_chat_archive_clock_and_keeps_bubbles(self):
        with tempfile.TemporaryDirectory() as tmp:
            lite = SocialLite(Path(tmp) / "s.json", Path(tmp) / "a.log", reply_archive_dir=Path(tmp) / "arch")
            lite.record_outgoing(1001, "第一句\n第二句[CQ:image,file=x]\n\n")
            day = datetime.now(ARCHIVE_TZ).strftime("%Y-%m-%d")
            text = (Path(tmp) / "arch" / f"{day}.log").read_text(encoding="utf-8")
            self.assertRegex(text, r"^\[\d\d:\d\d:\d\d\] group:1001 reply: 第一句 / 第二句\n$")

    def test_no_archive_directory_means_no_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            SocialLite(Path(tmp) / "s.json", Path(tmp) / "a.log").record_outgoing(1, "不归档")
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["a.log", "s.json"])


class ChatArchiveRetentionTests(unittest.TestCase):
    def test_prune_uses_file_name_dates_across_group_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for group in ("old-name", "new-name"):
                (root / group).mkdir()
            for path in ("old-name/2025-01-01.log", "old-name/2026-10-01.log",
                         "new-name/2025-10-24.log", "new-name/2025-10-25.log", "new-name/notes.log"):
                (root / path).write_text("x\n", encoding="utf-8")
            removed = prune_day_files(root, 365, date(2026, 10, 25))
            remaining = sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*.log"))
        self.assertEqual(removed, 2)
        self.assertEqual(remaining, ["new-name/2025-10-25.log", "new-name/notes.log", "old-name/2026-10-01.log"])

    def test_append_prunes_once_per_day_only_when_retention_is_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "g").mkdir()
            stale = root / "g" / "2020-01-01.log"
            stale.write_text("x\n", encoding="utf-8")
            ChatArchive(root).append(1, "g", "a", "hi")
            self.assertTrue(stale.exists())
            ChatArchive(root, retention_days=365).append(1, "g", "a", "hi")
            self.assertFalse(stale.exists())


class TimestampedLogTests(unittest.TestCase):
    def test_each_line_gets_local_time_with_offset(self):
        source = (ROOT / "llbot_bridge.py").read_text(encoding="utf-8")
        block = re.search(r"class _TimestampedStream:.*?(?=\n\ndef _configure_child_stdio)", source, re.S).group(0)
        namespace = {}
        exec(block, namespace)
        buffer = io.StringIO()
        stream = namespace["_TimestampedStream"](buffer)
        print("first", end="", file=stream)
        print(" continues", file=stream)
        stream.write("two\nlines\n")
        lines = buffer.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertRegex(line, r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[+-]\d{4}\] ")
        self.assertTrue(lines[0].endswith("first continues"))


class RuntimePathTests(unittest.TestCase):
    def test_test_mode_uses_temp_tree_and_production_uses_bot_root(self):
        with mock.patch.dict(os.environ, {"SCE_BOT_TEST_MODE": "1"}):
            self.assertTrue(runtime_paths.default_path("state", "x.json").startswith(tempfile.gettempdir()))
        with mock.patch.dict(os.environ, {"SCE_BOT_TEST_MODE": "0"}):
            self.assertEqual(runtime_paths.default_path("state", "x.json"),
                             str(runtime_paths.BOT_ROOT / "state" / "x.json"))


class ScriptEncodingTests(unittest.TestCase):
    """wscript and Windows PowerShell 5 read BOM-less files in the ANSI code page."""

    def test_vbs_is_ascii_and_powershell_is_ascii_or_bom(self):
        for path in (ROOT / "tools").glob("*.vbs"):
            with self.subTest(path=path.name):
                self.assertTrue(all(b < 128 for b in path.read_bytes()))
        for path in [ROOT / "start.example.ps1", ROOT / "examples" / "env.example.ps1", *(ROOT / "tools").glob("*.ps1")]:
            with self.subTest(path=path.name):
                data = path.read_bytes()
                self.assertTrue(data.startswith(b"\xef\xbb\xbf") or all(b < 128 for b in data))


if __name__ == "__main__":
    unittest.main()
