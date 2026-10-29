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
MARKET_ANSWER = ('[怀旧] 期货市场（120 种商品）\n5 天涨得最多：**熟鳕鱼** 235（+140%）、红石粉 55（+28%）\n'
                 '5 天跌得最多：蜂蜜瓶 6（-96%）')
# QQ shows the sent text without the Markdown marks.
MARKET_QUOTE = ('【引用来源：消息 9；机器人自身先前回复，账号 1005；不代表群友自述或已核实事实】\n'
                '[怀旧] 期货市场（120 种商品）\n5 天涨得最多：熟鳕鱼 235（+140%）、红石粉 55（+28%）')


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
        bridge._media_reply_thread(event, '[CQ:reply,id=9][CQ:at,qq=1005] 你推荐购买哪一支',
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
        for name in ('_SERVER_TURNS', '_SERVER_ANSWERS'):
            patcher = mock.patch.object(s, name, {})
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_same_person_within_five_minutes(self):
        s._note_server_turn('g', 'u', MARKET_ANSWER, now=1000)
        ask = '请推荐给我所有有概率实现一夜暴富的商品'
        self.assertTrue(s._server_followup('g', 'u', ask, now=1200))
        self.assertTrue(s._server_followup('g', 'u', '[CQ:at,qq=1005] 那金块呢', now=1290))
        self.assertFalse(s._server_followup('g', 'u', ask, now=1000 + s.SERVER_FOLLOWUP_SECONDS + 1))
        self.assertFalse(s._server_followup('g', 'other', ask, now=1200))
        self.assertFalse(s._server_followup('other-group', 'u', ask, now=1200))

    def test_anyone_quoting_that_answer_within_ten_minutes(self):
        s._note_server_turn('g', 'u', MARKET_ANSWER, now=1000)
        self.assertTrue(s._server_followup('g', 'other', '你推荐购买哪一支', quoted=MARKET_QUOTE, now=1500))
        self.assertFalse(s._server_followup('g', 'other', '你推荐购买哪一支', quoted=MARKET_QUOTE,
                                            now=1000 + s.SERVER_QUOTE_FOLLOWUP_SECONDS + 1))
        # Quoting one of her chat lines, or a member's message, is not a follow-up.
        chat = ('【引用来源：消息 8；机器人自身先前回复，账号 1005；不代表群友自述或已核实事实】\n'
                '刚才光顾着想柚子，把你们前面聊的虚空市场和做空给漏掉了')
        member = '【引用来源：消息 7；原发言者用户 20001；按该发言者归属，不代表当前用户已确认】\n[怀旧] 期货市场（120 种商品）'
        self.assertFalse(s._server_followup('g', 'other', '为什么', quoted=chat, now=1100))
        self.assertFalse(s._server_followup('g', 'other', '推荐哪个', quoted=member, now=1100))

    def test_closings_and_plain_chat_are_not_followups(self):
        s._note_server_turn('g', 'u', MARKET_ANSWER, now=1000)
        for text in ('谢谢', '好的', '行', '哈哈哈', '收到！', '我一会也发展一下机械动力',
                     '早苗你吃饭了吗？', '晚上好'):
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
        s._note_server_turn('g', 'u', MARKET_ANSWER)
        own = self.capture_reply('[CQ:at,qq=1005] 请推荐给我所有有概率实现一夜暴富的商品', 'u')
        self.assertTrue(own.get('server_followup'))
        quoted = self.capture_reply('[CQ:reply,id=9][CQ:at,qq=1005] 你推荐购买哪一支', 'other', MARKET_QUOTE)
        self.assertTrue(quoted.get('server_followup'))
        self.assertIn('期货市场', quoted['context_reference'])
        plain = self.capture_reply('[CQ:at,qq=1005] 你推荐购买哪一支', 'stranger')
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
                mock.patch.object(s, '_SERVER_ANSWERS', {}) as answers, \
                mock.patch.object(s, '_text_post', side_effect=model), \
                mock.patch.object(s, 'execute_tool', return_value='[怀旧] 期货市场（120 种商品）'):
            answer, _, used = s.run_agent(text, 'fixture-member', False, 'fixture-group',
                                          selected_server=LEGACY, **kwargs)
            recorded = dict(turns), dict(answers)
        return answer, used, sent, recorded

    def test_a_tool_answer_is_recorded_for_followups(self):
        answer, used, _, (turns, answers) = self.run_market('帮我读取一下服务器的期货市场状态')
        self.assertTrue(used)
        self.assertIn(('fixture-group', 'fixture-member'), turns)
        self.assertIn('熟鳕鱼5天涨了140%', answers['fixture-group'][0][1])

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
        self.assertIn('本服的物品和机器只会来自已装的模组', note)
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

    def write_items(self):
        names = {'thermal:machine_insolator': {'zh': '有机灌注器', 'en': 'Phytogenic Insolator'},
                 'thermal:charge_bench': {'zh': '能量灌注机', 'en': 'Energetic Infuser'},
                 'botania:runic_altar': {'zh': '符文祭坛', 'en': 'Runic Altar'},
                 'create:andesite_alloy': {'zh': '安山合金', 'en': 'Andesite Alloy'}}
        (Path(self.temp.name) / 'legacy' / 'recipe-index.json').write_text(
            json.dumps({'names': names}, ensure_ascii=False), encoding='utf-8')

    def test_a_machine_asked_about_is_looked_up_in_the_item_names(self):
        self.write_items()
        context = '[1分钟前 群友原话] 　　　　　　：树场用灌注机可能会更好没有渲染更流畅\n'
        note = s.item_source_note('mek的吗？', context)
        self.assertIn('〖本服物品表（来自本服配方索引）〗名字里有“灌注”的：', note)
        self.assertIn('有机灌注器（thermal）', note)
        self.assertIn('能量灌注机（thermal）', note)
        self.assertIn('安山合金', s.item_source_note('安山合金机是哪个模组的'))
        # Only when the message asks where something comes from.
        self.assertEqual('', s.item_source_note('树场用灌注机可能会更好'))
        self.assertEqual('', s.item_source_note('今天服务器好卡是什么的问题吗'))

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



class GeminiReplayTests(unittest.TestCase):
    def test_empty_text_parts_are_not_replayed(self):
        # 10-26 22:49: replaying {"text": ""} after a function call made the relay answer
        # "500 Upstream gateway error" on every call after a tool result.
        parts = [{'functionCall': {'name': 'server_market', 'args': {}, 'id': 'call_1'}, 'thoughtSignature': 'sig'},
                 {'text': ''}, {'text': '  ', 'thought': True}, {'text': '', 'thoughtSignature': 'sig-2'}]
        messages = [{'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': '期货市场'},
                    {'role': 'assistant', 'content': '', '_gemini_parts': parts,
                     'tool_calls': [{'id': 'call_1', 'type': 'function',
                                     'function': {'name': 'server_market', 'arguments': '{}'}}]},
                    {'role': 'tool', 'tool_call_id': 'call_1', 'name': 'server_market', 'content': '[怀旧] 期货市场'}]
        _, contents = s._gemini_contents(messages)
        model = contents[1]['parts']
        self.assertEqual([parts[0], parts[3]], model)
        self.assertEqual('call_1', contents[2]['parts'][0]['functionResponse']['id'])



def calls(name, count, **arguments):
    return {'choices': [{'message': {'content': '', 'tool_calls': [
        {'id': f'call-{i}', 'type': 'function',
         'function': {'name': name, 'arguments': json.dumps(dict(arguments, item=f'item-{i}'))}}
        for i in range(count)]}}]}


class ToolCapTests(unittest.TestCase):
    setUp = runtime_tests.YachiyoPayloadTests.setUp

    def run_cap(self, name, count):
        sent = []
        replies = iter([calls(name, count), text_reply('丛林原木 5 天涨了 400%，可以留意一下')])

        def model(payload, **kw):
            sent.append(copy.deepcopy(payload))
            return next(replies)
        with mock.patch.object(s, '_SERVER_TURNS', {}), mock.patch.object(s, '_SERVER_ANSWERS', {}), \
                mock.patch.object(s, '_text_post', side_effect=model), \
                mock.patch.object(s, 'execute_tool', return_value='[怀旧] 丛林原木 现价 10') as execute:
            answer, _, used = s.run_agent('请推荐给我所有有概率实现一夜暴富的商品', 'fixture-member', False,
                                          'fixture-group', selected_server=LEGACY, server_followup=True)
        return answer, sent, execute

    def test_read_only_results_are_summarised_at_the_cap(self):
        # 10-26 replay: the follow-up looked up the market item by item and ended with
        # "本次已达到工具调用上限".
        answer, sent, execute = self.run_cap('server_market', 13)
        self.assertEqual('丛林原木 5 天涨了 400%，可以留意一下', answer)
        self.assertEqual(12, execute.call_count)
        final = sent[-1]
        self.assertEqual('none', final['tool_choice'])
        tool_rows = [m for m in final['messages'] if m.get('role') == 'tool']
        self.assertEqual(13, len(tool_rows))
        self.assertIn('本轮查询次数已到上限', tool_rows[-1]['content'])

    def test_other_tools_still_stop_at_the_cap(self):
        answer, sent, execute = self.run_cap('run_rcon', 13)
        self.assertIn('本次已达到工具调用上限', answer)
        self.assertEqual(1, len(sent))

    def test_answer_rules(self):
        self.assertIn('不拿“我是人工智能/AI”开头或当免责声明', s.SANAE_OPERATIONAL_POLICY)
        self.assertIn('本服期货市场是游戏玩法，不是现实投资', s.SANAE_OPERATIONAL_POLICY)


if __name__ == '__main__':
    unittest.main()
