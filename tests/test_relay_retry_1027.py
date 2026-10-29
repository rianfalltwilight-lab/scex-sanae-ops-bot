"""Round 23: retry the relay's transient 500s, mod-count questions by name, no opening
self-introduction. Offline.

10-27 12:10 (machine time) "早苗你能把服务器调成白天吗？": run_rcon ran, then the next model call got
"500 Upstream gateway error" on both endpoints and she answered "这次没能完成（HTTP 500）". Sending
that same request three times got 500, 500, OK.
"""
import json
import unittest
import urllib.error
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests

s = runtime_tests.s
OK = json.dumps({'candidates': [{'content': {'parts': [{'text': '调好了'}]}}]})


def response(text):
    handle = mock.MagicMock()
    handle.__enter__.return_value.read.return_value = text.encode('utf-8')
    return handle


class RetryTests(unittest.TestCase):
    def run_post(self, outcomes, stream=True):
        urls, waits = [], []
        outcomes = list(outcomes)

        def open_(request, **kwargs):
            urls.append('stream' if 'stream' in request.full_url else 'plain')
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return response(outcome)
        opener = mock.Mock()
        opener.open.side_effect = open_
        with mock.patch.object(s.urllib.request, 'build_opener', return_value=opener), \
                mock.patch.object(s, 'GEMINI_KEY', 'test-placeholder'), mock.patch.object(s, 'GEMINI_STREAM', stream), \
                mock.patch.object(s, 'GEMINI_RETRIES', 2), mock.patch.object(s, '_retry_sleep', side_effect=waits.append):
            try:
                result = s._gemini_post({'model': 'm', 'messages': [{'role': 'user', 'content': 'x'}]})
            except Exception as exc:
                result = exc
        return result, urls, waits

    @staticmethod
    def http(code):
        return urllib.error.HTTPError('https://relay/x', code, 'Upstream gateway error', {}, None)

    def test_the_relays_500s_are_retried(self):
        result, urls, waits = self.run_post([self.http(500), self.http(500), OK])
        self.assertEqual('调好了', result['choices'][0]['message']['content'])
        self.assertEqual(['stream', 'plain', 'plain'], urls)
        self.assertEqual([1.5], waits)
        result, urls, waits = self.run_post([self.http(500)] * 4)
        self.assertIsInstance(result, urllib.error.HTTPError)
        self.assertEqual(['stream', 'plain', 'plain', 'plain'], urls)
        self.assertEqual([1.5, 3.0], waits)

    def test_connection_drops_are_retried_too(self):
        result, urls, _ = self.run_post([urllib.error.URLError('reset'), OK], stream=False)
        self.assertEqual('调好了', result['choices'][0]['message']['content'])
        self.assertEqual(['plain', 'plain'], urls)

    def test_quota_and_request_errors_are_not(self):
        for code in (400, 429):
            with self.subTest(code=code):
                result, urls, waits = self.run_post([self.http(code)], stream=False)
                self.assertIsInstance(result, urllib.error.HTTPError)
                self.assertEqual(['plain'], urls)
                self.assertEqual([], waits)
        result, urls, _ = self.run_post([s.RequestTimeout('budget')], stream=False)
        self.assertIsInstance(result, s.RequestTimeout)
        self.assertEqual(['plain'], urls)

    def test_no_retry_without_time_left(self):
        with mock.patch.object(s, 'remaining_timeout', return_value=5):
            result, urls, waits = self.run_post([self.http(500), self.http(500), OK])
        self.assertIsInstance(result, urllib.error.HTTPError)
        self.assertEqual(['stream', 'plain'], urls)
        self.assertEqual([], waits)


class SmallFixTests(unittest.TestCase):
    def test_mod_count_questions_are_queries(self):
        for text in ('早苗，本服拔刀剑总共有几个附属模组？', '本服有多少个和奇点迭代联动的模组', '机械动力有哪些附属模组'):
            with self.subTest(text=text):
                self.assertTrue(s._has_server_query_intent(text))
        self.assertFalse(s._has_server_query_intent('模组作者是谁'))

    def test_opening_self_introduction_is_dropped(self):
        self.assertEqual('看到你问起起步的事……', s._strip_self_intro('我是八千代。看到你问起起步的事……'))
        self.assertEqual('先跟着任务书走', s._strip_self_intro('你好，我是八千代！先跟着任务书走'))
        self.assertEqual('我是八千代。', s._strip_self_intro('我是八千代。'))
        self.assertEqual('现在是白天，我是八千代这件事不用再说了', s._strip_self_intro('现在是白天，我是八千代这件事不用再说了'))


if __name__ == '__main__':
    unittest.main()
