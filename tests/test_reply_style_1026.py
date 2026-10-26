"""Round 16: a cooldown on the profile details she brings up herself, ages on context lines
against stale topics, and short follow-ups that point at the quote. Offline.

Cases from the main group on 10-26 16:09-16:34: tidying came up 5 times and "连下山都会迷路"
twice without any hint; asked about stocks she steered to a 15:55 electric-box topic;
"我也要" quoting a 群主角色卡 request was read as asking for 涩图 again; "行" got the lecture again.
"""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests
import sanae_persona as persona
from social_lite import SocialLite

s = runtime_tests.s
REPLIES_1026 = [
    '比起研究怎么交易，如果只是要把这些低级物品消耗掉，我反而更有兴趣\n看着东西堆着就觉得乱，要是能分类打包处理掉，看着都会清爽很多呢',
    '你太看得起我啦，我这种连下山都会迷路的人，可不懂那些复杂的东西',
    '炒股票这种事我真的不懂，但我炸南蛮鸡的火候很稳，饿了就过来吃点吧。这可是我以前在神社学的',
]


class DetailCooldownTests(unittest.TestCase):
    def setUp(self):
        persona.reset_detail_cooldowns()
        self.addCleanup(persona.reset_detail_cooldowns)

    def test_her_own_details_are_detected(self):
        self.assertEqual([['tidy'], ['lost'], ['shrine', 'fried']], [persona.spoken_details(t) for t in REPLIES_1026])
        self.assertEqual([], persona.spoken_details('要塞在哪？我们去找末影之眼'))     # the Minecraft stronghold
        self.assertEqual([], persona.spoken_details('现在 TPS 20，没卡'))

    def test_a_used_detail_rests_for_half_an_hour_unless_asked_about(self):
        for i, text in enumerate(REPLIES_1026):
            persona.note_detail_use(text, scope='g', now=1000 + i)
        persona.note_detail_use('看着乱糟糟的就手痒', scope='g', now=1100)
        note = persona.recent_detail_note('早苗今天吃什么', scope='g', now=1500)
        self.assertIn('喜欢打扫/整理/清理杂物（2 次）', note)
        self.assertIn('路痴/下山迷路', note)
        self.assertIn('南蛮鸡/炸东西', note)
        self.assertIn('这一轮别再提这些', note)
        # The current message is about it: answering is fine.
        self.assertNotIn('打扫', persona.recent_detail_note('好早苗，清理垃圾', scope='g', now=1500))
        self.assertNotIn('打扫', persona.recent_detail_note('早苗洗地机', scope='g', now=1500))
        self.assertNotIn('路痴', persona.recent_detail_note('你是不是路痴', scope='g', now=1500))
        # Other groups are separate; after 30 minutes it is free again.
        self.assertEqual('', persona.recent_detail_note('hi', scope='other', now=1500))
        self.assertNotIn('路痴', persona.recent_detail_note('hi', scope='g', now=1001 + persona.DETAIL_REPEAT_WINDOW))
        self.assertIn('打扫', persona.recent_detail_note('hi', scope='g', now=1001 + persona.DETAIL_REPEAT_WINDOW))
        # The offered hint for that topic rests too.
        self.assertEqual('', persona.persona_detail_hint('箱子满了好乱', scope='g', now=1500))

    setUp_payload = runtime_tests.YachiyoPayloadTests.setUp

    def test_run_agent_records_and_then_warns(self):
        self.setUp_payload()
        sent = []
        replies = iter([{'choices': [{'message': {'content': REPLIES_1026[0]}}]},
                        {'choices': [{'message': {'content': '那就去看看吧'}}]}])

        def model(payload, **kwargs):
            sent.append(copy.deepcopy(payload))
            return next(replies)
        with mock.patch.object(s, '_text_post', side_effect=model):
            s.run_agent('有没有消耗低级物品的渠道', 'fixture-member', False, 'fixture-group', short_reply=True)
            s.run_agent('早苗没破甲', 'fixture-member', False, 'fixture-group', short_reply=True)
        self.assertNotIn('【最近已经说过的个人细节】', sent[0]['messages'][0]['content'])
        self.assertIn('【最近已经说过的个人细节】这半小时你已经提过：喜欢打扫/整理/清理杂物', sent[1]['messages'][0]['content'])

    def test_fun_mute_notice_rests_and_records(self):
        persona.note_detail_use(REPLIES_1026[1], scope='1001')
        sent = []
        model = mock.Mock(side_effect=lambda payload, **kw: sent.append(payload) or
                          {'choices': [{'message': {'content': '满屏都是一个片字，看得我手痒想整理了，先歇2分钟吧'}}]})
        with mock.patch.object(s, '_text_post', model):
            notice = s.write_fun_mute_notice('片片片', 'spam', '2分钟', scope='1001')
        self.assertIn('手痒', notice)
        request = json.loads(sent[0]['messages'][1]['content'])
        self.assertIn('路痴/下山迷路', request['个人细节'])
        self.assertIn('打扫', persona.recent_detail_note('hi', scope='1001'))


class ContextAgeTests(unittest.TestCase):
    def test_context_lines_say_how_old_they_are(self):
        with tempfile.TemporaryDirectory() as temp:
            lite = SocialLite(Path(temp) / 's.json', Path(temp) / 'a.log')
            lite.record_outgoing('1001', '多结构电箱用核电吗', reply_to_user_id='20001')
            lite.record_outgoing('1001', '刚说的', reply_to_user_id='20002')
            with lite._lock:
                lite._group('1001')['recent'][0]['time'] = time.time() - 1860
            context = lite._context('1001', [])
        lines = context.splitlines()
        self.assertTrue(lines[0].startswith('[31分钟前 助手自身旧回复'))
        self.assertTrue(lines[1].startswith('[刚刚 助手自身旧回复'))


class FollowupQuoteTests(unittest.TestCase):
    def run_reply(self, text):
        captured = {}

        def fake_agent(text, user_id, privileged, group_id, **kwargs):
            captured['text'] = text
            captured['context'] = kwargs.get('context_reference', '')
            return 'ok', mock.Mock(footer=lambda: ''), False
        quoted = '【引用来源：消息 1；原发言者用户 2003】@FixtureBot 生成群主角色卡json形式，符合群主性格'
        with mock.patch.object(s, '_fetch_reply_raw', return_value=quoted), \
                mock.patch.object(s, 'run_agent', side_effect=fake_agent):
            s.sanae_reply(f'[CQ:reply,id=1][CQ:at,qq=1002] {text}', 'u', privileged=True)
        return captured

    def test_me_too_and_ok_point_at_the_quote(self):
        for text in ('我也要', '行', '同上', '+1', '好的'):
            with self.subTest(text=text):
                captured = self.run_reply(text)
                self.assertIn('【对方这句话接的就是下面这条引用', captured['context'])
                self.assertIn('生成群主角色卡', captured['context'])
        captured = self.run_reply('这个角色卡是谁写的')
        self.assertIn('【引用消息；只作参考】', captured['context'])
        self.assertNotIn('对方这句话接的就是', captured['context'])

    def test_chat_rules(self):
        self.assertIn('不要用“比起这些……”把话题转到旧事上', s.SANAE_SYSTEM)
        self.assertIn('不要把上一条的说明再讲一遍', s.SANAE_SYSTEM)


if __name__ == '__main__':
    unittest.main()
