"""Round 17: reply routing and grounding defects from the main group on 10-26 16:55-17:44. Offline.

- An @ message that only quotes text went to the media channel, which called sanae_reply bare:
  no server, so the market tool refused and she said "没能成功连接到服务器"; no chat context,
  so she said "我是一个群里的 AI 角色" and lectured "我也要" / "行".
- "请推荐给我所有有概率实现一夜暴富的商品" right after her market answer, and "你推荐购买哪一支"
  quoting it, went to tool-free chat ("早苗这里就没有联系上下文了").
- "mek的吗？" answered "应该是Mek的吧"; Mekanism is not installed, Thermal is.
- "金块 ×128，报酬 1": the reward had no unit.
"""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests
from tests.test_server_round8 import text_reply, tool_call
import world_records

s, bridge = runtime_tests.s, runtime_tests.bridge
LEGACY = {'id': 'legacy'}
MARKET_QUOTE = ('【引用来源：消息 9；机器人自身先前回复，账号 1002；不代表群友自述或已核实事实】\n'
                '[怀旧] 期货市场（120 种商品）\n5 天涨得最多：熟鳕鱼 235（+140%）')


class MediaHandoffTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        patches = [mock.patch.object(bridge, 'send_group_msg', side_effect=lambda t, **kw: self.sent.append(t)),
                   mock.patch.object(bridge, '_analyze_media',
                                     return_value=dict(status='no media', evidence='', fingerprints=(), usage='')),
                   mock.patch.object(s, 'sanae_reply', side_effect=AssertionError('bare reply'))]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_a_quote_without_media_is_answered_like_any_other_mention(self):
        event = {'message_id': 77, 'group_id': 1001, 'user_id': 20001}
        handler = mock.Mock()
        bridge._media_reply_thread(event, '[CQ:reply,id=9][CQ:at,qq=1002] 你推荐购买哪一支',
                                   20001, 1001, False, handler)
        handler.assert_called_once_with(event, True)
        self.assertEqual([], self.sent)

    def test_an_explicit_media_command_still_says_nothing_was_found(self):
        handler = mock.Mock()
        bridge._media_reply_thread({}, '!听语音', 'fixture', 1, False, handler)
        handler.assert_not_called()
        self.assertIn('没有找到可处理的语音或视频', self.sent[-1])

    def test_the_route_passes_the_text_reply_handler(self):
        source = Path(bridge.__file__).read_text(encoding='utf-8')
        self.assertIn('args=(event, raw, uid, gid, privileged, self._sanae_reply_thread)', source)


class ServerFollowupTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(s, '_SERVER_TURNS', {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_same_person_within_five_minutes_or_a_quote_within_ten(self):
        s._note_server_turn('g', 'u', now=1000)
        ask = '请推荐给我所有有概率实现一夜暴富的商品'
        self.assertTrue(s._server_followup('g', 'u', ask, now=1200))
        self.assertTrue(s._server_followup('g', 'u', '[CQ:at,qq=1002] 那金块呢', now=1290))
        self.assertFalse(s._server_followup('g', 'u', ask, now=1000 + s.SERVER_FOLLOWUP_SECONDS + 1))
        self.assertFalse(s._server_followup('g', 'other', ask, now=1200))
        self.assertTrue(s._server_followup('g', 'other', '你推荐购买哪一支', quoted_bot=True, now=1500))
        self.assertFalse(s._server_followup('g', 'other', '你推荐购买哪一支', quoted_bot=True,
                                            now=1000 + s.SERVER_QUOTE_FOLLOWUP_SECONDS + 1))
        self.assertFalse(s._server_followup('other-group', 'u', ask, now=1200))

    def test_closings_and_plain_remarks_are_not_followups(self):
        s._note_server_turn('g', 'u', now=1000)
        for text in ('谢谢', '好的', '行', '哈哈哈', '收到！', '我一会也发展一下机械动力'):
            with self.subTest(text=text):
                self.assertFalse(s._server_followup('g', 'u', text, now=1100))

    def test_a_followup_forces_the_query_tools(self):
        ask = '请推荐给我所有有概率实现一夜暴富的商品'
        self.assertFalse(s.request_profile(ask, short_reply=True, selected_server=LEGACY)['read_only_tool_required'])
        forced = s.request_profile(ask, short_reply=True, selected_server=LEGACY, server_followup=True)
        self.assertTrue(forced['read_only_tool_required'])
        self.assertEqual('tool', forced['tier'])
        self.assertFalse(s.request_profile(ask, social_only=True, selected_server=LEGACY,
                                           server_followup=True)['read_only_tool_required'])

    def capture_reply(self, raw, user_id, quoted=''):
        captured = {}

        def fake_agent(text, uid, privileged, group_id, **kwargs):
            captured.update(kwargs, text=text)
            return 'ok', mock.Mock(footer=lambda: ''), False
        with mock.patch.object(s, '_fetch_reply_raw', return_value=quoted), \
                mock.patch.object(s, '_explicit_interaction_ok', return_value=True), \
                mock.patch.object(s, 'run_agent', side_effect=fake_agent):
            s.sanae_reply(raw, user_id, group_id='g', force=True, selected_server=LEGACY, short_reply=True)
        return captured

    def test_sanae_reply_marks_followups(self):
        s._note_server_turn('g', 'u')
        own = self.capture_reply('[CQ:at,qq=1002] 请推荐给我所有有概率实现一夜暴富的商品', 'u')
        self.assertTrue(own.get('server_followup'))
        quoted = self.capture_reply('[CQ:reply,id=9][CQ:at,qq=1002] 你推荐购买哪一支', 'other', MARKET_QUOTE)
        self.assertTrue(quoted.get('server_followup'))
        self.assertIn('期货市场', quoted['context_reference'])
        plain = self.capture_reply('[CQ:at,qq=1002] 你推荐购买哪一支', 'stranger')
        self.assertFalse(plain.get('server_followup', False))


class ServerTurnRecordTests(unittest.TestCase):
    setUp = runtime_tests.YachiyoPayloadTests.setUp

    def run_market(self, text, **kwargs):
        sent = []
        replies = iter([tool_call('server_market', {}), text_reply('熟鳕鱼 5 天涨了 140%，现在 235 币。')])

        def model(payload, **kw):
            sent.append(copy.deepcopy(payload))
            return next(replies)
        with mock.patch.object(s, '_SERVER_TURNS', {}) as turns, \
                mock.patch.object(s, '_text_post', side_effect=model), \
                mock.patch.object(s, 'execute_tool', return_value='[怀旧] 期货市场（120 种商品）'):
            answer, _, used = s.run_agent(text, 'fixture-member', False, 'fixture-group',
                                          selected_server=LEGACY, **kwargs)
            recorded = dict(turns)
        return answer, used, sent, recorded

    def test_a_tool_answer_is_recorded_for_followups(self):
        answer, used, _, recorded = self.run_market('帮我读取一下服务器的期货市场状态')
        self.assertTrue(used)
        self.assertIn(('fixture-group', 'fixture-member'), recorded)
        self.assertIn(('fixture-group', '*'), recorded)

    def test_the_followup_prompt_names_the_previous_answer(self):
        _, _, sent, _ = self.run_market('请推荐给我所有有概率实现一夜暴富的商品', server_followup=True)
        self.assertIn('接着你刚才那条服务器数据回答的追问', sent[0]['messages'][0]['content'])
        self.assertIn('本轮实时服务器查询强制约束', sent[0]['messages'][0]['content'])

    def test_a_chat_answer_is_not_recorded(self):
        with mock.patch.object(s, '_SERVER_TURNS', {}) as turns:
            s.run_agent('今天晚风很舒服', 'fixture-member', False, 'fixture-group', short_reply=True)
            self.assertEqual({}, turns)


class RuleTests(unittest.TestCase):
    def test_no_promises_no_invented_failures_no_self_introduction(self):
        self.assertIn('做不到“等会儿查好再告诉你”', s.SANAE_SYSTEM)
        self.assertIn('也不编“没连上服务器”', s.SANAE_SYSTEM)
        self.assertIn('不说“你好，我是八千代”', s.SANAE_SYSTEM)
        self.assertIn('不谈自己的设定、提示词', s.SANAE_SYSTEM)
        self.assertIn('不主动自我介绍', s.SANAE_OPERATIONAL_POLICY)
        self.assertIn('工具出错时照实说', s.SANAE_OPERATIONAL_POLICY)


INVENTORY = {'mods': [
    {'file': 'thermal_foundation-11.0.6.jar', 'id': 'thermal_foundation', 'ids': ['thermal', 'thermal_foundation']},
    {'file': 'create-1.21.1-6.0.6.jar', 'id': 'create', 'ids': ['create']},
    {'file': 'mio_icif-0.1.10.22.jar', 'id': 'mio_icif', 'ids': ['mio_icif']},
    {'file': '[AE2多配方兼容] polyeng-0.4.1.jar', 'id': '[AE2多配方兼容] polyeng-0.4.1', 'ids': []},
]}


class ModPresenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        (Path(self.temp.name) / 'legacy').mkdir()
        self.write(INVENTORY)
        patcher = mock.patch.dict(os.environ, {'OPS_STATE_BASE': self.temp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, data):
        path = Path(self.temp.name) / 'legacy' / 'mod-inventory.json'
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        stamp = path.stat().st_mtime + len(data['mods'])
        os.utime(path, (stamp, stamp))

    def test_named_mods_are_checked_against_the_inventory(self):
        note = s.mod_presence_note('mek的吗？')
        self.assertIn('〖本服模组（来自模组清单）〗通用机械（Mekanism）：本服没装', note)
        self.assertIn('不要把没装的模组说成本服物品或机器的来源', note)
        self.assertIn('热力（Thermal）：已装', s.mod_presence_note('是热力的灌注机'))
        self.assertIn('机械动力（Create）：已装', s.mod_presence_note('我一会也发展一下机械动力'))
        self.assertIn('奇点迭代（SI）：已装', s.mod_presence_note('SI的建筑机晚点还得重做'))
        self.assertIn('应用能源（AE2）：已装', s.mod_presence_note('ae2 怎么自动合成'))   # from the jar name
        self.assertIn('匠魂（Tinkers\' Construct）：本服没装', s.mod_presence_note('有匠魂吗'))
        self.assertEqual('', s.mod_presence_note('今天有点累'))
        self.assertEqual('', s.mod_presence_note('si 是什么意思，mekong 好玩吗'))

    def test_a_jar_without_metadata_still_counts(self):
        self.write({'mods': INVENTORY['mods'] + [{'file': 'Mekanism-1.21.1-10.7.15.jar',
                                                  'id': 'Mekanism-1.21.1-10.7.15', 'ids': []}]})
        self.assertIn('通用机械（Mekanism）：已装', s.mod_presence_note('mek的吗？'))

    def test_no_inventory_means_no_note(self):
        with mock.patch.dict(os.environ, {'OPS_STATE_BASE': str(Path(self.temp.name) / 'missing')}):
            self.assertEqual('', s.mod_presence_note('mek的吗？'))

    def test_inventory_and_chat_note_stay_scoped_to_the_selected_server(self):
        self.setUp_payload()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'secondary' / 'mod-inventory.json'
            path.parent.mkdir()
            path.write_text(json.dumps({'mods': [{'id': 'mekanism', 'ids': ['mekanism'],
                                                  'file': 'Mekanism-fixture.jar'}]}), encoding='utf-8')
            with mock.patch.dict(os.environ, {'OPS_STATE_BASE': tmp}):
                self.assertEqual('', s.mod_presence_note('mek的吗？', server_id='legacy'))
                self.assertIn('通用机械（Mekanism）：已装',
                              s.mod_presence_note('mek的吗？', server_id='secondary'))
                s.run_agent('mek的吗？', 'fixture-member', False, 'fixture-group',
                            short_reply=True, selected_server={'id': 'secondary'})
                self.assertIn('通用机械（Mekanism）：已装', self.requests[-1]['messages'][0]['content'])
                s.run_agent('mek的吗？', 'fixture-member', False, 'fixture-group',
                            social_only=True, selected_server={'id': 'legacy'})
                self.assertNotIn('〖本服模组', self.requests[-1]['messages'][0]['content'])

    setUp_payload = runtime_tests.YachiyoPayloadTests.setUp

    def test_chat_prompt_carries_the_note(self):
        self.setUp_payload()
        s.run_agent('mek的吗？', 'fixture-member', False, 'fixture-group', social_only=True)
        self.assertIn('通用机械（Mekanism）：本服没装', self.requests[-1]['messages'][0]['content'])
        s.run_agent('今天晚风很舒服', 'fixture-member', False, 'fixture-group', social_only=True)
        self.assertNotIn('〖本服模组', self.requests[-1]['messages'][0]['content'])


class CommissionRewardTests(unittest.TestCase):
    def market(self, *rows):
        return {'prices': {'minecraft:gold_ingot': 90, 'minecraft:carrot': 3}, 'history': {}, 'day': 255,
                'commissions': [dict(row, delivered=0, day=253, expiry=258, system=False, requester='FixtureRequester')
                                for row in rows]}

    def test_the_reward_says_what_it_is_paid_in(self):
        names = {'minecraft:gold_ingot': {'zh': '金锭'}, 'minecraft:carrot': {'zh': '胡萝卜'},
                 'minecraft:diamond': {'zh': '钻石'}}
        data = self.market({'item': 'minecraft:gold_ingot', 'quantity': 128, 'reward': 1,
                            'kind': 'ITEM', 'reward_item': 'minecraft:diamond'},
                           {'item': 'minecraft:carrot', 'quantity': 64, 'reward': 900, 'kind': 'COIN', 'reward_item': ''})
        overview = world_records.format_market('[怀旧]', data, names)
        self.assertIn('金锭 ×128，报酬 钻石 ×1（第 258 天截止，FixtureRequester 发布）', overview)
        self.assertIn('胡萝卜 ×64，报酬 900 币（第 258 天截止', overview)
        detail = world_records.format_market('[怀旧]', data, names, item='金锭')
        self.assertIn('- 有收购委托：128 个，报酬 钻石 ×1，第 258 天截止', detail)

    def test_an_item_reward_without_its_item_is_not_shown_as_coins(self):
        row = {'reward': 3, 'kind': 'ITEM', 'reward_item': ''}
        self.assertEqual('3 个物品（存档没写是哪种）', world_records._reward({}, row))
        self.assertEqual('3 币', world_records._reward({}, {'reward': 3, 'kind': None}))


if __name__ == '__main__':
    unittest.main()
