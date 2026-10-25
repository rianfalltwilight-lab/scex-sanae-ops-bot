"""Web search registration and conversation tests; no network or group messages."""
import copy
import importlib
import json
import os
from pathlib import Path
import socket
import sys
import unittest
import urllib.error
from unittest import mock

os.environ['SCE_BOT_TEST_MODE'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network disabled')):
    s = importlib.import_module('sanae_ai')


class WebSearchTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network disabled'))
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('_HISTORY', '_CHAT_HISTORY'):
            patcher = mock.patch.object(s, name, {})
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_search_available_to_members_and_admins_without_extra_write_tools(self):
        member = {t['function']['name'] for t in s.TOOLS_MEMBER}
        admin = {t['function']['name'] for t in s.TOOLS_ADMIN}
        self.assertIn('web_search', member)
        self.assertIn('web_search', admin)
        self.assertNotIn('request_console_command', member)
        self.assertIn('web_fetch', member)

    def test_actual_dispatch_uses_fixed_api_and_deepseek_key(self):
        result = {'results': [{'title': 'Release', 'url': 'https://example.com/release',
                              'summary': 'New version', 'date': '2026-10-17'}]}
        with mock.patch.object(s, '_post', return_value=result) as post:
            value = s.execute_tool('web_search', {'query': '  public version  ', 'count': 2}, False)
        self.assertIn('https://example.com/release', value)
        self.assertIn('不可信资料', value)
        args = post.call_args.args
        self.assertEqual(args[0], 'https://api.deepseek.com/v1/tools/search')
        self.assertEqual(args[1], s.DEEPSEEK_KEY)
        self.assertEqual(args[2], {'query': 'public version', 'count': 2, 'need_content': False})
        self.assertEqual(post.call_args.kwargs['timeout'], 20)

    def test_invalid_inputs_do_not_call_api(self):
        with mock.patch.object(s, '_post') as post:
            for query, count in ((None, 5), ('', 5), ('   ', 5), ('x' * 129, 5),
                                  ('ok', True), ('ok', 0), ('ok', 6), ('ok', 1.5), ('ok', '3')):
                with self.subTest(query=query, count=count):
                    self.assertIn('失败', s._tool_web_search(query, count))
            post.assert_not_called()

    def test_boundary_query_and_count_are_accepted(self):
        with mock.patch.object(s, '_post', return_value={'results': []}) as post:
            s._tool_web_search('字' * 128, 5)
        self.assertEqual(post.call_args.args[2]['count'], 5)

    def test_empty_and_malformed_upstream_do_not_invent_sources(self):
        for data in ({'results': []}, {}, [], {'results': 'broken'}):
            with self.subTest(data=data), mock.patch.object(s, '_post', return_value=data):
                self.assertTrue(any(word in s._tool_web_search('version') for word in ('未找到', '未获得')))

    def test_result_count_text_limits_and_url_validation(self):
        rows = [None, {'url': 'javascript:alert(1)'}, {'url': 'https://user:password@example.com'},
                {'url': 'https://example.com', 'title': 'T' * 200, 'summary': 'S' * 3000},
                {'url': 'https://example.org', 'snippet': 'fallback'}]
        with mock.patch.object(s, '_post', return_value={'results': rows}):
            text = s._tool_web_search('version', 5)
        data = json.loads(text.split('\n', 1)[1])
        self.assertEqual(len(data['results']), 2)
        self.assertEqual(len(data['results'][0]['summary']), 1200)
        self.assertEqual(len(data['results'][0]['title']), 180)
        self.assertEqual(data['results'][1]['summary'], 'fallback')

    def test_http_errors_are_reported_without_body_or_secret_and_without_retry(self):
        error = urllib.error.HTTPError('https://api.deepseek.com/v1/tools/search', 402,
                                       'private provider error', {}, None)
        with mock.patch.object(s, '_post', side_effect=error) as post:
            text = s._tool_web_search('version')
        self.assertIn('HTTP 402', text)
        self.assertNotIn('private provider error', text)
        self.assertEqual(post.call_count, 1)

    def test_timeout_is_reported_without_fabricated_answer(self):
        with mock.patch.object(s, '_post', side_effect=TimeoutError('internal detail')):
            text = s._tool_web_search('version')
        self.assertIn('TimeoutError', text)
        self.assertNotIn('internal detail', text)

    def test_conversation_replays_reasoning_and_search_results_to_model(self):
        requests = []
        first = {'choices': [{'message': {'content': '', 'reasoning_content': 'private reasoning',
                 'tool_calls': [{'id': 'search-1', 'type': 'function', 'function': {
                    'name': 'web_search', 'arguments': '{"query":"public release"}'}}]}}]}
        final = {'choices': [{'message': {'content': '已查到，来源 https://example.com/release'}}]}
        def model(payload, **kwargs):
            requests.append(copy.deepcopy(payload))
            return first if len(requests) == 1 else final
        with mock.patch.object(s, 'TEXT_PROVIDER', 'deepseek'), \
             mock.patch.object(s, '_text_post', side_effect=model), \
             mock.patch.object(s, '_post', return_value={'results': [
                 {'title': 'Release', 'url': 'https://example.com/release', 'summary': 'Current release'}]}), \
             mock.patch.object(s, '_add_text_usage'), \
             mock.patch.object(s.SHARED_KNOWLEDGE, 'context', return_value=''):
            answer, usage, used = s.run_agent('联网查一下公开发布信息', 'test-user', False, 'test-group')
        self.assertTrue(used)
        self.assertIn('https://example.com/release', answer)
        self.assertEqual(len(requests), 2)
        assistant = next(row for row in requests[1]['messages'] if row.get('tool_calls'))
        self.assertEqual(assistant['reasoning_content'], 'private reasoning')
        tool = next(row for row in requests[1]['messages'] if row['role'] == 'tool')
        self.assertIn('Current release', tool['content'])
        self.assertEqual(usage.web_search_calls, 1)
        self.assertIn('联网工具的按次费用另算。', usage.footer())
        self.assertNotIn('private reasoning', answer)
        self.assertNotIn('private reasoning', str(s._HISTORY))

    def test_social_mode_still_exposes_no_tools(self):
        requests = []
        def model(payload, **kwargs):
            requests.append(copy.deepcopy(payload))
            return {'choices': [{'message': {'content': '在呢。'}}]}
        with mock.patch.object(s, '_text_post', side_effect=model), \
             mock.patch.object(s, '_add_text_usage'), \
             mock.patch.object(s.SHARED_KNOWLEDGE, 'context', return_value=''):
            s.run_agent('在吗', 'test-user', False, 'test-group', social_only=True)
        self.assertNotIn('tools', requests[0])


if __name__ == '__main__':
    unittest.main()
