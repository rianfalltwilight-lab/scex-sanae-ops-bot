"""Offline real run_agent/sanae_reply + ConversationTurns integration tests."""
import copy
import importlib
import os
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ["SCE_BOT_TEST_MODE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network disabled")):
    s = importlib.import_module("sanae_ai")
    from conversation_turns import ConversationTurns


class ManualClock:
    def __init__(self):
        self.now, self.timers = 100.0, []

    def __call__(self):
        return self.now

    def timer(self, delay, callback):
        clock = self

        class Timer:
            daemon = False

            def __init__(self):
                self.due, self.cancelled, self.started = clock.now + delay, False, False

            def start(self):
                self.started = True
                clock.timers.append(self)

            def cancel(self):
                self.cancelled = True

            def run(self):
                self.started = False
                callback()

        return Timer()

    def advance(self, seconds):
        target = self.now + seconds
        while True:
            ready = [t for t in self.timers if t.started and not t.cancelled and t.due <= target]
            if not ready:
                break
            timer = min(ready, key=lambda t: t.due)
            self.now = timer.due
            timer.run()
        self.now = target


class TurnModelIntegrationTests(unittest.TestCase):
    def test_explicit_temporary_style_has_host_directive_and_skips_roast_hint(self):
        s.run_agent('你真会吐槽', '11', False, '22', social_only=True,
                    social_style_override='gentle_serious')
        system = self.requests[-1]['messages'][0]['content']
        self.assertIn('【当前本人明确风格要求】', system)
        self.assertNotIn('【当前表达提示】', system)

    def test_temporary_style_cannot_change_qa_mode(self):
        s.run_agent('解释光的折射', '11', False, '22', social_style_override='gentle')
        system = self.requests[-1]['messages'][0]['content']
        self.assertNotIn('【当前本人明确风格要求】', system)
        self.assertIn(s.SANAE_ANSWER_SYSTEM, system)

    def setUp(self):
        self.clock = ManualClock()
        self.turns = ConversationTurns(("22",), clock=self.clock, timer_factory=self.clock.timer)
        self.addCleanup(self.turns.close)
        self.events, self.serial, self.requests = [], 0, []
        for patcher in (
                mock.patch.object(s.time, "monotonic", side_effect=self.clock),
                mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network disabled")),
                mock.patch.object(socket, "create_connection", side_effect=AssertionError("network disabled")),
                mock.patch.object(s.SHARED_KNOWLEDGE, "context", return_value=""),
                mock.patch.object(s, "_with_reply_context", side_effect=lambda raw, **kw: raw),
                mock.patch.object(s, "_add_text_usage", side_effect=self.add_usage),
                mock.patch.object(s, "_text_post", side_effect=self.model),
                mock.patch.object(s, "execute_tool", side_effect=AssertionError("unexpected tool"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("_HISTORY", "_CHAT_HISTORY", "_HISTORY_GENERATION", "_LAST_MEMBER", "_LAST_INTERACTION"):
            patcher = mock.patch.object(s, name, {})
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def add_usage(usage, response):
        usage.calls += 1

    def model(self, payload, **kwargs):
        self.requests.append(copy.deepcopy(payload))
        return self.answer("当前这句我接住了。")

    @staticmethod
    def answer(text):
        return {"choices": [{"message": {"content": text}}], "usage": {}}

    @staticmethod
    def tool_reply(name="list_mods"):
        return {"choices": [{"message": {"content": "", "tool_calls": [{
            "id": "offline-tool-1", "type": "function", "function": {
                "name": name, "arguments": "{}"}}]}}], "usage": {}}

    def event(self, text="早苗，今天这个梗挺好笑", uid="11"):
        self.serial += 1
        return {"post_type": "message", "message_type": "group", "group_id": "22", "user_id": uid,
                "self_id": "99", "message_id": str(self.serial), "raw_message": text,
                "message": [{"type": "text", "data": {"text": text}}]}

    def queue(self, text="早苗，今天这个梗挺好笑", *, flush=True):
        self.assertTrue(self.turns.submit(self.event(text), self.events.append, chat_only=True))
        if flush:
            self.clock.advance(1.0)
            return self.events[-1]["_conversation_turn"]

    def ask(self, token, **kwargs):
        return s.run_agent("今天这个梗挺好笑", "11", False, "22", short_reply=True,
                           conversation_turn=token, **kwargs)

    def reply(self, token, **kwargs):
        return s.sanae_reply("早苗，今天这个梗挺好笑", "11", group_id="22", force=True,
                            explicit_interaction=True, short_reply=True, conversation_turn=token, **kwargs)

    def test_already_superseded_turn_never_calls_model(self):
        token = self.queue()
        self.queue("不是，刚才那个梗说错了", flush=False)
        self.assertFalse(token.current())
        answer, usage, used = self.ask(token)
        self.assertEqual(answer, "")
        self.assertEqual(usage.calls, 0)
        self.assertFalse(used)
        s._text_post.assert_not_called()
        self.assertEqual(s._CHAT_HISTORY, {})
        self.assertEqual(s._HISTORY, {})

    def test_correction_during_model_generation_drops_answer_and_all_history(self):
        token = self.queue()

        def interrupted(payload, **kwargs):
            self.queue("不是，我刚才说错了，换成后一条", flush=False)
            return self.answer("这是已经过时的答复")

        with mock.patch.object(s, "_text_post", side_effect=interrupted):
            answer, usage, used = self.ask(token)
        self.assertEqual(answer, "")
        self.assertEqual(usage.calls, 1)
        self.assertFalse(used)
        self.assertEqual(s._CHAT_HISTORY, {})
        self.assertEqual(s._HISTORY, {})

    def test_superseded_sanae_reply_does_not_return_usage_footer(self):
        token = self.queue()

        def interrupted(payload, **kwargs):
            self.queue("更正一下，刚才说的是另一件事", flush=False)
            return self.answer("旧答复")

        with mock.patch.object(s, "_text_post", side_effect=interrupted), \
                mock.patch.object(s._Usage, "footer", return_value="费用尾注") as footer:
            self.assertIsNone(self.reply(token))
        footer.assert_not_called()
        self.assertEqual(s._CHAT_HISTORY, {})
        self.assertEqual(s._HISTORY, {})

    def test_superseded_before_tool_execution_cannot_run_tool(self):
        token = self.queue()

        def interrupted(payload, **kwargs):
            self.queue("等等，先别接上一个话题", flush=False)
            return self.tool_reply()

        with mock.patch.object(s, "_text_post", side_effect=interrupted):
            answer, usage, used = self.ask(token)
        self.assertEqual(answer, "")
        self.assertFalse(used)
        self.assertFalse(token._protected)
        s.execute_tool.assert_not_called()
        self.assertEqual(s._HISTORY, {})

    def test_actual_tool_start_protects_result_without_expanding_member_authority(self):
        token = self.queue()
        requests = []

        def model(payload, **kwargs):
            requests.append(copy.deepcopy(payload))
            return self.tool_reply() if len(requests) == 1 else self.answer("已读取示例模组列表。")

        def tool(name, args, privileged, **kwargs):
            self.assertTrue(token._protected)
            self.assertFalse(privileged)
            self.assertFalse(kwargs["allow_mutation"])
            self.assertFalse(kwargs["allow_config_write"])
            self.assertFalse(kwargs["allow_group_file_download"])
            self.assertEqual(name, "list_mods")
            self.queue("更正一下，接下来聊别的", flush=False)
            self.assertTrue(token.current())
            return "示例模组"

        with mock.patch.object(s, "_text_post", side_effect=model), \
                mock.patch.object(s, "execute_tool", side_effect=tool) as execute:
            answer, _, used = self.ask(token)
        self.assertTrue(used)
        self.assertEqual(answer, "已读取示例模组列表。")
        execute.assert_called_once()
        offered = {t["function"]["name"] for t in requests[0]["tools"]}
        self.assertEqual(offered, {t["function"]["name"] for t in s.TOOLS_MEMBER})
        self.assertNotIn("request_console_command", offered)
        self.assertEqual(s._get_chat_history("22", "11", False), [])
        self.assertIn("已读取示例模组列表", str(s._get_history("22", "11", False)))
        self.clock.advance(1.0)
        self.assertNotIn("今天这个梗", self.events[-1]["raw_message"])

    def test_rejected_tool_does_not_protect_or_bypass_member_tool_list(self):
        token = self.queue()
        calls = []

        def model(payload, **kwargs):
            calls.append(copy.deepcopy(payload))
            if len(calls) == 1:
                return self.tool_reply("read_server_log")
            self.assertFalse(token._protected)
            self.assertIn("不在当前请求可用列表", str(payload["messages"]))
            self.queue("不是，取消上句话题", flush=False)
            return self.answer("被更正后应丢弃")

        with mock.patch.object(s, "_text_post", side_effect=model):
            answer, _, _ = self.ask(token)
        s.execute_tool.assert_not_called()
        self.assertEqual(answer, "")
        self.assertFalse(token._protected)
        self.assertEqual(s._HISTORY, {})

    def test_new_merged_turn_bypasses_old_interaction_throttle(self):
        with mock.patch.object(s.time, "time", return_value=10000):
            self.assertTrue(s._explicit_interaction_ok("22", "11"))
            self.assertFalse(s._explicit_interaction_ok("22", "11"))
            self.queue("早苗，第一半句", flush=False)
            self.clock.advance(.4)
            token = self.queue("补充一下，第二半句")
            self.assertEqual(len(token.source_ids), 2)
            answer = s.sanae_reply(token.merged_event["raw_message"], "11", group_id="22", force=True,
                                   explicit_interaction=True, short_reply=True, conversation_turn=token,
                                   include_usage_footer=False)
        self.assertEqual(answer, "当前这句我接住了。")
        prompt = self.requests[-1]["messages"][-1]["content"]
        self.assertIn("第一半句", prompt)
        self.assertIn("第二半句", prompt)
        self.assertEqual(s._LAST_INTERACTION[("22", "11")], 10000)

    def test_independent_qa_without_token_retains_history_and_original_throttle(self):
        with mock.patch.object(s.time, "time", return_value=10000):
            answer = s.sanae_reply("!问 解释一下齿轮传动", "11", group_id="22", force=True,
                                   explicit_interaction=True, include_usage_footer=False)
            second = s.sanae_reply("!问 再说一遍", "11", group_id="22", force=True,
                                   explicit_interaction=True, include_usage_footer=False)
            history = s._get_history("22", "11", False)
        self.assertEqual(answer, "当前这句我接住了。")
        self.assertIsNone(second)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("齿轮传动", str(history))
        self.assertEqual(s._get_chat_history("22", "11", False), [])


if __name__ == "__main__":
    unittest.main()
