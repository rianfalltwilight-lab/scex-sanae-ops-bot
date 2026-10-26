"""DeepSeek as the main model: chat effort, official pricing page, daily usage ledger.

Offline: model calls, clocks and the ledger file are fakes or temp files.
"""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests

s = runtime_tests.s

PAGE_2026_10 = """<h2>模型细节</h2><table><tr><td>模型</td><td>deepseek-flash (1)</td><td>deepseek-v4-pro</td></tr>
<tr><td>价格 (2)</td><td>百万tokens输入 （缓存命中）</td><td>空闲时段</td><td>0.02元</td><td>0.15元</td></tr>
<tr><td>高峰时段</td><td>0.04元</td><td>0.30元</td></tr>
<tr><td>百万tokens输入 （缓存未命中）</td><td>空闲时段</td><td>1元</td><td>4.5元</td><td>高峰时段</td><td>2元</td><td>9.0元</td></tr>
<tr><td>百万tokens输出</td><td>空闲时段</td><td>4元</td><td>13.5元</td><td>高峰时段</td><td>8元</td><td>27.0元</td></tr></table>
<p>(1) 模型名请使用 deepseek-flash 。旧模型名 deepseek-v4-flash 、 deepseek-v4-flash-vision-exp 仍可调用。</p>
<p>(2) 北京时间周一至周五（不含中国法定节假日）9:00 - 12:00、14:00 - 18:00 为高峰时段；其余时段均为空闲时段。</p>"""


class PricingPageTests(unittest.TestCase):
    def test_new_page_layout_picks_the_flash_column_and_weekday_schedule(self):
        parsed = s._parse_pricing_page(PAGE_2026_10)
        self.assertEqual((1.0, 0.02, 4.0), parsed['offpeak'])
        self.assertEqual((2.0, 0.04, 8.0), parsed['peak'])
        self.assertEqual(((540, 720), (840, 1080)), parsed['windows'])
        self.assertTrue(parsed['weekdays_only'])
        self.assertTrue(s._price_is_peak(datetime(2026, 10, 26, 10, 0), parsed))       # Monday morning
        self.assertFalse(s._price_is_peak(datetime(2026, 10, 26, 20, 0), parsed))      # Monday evening
        self.assertFalse(s._price_is_peak(datetime(2026, 10, 31, 10, 0), parsed))      # Saturday

    def test_a_page_without_a_flash_column_is_rejected(self):
        with self.assertRaises(ValueError):
            s._parse_pricing_page(PAGE_2026_10.replace('deepseek-flash', 'deepseek-other')
                                  .replace('deepseek-v4-flash', 'x'))


class ChatEffortTests(unittest.TestCase):
    setUp = runtime_tests.YachiyoPayloadTests.setUp

    def test_chat_turns_use_the_configured_effort_and_tools_are_unaffected(self):
        with mock.patch.object(s, 'CHAT_EFFORT', 'high'):
            social = s.request_profile('今天晚风挺舒服的', social_only=True)
            short = s.request_profile('今天晚风挺舒服的', short_reply=True)
            query = s.request_profile('当前世界多少天了', short_reply=True, selected_server={'id': 'legacy'})
        self.assertEqual(('high', 'high'), (social['effort'], short['effort']))
        self.assertTrue(query['read_only_tool_required'])
        self.assertEqual('low', query['effort'])
        self.assertEqual('low', s.request_profile('今天晚风挺舒服的', social_only=True)['effort'])


class UsageLedgerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'deepseek-usage.json'
        for name, value in (('USAGE_LEDGER_PATH', str(self.path)), ('_USAGE_TODAY', {'day': None}),
                            ('_USAGE_CLOCK', {'saved': 0.0, 'logged': 0.0})):
            patcher = mock.patch.object(s, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.pricing = mock.patch.dict(s._PRICING, {'offpeak': (1.0, 0.02, 4.0), 'peak': (2.0, 0.04, 8.0),
                                                     'windows': ((540, 720), (840, 1080)), 'weekdays_only': True})
        self.pricing.start()
        self.addCleanup(self.pricing.stop)

    @staticmethod
    def response(prompt, cache, completion):
        return {'model': 'deepseek-flash', 'usage': {'prompt_tokens': prompt, 'prompt_cache_hit_tokens': cache,
                                                     'completion_tokens': completion}}

    def test_totals_cost_by_time_of_day_and_survive_a_restart(self):
        evening = datetime(2026, 10, 26, 20, 0, tzinfo=s._BEIJING).timestamp()
        morning = datetime(2026, 10, 26, 10, 0, tzinfo=s._BEIJING).timestamp()
        with mock.patch('builtins.print') as printed:
            s._record_deepseek_usage(self.response(5000, 3000, 100), now=evening)   # 2000*1 + 3000*0.02 + 100*4
            s._record_deepseek_usage(self.response(1000, 0, 0), now=morning)        # weekday peak: 1000*2
            s._record_deepseek_usage({'usage': {}}, now=evening)                    # nothing to count
        today = s._USAGE_TODAY
        self.assertEqual((2, 6000, 3000, 100), (today['calls'], today['prompt'], today['cache'], today['completion']))
        self.assertAlmostEqual((2000 + 60 + 400 + 2000) / 1e6, today['cost'])
        self.assertEqual({'deepseek-flash': 2}, today['models'])
        self.assertIn('[usage] 2026-10-26 DeepSeek 1 次', printed.call_args_list[0].args[0])
        saved = json.loads(self.path.read_text(encoding='utf-8'))['days']['2026-10-26']
        self.assertEqual(1, saved['calls'])                  # saved at most once a minute
        s._USAGE_TODAY.clear()
        s._USAGE_TODAY['day'] = None
        s._record_deepseek_usage(self.response(10, 0, 0), now=evening + 120)
        self.assertEqual(2, s._USAGE_TODAY['calls'])         # continued from the saved day

    def test_a_new_day_starts_fresh_and_logs_the_previous_total(self):
        day1 = datetime(2026, 10, 26, 23, 59, tzinfo=s._BEIJING).timestamp()
        with mock.patch('builtins.print') as printed:
            s._record_deepseek_usage(self.response(100, 0, 10), now=day1)
            s._record_deepseek_usage(self.response(100, 0, 10), now=day1 + 120)
        self.assertEqual('2026-10-27', s._USAGE_TODAY['day'])
        self.assertEqual(1, s._USAGE_TODAY['calls'])
        self.assertIn('2026-10-26 DeepSeek 1 次', printed.call_args_list[-1].args[0])
        days = json.loads(self.path.read_text(encoding='utf-8'))['days']
        self.assertEqual(['2026-10-26', '2026-10-27'], sorted(days))

    def test_only_deepseek_endpoints_are_counted(self):
        body = json.dumps(self.response(100, 0, 10)).encode('utf-8')
        opened = mock.MagicMock()
        opened.__enter__.return_value.read.return_value = body
        with mock.patch.object(s.urllib.request, 'urlopen', return_value=opened), \
                mock.patch.object(s, '_record_deepseek_usage') as record:
            s._post('https://api.deepseek.com/chat/completions', 'k', {})
            s._post('https://open.bigmodel.cn/api/paas/v4/chat/completions', 'k', {})
        record.assert_called_once()


if __name__ == '__main__':
    unittest.main()
