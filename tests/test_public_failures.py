"""User-visible failures carry a coarse class, never raw exception text."""
import os
import socket
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

os.environ["SCE_BOT_TEST_MODE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from public_errors import public_failure_label  # noqa: E402
import sanae_ai as s  # noqa: E402

SECRETS = ("yt.example", "192.168", "E:\\", "token=")


class PublicFailureLabelTests(unittest.TestCase):
    def test_labels(self):
        rcon_error = type("RconError", (Exception,), {})("RCON 认证失败（密码错误？）")
        cases = (
            (urllib.error.HTTPError("https://yt.example/v1", 503, "x", {}, None), "HTTP 503"),
            (urllib.error.URLError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF yt.example"), "连接中断"),
            (urllib.error.URLError("timed out"), "连接超时"),
            (ConnectionRefusedError(10061, "refused 192.0.2.16:25575"), "连接被拒绝"),
            (socket.timeout("timed out"), "连接超时"),
            (FileNotFoundError(2, "missing", "E:\\secret\\x.ps1"), "文件不存在"),
            (ValueError("Expecting value"), "数据格式错误"),
            (rcon_error, "RCON 认证失败（密码错误？）"),
            (RuntimeError("OneBot failed token=abc host=192.0.2.16"), "内部错误"),
        )
        for exc, expected in cases:
            with self.subTest(exc=type(exc).__name__):
                label = public_failure_label(exc)
                self.assertEqual(label, expected)
                for secret in SECRETS:
                    self.assertNotIn(secret, label)


class ChatFallbackTests(unittest.TestCase):
    def setUp(self):
        for name in ("_HISTORY", "_CHAT_HISTORY", "_HISTORY_GENERATION"):
            patcher = mock.patch.object(s, name, {})
            patcher.start()
            self.addCleanup(patcher.stop)
        for owner, name in ((socket.socket, "connect"), (socket, "create_connection")):
            patcher = mock.patch.object(owner, name, side_effect=AssertionError("network disabled"))
            patcher.start()
            self.addCleanup(patcher.stop)
        knowledge = mock.patch.object(s.SHARED_KNOWLEDGE, "context", return_value="")
        knowledge.start()
        self.addCleanup(knowledge.stop)

    def test_chat_failure_is_in_character_and_not_remembered(self):
        error = urllib.error.URLError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF yt.example 192.0.2.16")
        with mock.patch.object(s, "_text_post", side_effect=error):
            answer, usage, used = s.run_agent("晚上好", "u", False, "g", social_only=True)
        self.assertEqual(answer, s._chat_failure_reply())
        self.assertFalse(used)
        for secret in ("SSL", "EOF") + SECRETS:
            self.assertNotIn(secret, answer)
        self.assertFalse(any(rows for rows, _gen in s._CHAT_HISTORY.values()))

    def test_chat_timeout_does_not_use_operations_wording(self):
        with mock.patch.object(s, "_text_post", side_effect=s.RequestTimeout("操作超时，结果未知；不自动重复执行")):
            answer, _usage, _used = s.run_agent("晚上好", "u", False, "g", social_only=True)
        self.assertEqual(answer, s._chat_failure_reply(timeout=True))
        self.assertNotIn("结果未知", answer)


class GeminiBackgroundThinkingTests(unittest.TestCase):
    def test_background_marker_selects_low_and_replies_keep_configured_level(self):
        base = {"messages": [{"role": "user", "content": "x"}], "max_tokens": 900}
        with mock.patch.object(s, "GEMINI_THINKING_LEVEL", "high"):
            reply = s._gemini_request_payload(dict(base))
            background = s._gemini_request_payload(dict(base, _background_thinking="low"))
        self.assertEqual(reply["generationConfig"]["thinkingConfig"]["thinkingLevel"], "high")
        self.assertEqual(background["generationConfig"]["thinkingConfig"]["thinkingLevel"], "low")

    def test_summary_marks_gemini_only(self):
        batch = {"prompt": "p", "messages": [{"text": "x"}]}
        response = {"choices": [{"message": {"content": "{}"}}], "usage": {}}
        for provider, marked in (("gemini", True), ("deepseek", False)):
            with self.subTest(provider=provider), \
                    mock.patch.object(s, "TEXT_PROVIDER", provider), \
                    mock.patch.object(s, "_text_post", return_value=response) as post:
                s.summarize_conversation_memory(batch)
            payload = post.call_args.args[0]
            self.assertEqual(payload.get("_background_thinking") == "low", marked)


if __name__ == "__main__":
    unittest.main()
