"""Leaked tool-call markup, the DeepSeek transport fallback and quiet ambient failures.

Offline only: sockets are disabled and every model call is a mock.
"""
import contextlib
import io
import os
import socket
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

os.environ["SCE_BOT_TEST_MODE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llbot_bridge as bridge  # noqa: E402
import sanae_ai as s  # noqa: E402

DSML = '<｜｜DSML｜｜ calls> <｜｜DSML｜｜ invoke name="run_rcon">list</｜｜DSML｜｜ invoke>'


def model_reply(text):
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}], "usage": {}}


def http_error(code):
    return urllib.error.HTTPError("https://relay.invalid/v1", code, "x", {}, None)


class OfflineCase(unittest.TestCase):
    def setUp(self):
        for name in ("_HISTORY", "_CHAT_HISTORY", "_HISTORY_GENERATION"):
            self.patch(s, name, {})
        for owner, name in ((socket.socket, "connect"), (socket, "create_connection")):
            self.patch(owner, name, mock.Mock(side_effect=AssertionError("network disabled")))
        self.patch(s.SHARED_KNOWLEDGE, "context", mock.Mock(return_value=""))
        # Pin the production shape: Gemini primary, DeepSeek fallback switched on.
        self.patch(s, "TEXT_PROVIDER", "gemini")
        self.patch(s, "TEXT_FALLBACK", "deepseek")

    def patch(self, owner, name, value):
        patcher = mock.patch.object(owner, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def chat(self, text="晚上好"):
        return s.run_agent(text, "u", False, "g", social_only=True)


class ToolMarkupTests(OfflineCase):
    def test_markup_patterns(self):
        leaked = (DSML, "<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function",
                  "<|im_start|>assistant", '<tool_call>{"name": "run_rcon"}</tool_call>',
                  '<function_calls><invoke name="run_rcon">', '{"tool_calls": [{"id": 1}]}',
                  'default_api.run_rcon(command="list")')
        ordinary = ("今晚高温炉别忘了加燃料", "<玩家A> 你好", "a < b 而且 c > d",
                    "[CQ:at,qq=1] 在吗", "invoke 这个词怎么读", "tool call 是什么意思")
        for text in leaked:
            with self.subTest(text=text):
                self.assertTrue(s._leaked_tool_markup(text))
        for text in ordinary:
            with self.subTest(text=text):
                self.assertFalse(s._leaked_tool_markup(text))

    def test_leaked_chat_reply_is_replaced_and_not_remembered(self):
        with mock.patch.object(s, "_text_post", return_value=model_reply("好的。" + DSML)):
            answer, usage, used = self.chat("服务器现在几个人")
        self.assertEqual(s._chat_failure_reply(), answer)
        self.assertFalse(usage.model_answer)
        self.assertFalse(used)
        self.assertFalse(any(rows for rows, _gen in s._CHAT_HISTORY.values()))

    def test_leaked_serious_reply_never_reaches_the_group(self):
        with mock.patch.object(s, "_text_post", return_value=model_reply(DSML)):
            answer, usage, _used = s.run_agent("解释一下红石中继器的延迟原理", "u", False, "g")
        self.assertNotIn("DSML", answer)
        self.assertFalse(usage.model_answer)

    def test_ordinary_reply_passes(self):
        with mock.patch.object(s, "_text_post", return_value=model_reply("晚上好呀")):
            answer, usage, _used = self.chat()
        self.assertEqual("晚上好呀", answer)
        self.assertTrue(usage.model_answer)


class DeepSeekFallbackTests(OfflineCase):
    def test_transport_failure_retries_chat_once_on_deepseek(self):
        with mock.patch.object(s, "_text_post", side_effect=http_error(503)) as primary, \
                mock.patch.object(s, "_post", return_value=model_reply("晚上好呀")) as fallback:
            answer, usage, used = self.chat()
        self.assertEqual("晚上好呀", answer)
        self.assertTrue(usage.model_answer)
        self.assertFalse(used)
        primary.assert_called_once()
        fallback.assert_called_once()
        url, key, request = fallback.call_args.args[:3]
        self.assertEqual(s.DEEPSEEK_URL, url)
        self.assertEqual(s.DEEPSEEK_KEY, key)
        self.assertEqual(s.DEEPSEEK_FALLBACK_MODEL, request["model"])
        self.assertEqual({"type": "enabled"}, request["thinking"])
        self.assertEqual("low", request["reasoning_effort"])
        self.assertFalse(request["stream"])
        self.assertNotIn("tools", request)
        self.assertFalse([k for k in request if k.startswith("_")])
        self.assertFalse([k for m in request["messages"] for k in m if k.startswith("_")])
        # A successful fallback reply is a normal answer and enters chat memory.
        self.assertTrue(any(rows for rows, _gen in s._CHAT_HISTORY.values()))

    def test_final_failures_do_not_fall_back(self):
        cases = ((s.RequestTimeout("budget"), s._chat_failure_reply(timeout=True)),
                 (http_error(400), s._chat_failure_reply()),
                 (http_error(401), s._chat_failure_reply()),
                 (ValueError("Expecting value"), s._chat_failure_reply()))
        for error, expected in cases:
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(s, "_text_post", side_effect=error), \
                    mock.patch.object(s, "_post") as fallback:
                answer, _usage, _used = self.chat()
            self.assertEqual(expected, answer)
            fallback.assert_not_called()

    def test_switch_off_or_deepseek_primary_means_no_fallback(self):
        for name, value in (("TEXT_FALLBACK", ""), ("TEXT_PROVIDER", "deepseek")):
            with self.subTest(**{name: value}), mock.patch.object(s, name, value), \
                    mock.patch.object(s, "_text_post", side_effect=http_error(503)), \
                    mock.patch.object(s, "_post") as fallback:
                answer, _usage, _used = self.chat()
            self.assertEqual(s._chat_failure_reply(), answer)
            fallback.assert_not_called()

    def test_failure_classes(self):
        worthy = (http_error(503), http_error(502), http_error(429),
                  urllib.error.URLError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF"),
                  ConnectionResetError(10054, "reset"))
        final = (http_error(400), http_error(403), socket.timeout("timed out"),
                 s.RequestTimeout("x"), s.RequestBusy("x"), ValueError("x"))
        for error in worthy:
            with self.subTest(error=repr(error)):
                self.assertTrue(s._fallback_worthy(error))
        for error in final:
            with self.subTest(error=repr(error)):
                self.assertFalse(s._fallback_worthy(error))

    def test_tool_history_is_not_replayed_on_deepseek(self):
        self.assertTrue(s._fallback_payload_ok({"messages": [
            {"role": "system", "content": "x"}, {"role": "user", "content": "y"}]}))
        self.assertFalse(s._fallback_payload_ok({"messages": [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]}]}))
        self.assertFalse(s._fallback_payload_ok({"messages": [
            {"role": "tool", "content": "z", "tool_call_id": "1"}]}))
        self.assertFalse(s._fallback_payload_ok({"messages": [
            {"role": "user", "content": [{"type": "text", "text": "x"}]}]}))


class AmbientSilenceTests(OfflineCase):
    def reply(self, uid, *, explicit, model_answer):
        usage = mock.Mock(calls=0, model_answer=model_answer)
        with mock.patch.object(s, "run_agent", return_value=(s._chat_failure_reply(), usage, False)):
            return s.sanae_reply("晚上好", uid, group_id="g", force=True, social_only=True,
                                 explicit_interaction=explicit, include_usage_footer=False)

    def test_uninvited_failure_stays_quiet(self):
        self.assertIsNone(self.reply("ambient-fail", explicit=False, model_answer=False))

    def test_invited_failure_still_answers(self):
        self.assertEqual(s._chat_failure_reply(),
                         self.reply("direct-fail", explicit=True, model_answer=False))

    def test_uninvited_model_answer_is_delivered(self):
        self.assertEqual(s._chat_failure_reply(),
                         self.reply("ambient-ok", explicit=False, model_answer=True))


class OutcomeMetadataTests(OfflineCase):
    def reply(self, uid, *, explicit, answer='好呀', model_answer=True, failure='', fallback=False):
        usage = mock.Mock(calls=0, model_answer=model_answer, failure=failure, text_fallback=fallback)
        meta = {}
        with mock.patch.object(s, 'run_agent', return_value=(answer, usage, False)):
            result = s.sanae_reply('晚上好', uid, group_id='g', force=True, social_only=True,
                                   explicit_interaction=explicit, include_usage_footer=False,
                                   reply_metadata=meta)
        return result, meta

    def test_outcomes_tell_silence_failure_and_answers_apart(self):
        _r, meta = self.reply('o-silent', explicit=True, answer='[SILENT]')
        self.assertEqual('silent', meta['outcome'])
        _r, meta = self.reply('o-quiet', explicit=False, answer=s._chat_failure_reply(),
                              model_answer=False, failure='HTTPError')
        self.assertEqual(('quiet-failure', 'HTTPError'), (meta['outcome'], meta['failure']))
        result, meta = self.reply('o-ok', explicit=True, fallback=True)
        self.assertEqual(('好呀', 'answered', True), (result, meta['outcome'], meta['text_fallback']))

    def test_run_agent_names_the_failure_and_the_fallback(self):
        with mock.patch.object(s, '_text_post', side_effect=http_error(503)), \
                mock.patch.object(s, 'TEXT_FALLBACK', ''):
            _a, usage, _u = self.chat()
        self.assertEqual('HTTPError', usage.failure)
        with mock.patch.object(s, '_text_post', return_value=model_reply(DSML)):
            _a, usage, _u = self.chat()
        self.assertEqual('markup', usage.failure)
        with mock.patch.object(s, '_text_post', side_effect=http_error(503)), \
                mock.patch.object(s, '_post', return_value=model_reply('嗯')):
            _a, usage, _u = self.chat()
        self.assertTrue(usage.text_fallback)
        self.assertEqual('', usage.failure)


class SocialOutcomeLogTests(unittest.TestCase):
    def run_thread(self, *, reply='好呀', meta=None, muted=False):
        event = {'group_id': bridge.SOCIAL_LITE_GROUP, 'user_id': 2001, 'message_id': 1,
                 'raw_message': '晚上好', 'sender': {'nickname': 'member'}}

        def fake_reply(*args, reply_metadata=None, **kwargs):
            reply_metadata.update(meta or {'model_answer': True, 'outcome': 'answered'})
            return reply

        out = io.StringIO()
        with mock.patch.object(bridge._sanae_ai, 'sanae_reply', side_effect=fake_reply), \
                mock.patch.object(bridge.FEEDBACK_STORE, 'strategy_hint', return_value=''), \
                mock.patch.object(bridge.FEEDBACK_STORE, 'style_override', return_value=''), \
                mock.patch.object(bridge.SOCIAL_LITE, 'expression_style_hint', return_value=''), \
                mock.patch.object(bridge.SOCIAL_LITE, 'closing_hint', return_value=''), \
                mock.patch.object(bridge.SOCIAL_LITE, 'is_explicit_interaction', return_value=False), \
                mock.patch.object(bridge.STICKER_CATALOG, 'recommend', return_value=[]), \
                mock.patch.object(bridge.CONVERSATION_MEMORY, 'context', return_value=''), \
                mock.patch.object(bridge, '_lesson_context', return_value=''), \
                mock.patch.object(bridge, '_expression_context', return_value=''), \
                mock.patch.object(bridge, '_silent_image_context', return_value=''), \
                mock.patch.object(bridge, '_fun_recently_muted', return_value=muted), \
                mock.patch.object(bridge, '_commit_social_outgoing'), \
                mock.patch.object(bridge, '_send_social_answer', return_value=True) as send, \
                contextlib.redirect_stdout(out):
            bridge.BridgeHandler._social_reply_thread(None, event, 'ctx')
        return send, out.getvalue()

    def test_every_turn_leaves_one_outcome_line(self):
        send, log = self.run_thread()
        send.assert_called_once()
        self.assertIn('result=replied', log)
        send, log = self.run_thread(reply=None, meta={'outcome': 'quiet-failure', 'failure': 'HTTPError'})
        send.assert_not_called()
        self.assertIn('result=quiet-failure', log)
        self.assertIn('failure=HTTPError', log)

    def test_a_member_just_joke_muted_gets_no_reply(self):
        send, log = self.run_thread(muted=True)
        send.assert_not_called()
        self.assertIn('result=muted', log)


class BridgeAmbientCrashTests(unittest.TestCase):
    def run_thread(self, explicit):
        event = {"group_id": bridge.SOCIAL_LITE_GROUP, "user_id": 2001, "message_id": 1,
                 "raw_message": "晚上好", "sender": {"nickname": "member"}}
        with mock.patch.object(bridge.FEEDBACK_STORE, "strategy_hint", side_effect=RuntimeError("x")), \
                mock.patch.object(bridge.SOCIAL_LITE, "is_explicit_interaction", return_value=explicit), \
                mock.patch.object(bridge, "send_group_msg") as send:
            bridge.BridgeHandler._social_reply_thread(None, event, "ctx")
        return send

    def test_uninvited_crash_sends_nothing(self):
        self.run_thread(False).assert_not_called()

    def test_invited_crash_still_says_so(self):
        send = self.run_thread(True)
        send.assert_called_once()
        self.assertIn("没接稳", send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
