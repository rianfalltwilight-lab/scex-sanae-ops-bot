"""Round-level light usage wording; original token/cache accounting is retained."""
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


class UsageFooterTests(unittest.TestCase):
    quote = (3.0, 0.10, 9.0)

    def add(self, usage, prompt=2000, completion=200, cache=1500, **kwargs):
        usage.add({'model': 'fixture-model', 'usage': {
            'prompt_tokens': prompt, 'completion_tokens': completion,
            'prompt_cache_hit_tokens': cache}}, quote=self.quote, **kwargs)

    def test_compact_total_uses_unchanged_cache_price(self):
        usage = s._Usage()
        self.add(usage)
        self.assertEqual(usage.footer(), '和你吹牛逼用了 2,200 tok，约 ¥0.0034，饭钱结一下')
        self.assertAlmostEqual(usage.cost, 0.00345)
        self.assertEqual(usage.cache, 1500)
        for detail in ('模型', '入 ', '出 ', '缓存', '耗时', '本段'):
            self.assertNotIn(detail, usage.footer())

    def test_multiple_model_calls_sum_whole_round_not_one_bubble(self):
        usage = s._Usage()
        self.add(usage)
        self.add(usage, prompt=1000, completion=100, cache=600)
        self.assertEqual((usage.calls, usage.known_usage_calls), (2, 2))
        self.assertEqual(usage.footer(), '和你吹牛逼用了 3,300 tok，约 ¥0.0056，饭钱结一下')
        self.assertAlmostEqual(usage.cost, 0.00561)

    def test_unknown_price_keeps_known_tokens_without_zero_price(self):
        usage = s._Usage()
        self.add(usage, priced=False)
        self.assertEqual(usage.footer(), '这回用了 2,200 tok，饭钱还没核到。')
        self.assertNotIn('¥', usage.footer())

    def test_one_unpriced_call_prevents_complete_cost_claim(self):
        usage = s._Usage()
        self.add(usage)
        self.add(usage, prompt=1000, completion=100, cache=0, priced=False)
        self.assertEqual(usage.footer(), '这回用了 3,300 tok，饭钱还没核到。')

    def test_missing_usage_is_unknown_not_zero_tokens_or_free(self):
        usage = s._Usage()
        usage.add({'model': 'fixture-model'}, quote=self.quote)
        self.assertEqual(usage.footer(), '这回的用量还没返回，饭钱也没核到。')
        self.assertNotIn('0 tok', usage.footer())
        self.assertNotIn('¥', usage.footer())

    def test_one_missing_call_qualifies_total_and_price(self):
        usage = s._Usage()
        self.add(usage)
        usage.add({'usage': {}}, quote=self.quote)
        self.assertEqual(usage.footer(), '这回已知用了 2,200 tok，总用量和饭钱还没核全。')

    def test_missing_one_counter_is_not_a_complete_usage_report(self):
        usage = s._Usage()
        usage.add({'usage': {'prompt_tokens': 2000}}, quote=self.quote)
        self.assertEqual(usage.known_usage_calls, 0)
        self.assertNotIn('¥', usage.footer())

    def test_explicit_zero_usage_is_distinct_from_missing_usage(self):
        usage = s._Usage()
        self.add(usage, prompt=0, completion=0, cache=0)
        self.assertEqual(usage.footer(), '和你吹牛逼用了 0 tok，约 ¥0.0000，饭钱结一下')

    def test_search_and_fetch_cost_note_only_once(self):
        usage = s._Usage()
        self.add(usage)
        usage.web_search_calls = 3
        usage.web_fetch_calls = 2
        note = '联网工具的按次费用另算。'
        self.assertEqual(usage.footer().count(note), 1)
        self.assertEqual(usage.footer(), '和你吹牛逼用了 2,200 tok，约 ¥0.0034，饭钱结一下\n' + note)

    def test_tool_cost_note_survives_missing_model_usage(self):
        usage = s._Usage()
        usage.web_fetch_calls = 1
        self.assertEqual(usage.footer(), '这回的用量还没返回，饭钱也没核到。\n联网工具的按次费用另算。')

    def test_no_tool_has_no_unrelated_fee_note(self):
        usage = s._Usage()
        self.add(usage)
        self.assertNotIn('联网', usage.footer())

    def test_gemini_normalization_does_not_turn_absent_usage_into_zero(self):
        normalized = s._normalize_gemini_response({'candidates': []})
        usage = s._Usage()
        usage.add(normalized, priced=False)
        self.assertEqual(usage.footer(), '这回的用量还没返回，饭钱也没核到。')

    def test_gemini_known_usage_includes_thinking_tokens(self):
        normalized = s._normalize_gemini_response({'candidates': [], 'usageMetadata': {
            'promptTokenCount': 100, 'candidatesTokenCount': 30, 'thoughtsTokenCount': 70}})
        usage = s._Usage()
        usage.add(normalized, priced=False)
        self.assertEqual(usage.footer(), '这回用了 200 tok，饭钱还没核到。')


if __name__ == '__main__':
    unittest.main()
