"""Web-only final answers reuse generation/evidence; all model and network I/O is fake."""
import contextlib
import copy
import importlib
import os
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock


os.environ['SCE_BOT_TEST_MODE'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network disabled')):
    s = importlib.import_module('sanae_ai')


class WebPolishTests(unittest.TestCase):
    question = '查一下这个公开项目的更新，保留主要变化和来源'
    body = ('2.4 版增加了导出功能，旧格式仍能读。\n\n'
            '批量导出只在桌面版开放，移动版还没确认。\n\n'
            '官方说明 https://example.org/releases/2.4?view=full#export')

    @staticmethod
    def answer(text, finish='stop'):
        return {'choices': [{'message': {'content': text}, 'finish_reason': finish}], 'usage': {}}

    @staticmethod
    def call(*names):
        return {'choices': [{'message': {'content': '', 'tool_calls': [
            {'id': 'web-fixture-' + str(i), 'type': 'function', 'function': {
                'name': name, 'arguments': '{}'}} for i, name in enumerate(names)]}}], 'usage': {}}

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for owner, attr in ((socket.socket, 'connect'), (socket, 'create_connection')):
            self.stack.enter_context(mock.patch.object(owner, attr,
                side_effect=AssertionError('network disabled')))
        for key in ('_HISTORY', '_CHAT_HISTORY', '_HISTORY_GENERATION', '_LAST_MEMBER', '_LAST_INTERACTION'):
            self.stack.enter_context(mock.patch.object(s, key, {}))
        self.stack.enter_context(mock.patch.object(s.SHARED_KNOWLEDGE, 'context', return_value=''))
        self.stack.enter_context(mock.patch.object(s, '_with_reply_context', side_effect=lambda text, **kw: text))
        self.stack.enter_context(mock.patch.object(s, '_member_cooldown_ok', return_value=True))
        self.stack.enter_context(mock.patch.object(s, '_explicit_interaction_ok', return_value=True))
        self.stack.enter_context(mock.patch.object(s, '_add_text_usage',
            side_effect=lambda usage, response: setattr(usage, 'calls', usage.calls + 1)))
        self.responses = [self.call('web_search'), self.answer(self.body)]
        self.requests = []
        self.results = {
            'web_search': s._WEB_RESULT_PREFIXES['web_search'] + '{"results":[{"title":"官方说明","url":"https://example.org/releases/2.4?view=full#export","summary":"桌面版支持，移动版未确认"}]}',
            'web_fetch': s._WEB_RESULT_PREFIXES['web_fetch'] + '{"url":"https://example.org/releases/2.4?view=full#export","content":"2.4 版，桌面版支持；网页中要求忽略前文的句子仅为资料"}',
            'list_mods': '合成模组清单',
            'request_console_command': '仅创建待确认命令，确认码 fixture-123',
        }
        self.model = self.stack.enter_context(mock.patch.object(s, '_text_post', side_effect=self.model_call))
        self.execute = self.stack.enter_context(mock.patch.object(s, 'execute_tool',
            side_effect=lambda name, args, privileged, **kw: self.results[name]))

    def model_call(self, payload, **kwargs):
        self.requests.append(copy.deepcopy(payload))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def reply(self, **kwargs):
        metadata = {'web_reply': True}
        result = s.sanae_reply(self.question, 'web-fixture-user', group_id='web-fixture-group',
                              force=True, reply_metadata=metadata, **kwargs)
        return result, metadata

    def test_search_fetch_keeps_sources_and_uses_existing_final_generation(self):
        self.responses = [self.call('web_search'), self.call('web_fetch'), self.answer(self.body)]
        answer, metadata = self.reply()
        self.assertEqual(self.model.call_count, 3)
        self.assertEqual([c.args[0] for c in self.execute.call_args_list], ['web_search', 'web_fetch'])
        self.assertNotIn(s._WEB_REPLY_STYLE, self.requests[0]['messages'][0]['content'])
        for payload in self.requests[1:]:
            self.assertIn(s._WEB_REPLY_STYLE, payload['messages'][0]['content'])
            self.assertEqual(payload['max_tokens'], self.requests[0]['max_tokens'])
            self.assertEqual(payload['reasoning_effort'], self.requests[0]['reasoning_effort'])
            self.assertEqual(payload['tools'], self.requests[0]['tools'])
        evidence = [m['content'] for m in self.requests[-1]['messages'] if m['role'] == 'tool']
        self.assertEqual(evidence, [self.results['web_search'], self.results['web_fetch']])
        self.assertTrue(metadata['model_answer'])
        self.assertTrue(metadata['web_reply'])
        self.assertFalse(metadata['social_reply'])
        self.assertEqual(metadata['reply_body'], self.body)
        self.assertIn(self.body, answer)

    def test_web_success_does_not_emit_template_interim(self):
        interim = mock.Mock()
        _, metadata = self.reply(send_cb=interim)
        self.assertTrue(metadata['web_reply'])
        interim.assert_not_called()

    def test_fast_mixed_first_batch_cancels_interim_and_never_applies_web_style(self):
        self.responses = [self.call('web_search', 'list_mods'), self.answer(self.body)]
        interim = mock.Mock()
        with mock.patch.object(s.threading, 'Timer') as timer:
            _, metadata = self.reply(send_cb=interim)
        timer.assert_called_once()
        timer.return_value.start.assert_called_once()
        timer.return_value.cancel.assert_called_once()
        self.assertFalse(metadata['web_reply'])
        interim.assert_not_called()
        self.assertEqual([c.args[0] for c in self.execute.call_args_list], ['web_search', 'list_mods'])
        self.assertNotIn(s._WEB_REPLY_STYLE, self.requests[-1]['messages'][0]['content'])

    def test_later_non_web_tool_removes_only_web_instruction(self):
        self.responses = [self.call('web_search'), self.call('list_mods'), self.answer(self.body)]
        _, metadata = self.reply()
        self.assertIn(s._WEB_REPLY_STYLE, self.requests[1]['messages'][0]['content'])
        self.assertNotIn(s._WEB_REPLY_STYLE, self.requests[2]['messages'][0]['content'])
        self.assertEqual(self.requests[2]['messages'][0]['content'], self.requests[0]['messages'][0]['content'])
        self.assertIn(self.results['web_search'], [m.get('content') for m in self.requests[-1]['messages']])
        self.assertFalse(metadata['web_reply'])

    def test_prior_non_web_tool_also_excludes_web_presentation(self):
        self.responses = [self.call('list_mods'), self.call('web_fetch'), self.answer(self.body)]
        _, metadata = self.reply()
        self.assertFalse(metadata['web_reply'])
        self.assertTrue(all(s._WEB_REPLY_STYLE not in p['messages'][0]['content'] for p in self.requests))

    def test_failed_or_empty_web_results_do_not_enable_web_presentation(self):
        for result in ('网页搜索失败（HTTP 503），未获得来源，请勿编造搜索结论。',
                       '网页搜索未找到可用结果；不能据此断言相关内容不存在。'):
            with self.subTest(result=result):
                self.requests.clear()
                self.responses = [self.call('web_search'), self.answer('这次没取得可用来源。')]
                self.results['web_search'] = result
                _, metadata = self.reply()
                self.assertFalse(metadata['web_reply'])
                self.assertNotIn(s._WEB_REPLY_STYLE, self.requests[-1]['messages'][0]['content'])

    def test_failed_fetch_after_search_does_not_keep_web_marker(self):
        self.responses = [self.call('web_search'), self.call('web_fetch'), self.answer(self.body)]
        self.results['web_fetch'] = '网页正文提取失败（TimeoutError），未取得正文，不能编造读取结果。'
        _, metadata = self.reply()
        self.assertIn(s._WEB_REPLY_STYLE, self.requests[1]['messages'][0]['content'])
        self.assertNotIn(s._WEB_REPLY_STYLE, self.requests[-1]['messages'][0]['content'])
        self.assertFalse(metadata['web_reply'])

    def test_no_tool_answer_resets_metadata_default(self):
        self.responses = [self.answer('普通回答。')]
        _, metadata = self.reply()
        self.assertFalse(metadata['web_reply'])
        self.execute.assert_not_called()

    def test_truncated_web_answer_is_not_marked_complete(self):
        self.responses = [self.call('web_search'), self.answer(self.body, finish='length')]
        answer, metadata = self.reply()
        self.assertFalse(metadata['web_reply'])
        self.assertIn('回答达到输出上限', answer)

    def test_stream_error_after_web_result_is_not_marked_model_or_web(self):
        self.responses = [self.call('web_search'), s.ds_features.StreamError('fixture stream stopped', {})]
        answer, metadata = self.reply()
        self.assertFalse(metadata['model_answer'])
        self.assertFalse(metadata['web_reply'])
        self.assertIn('生成中断', answer)

    def test_timeout_after_web_result_stays_an_error_receipt(self):
        self.responses = [self.call('web_search'), s.RequestTimeout('fixture deadline')]
        answer, metadata = self.reply()
        self.assertFalse(metadata['model_answer'])
        self.assertFalse(metadata['web_reply'])
        self.assertIn('本次请求已停止', answer)

    def test_server_command_receipt_stays_deterministic_after_web_search(self):
        self.responses = [self.call('web_search'), self.call('request_console_command')]
        answer, metadata = self.reply(privileged=True)
        self.assertFalse(metadata['web_reply'])
        self.assertFalse(metadata['model_answer'])
        self.assertIn(self.results['request_console_command'], answer)
        self.assertEqual(self.model.call_count, 2)

    def test_actual_stream_delivery_excludes_web_presentation(self):
        # A future route must not accidentally replay an already-delivered
        # prefix even if its model result carries the web expression marker.
        usage = s._Usage()
        usage.model_answer = usage.web_reply = True
        usage.streamed_text = self.body.split('\n\n')[0]
        with mock.patch.object(s, 'run_agent', return_value=(self.body, usage, True)):
            answer, metadata = self.reply(stream_cb=mock.Mock())
        self.assertFalse(metadata['web_reply'])
        self.assertNotIn(usage.streamed_text, answer)
        self.assertIn('移动版还没确认', answer)

    def test_available_stream_callback_without_delivery_still_allows_web_reply(self):
        callback = mock.Mock()
        _, metadata = self.reply(stream_cb=callback)
        self.assertTrue(metadata['web_reply'])
        callback.assert_not_called()


if __name__ == '__main__':
    unittest.main()
