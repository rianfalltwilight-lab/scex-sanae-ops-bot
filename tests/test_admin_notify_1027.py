"""Round 19: in-game time questions, follow-ups by name, and forwarding a member's request to the
admin group. Offline.

Main group 10-27 00:12-00:25 (machine time):
- owner "早苗，服务器目前什么时间？" / "早苗，主世界目前什么时间" went to tool-free chat; she said she
  could not read the in-game time and told him to log in and look.
- returnling asked where the item and XP piles were; she rightly kept the coordinates for admins.
  Two minutes later "早苗，把上述本次分析坐标及距离该坐标最近的玩家id记录丢给管理员" went to chat too,
  which said "上面没有分析过坐标呀……也没发给管理员呢" ("隔一句话就忘").
"""
import copy
import unittest
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests
from tests.test_server_round8 import REAL_EXECUTE_TOOL, text_reply, tool_call

s, bridge = runtime_tests.s, runtime_tests.bridge
LEGACY = {'id': 'legacy', 'name': 'Legacy', 'prefix': '[怀旧]'}
ASK = '早苗，把上述本次分析坐标及距离该坐标最近的玩家id记录丢给管理员。'
ENTITY_ANSWER = '目前全服经验球约有 1282 个，掉落物约 375 个，具体坐标只对管理员可见'
RCON = {
    'neoforge entity list "minecraft:item"': 'Entity: minecraft:item Total: 336\n  33: -941, 807\n  16: -924, 1582\n',
    'neoforge entity list "minecraft:experience_orb"': 'Entity: minecraft:experience_orb Total: 784\n  350: -938, 800\n  228: -938, 802\n',
    'list': 'There are 2 of a max of 20 players online: Frozen9, Litxue',
    'data get entity Frozen9 Dimension': 'Frozen9 has the following entity data: "minecraft:overworld"',
    'data get entity Frozen9 Pos': 'Frozen9 has the following entity data: [-15000.5d, 70.0d, 12810.2d]',
    'data get entity Litxue Dimension': 'Litxue has the following entity data: "minecraft:the_nether"',
    'data get entity Litxue Pos': 'Litxue has the following entity data: [10.0d, 70.0d, 10.0d]',
}


class IntentTests(unittest.TestCase):
    def test_in_game_time_questions(self):
        for text in ('早苗，服务器目前什么时间？', '早苗，主世界目前什么时间', '服务器现在时间多少'):
            with self.subTest(text=text):
                self.assertTrue(s._has_server_query_intent(text))
        for text in ('你什么时间有空', '明天什么时间开服', '现在什么时间了'):
            with self.subTest(text=text):
                self.assertFalse(s._has_server_query_intent(text))

    def test_asks_to_notify_admins(self):
        for text in (ASK, '帮我告诉管理员一下服务器卡了', '这个反馈给群主', '让管理员看看这个'):
            with self.subTest(text=text):
                self.assertTrue(s._asks_to_notify_admins(text))
        for text in ('管理员在吗', '你是管理员吗', '群主今天值班吗'):
            with self.subTest(text=text):
                self.assertFalse(s._asks_to_notify_admins(text))

    def test_a_follow_up_by_name_goes_to_the_tools(self):
        with mock.patch.object(s, '_SERVER_TURNS', {}), mock.patch.object(s, '_SERVER_ANSWERS', {}):
            s._note_server_turn('g', 'returnling', ENTITY_ANSWER)
            self.assertTrue(s._server_followup('g', 'returnling', '早苗，那这些呢'))
            self.assertTrue(bridge._natural_server_operation_candidate(
                '早苗，那这些呢', False, direct_call=True, group_id='g', user_id='returnling'))
            self.assertFalse(bridge._natural_server_operation_candidate(
                '早苗，那这些呢', False, direct_call=True, group_id='g', user_id='someone-else'))
        self.assertTrue(bridge._natural_server_operation_candidate(ASK, False, direct_call=True))
        self.assertTrue(bridge._is_ai_operation_turn(ASK, False, 'returnling'))

    def test_profile_forces_the_forwarding_tool(self):
        profile = s.request_profile(ASK, short_reply=True, selected_server=LEGACY)
        self.assertTrue(profile['notify_request'])
        self.assertFalse(profile['chat_mode'])


class NotifyToolTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        for patcher in (mock.patch.object(s, 'ADMIN_NOTIFY_FN', side_effect=lambda t: self.sent.append(t) or True),
                        mock.patch.object(s, '_NOTIFY_LOG', [])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def notify(self, args, text=ASK, user='2681490724', now=None):
        with mock.patch.object(s, 'query_server', side_effect=lambda server, command: RCON.get(command, '')):
            return s._notify_admins(args, text, user, 'returnling', 1001, LEGACY, now=now)

    def test_the_admins_get_the_coordinates_and_the_member_does_not(self):
        result = self.notify({'summary': '经验球和掉落物堆积，可能拖慢主世界', 'attach': 'entities'})
        self.assertEqual('已经转给管理组了。附上的坐标和玩家位置只发给了管理员，这里不显示。', result)
        self.assertNotIn('-938', result)
        message = self.sent[0]
        self.assertTrue(message.startswith('[转给管理员] 来自 returnling（2681490724），群 1001'))
        self.assertIn('原话：早苗，把上述本次分析坐标', message)
        self.assertIn('八千代：经验球和掉落物堆积', message)
        self.assertIn('经验球共 784 个，最集中的区块：(-938, 800) 350 个', message)
        self.assertIn('最大一堆：经验球 350 个，区块 (-938, 800)，方块坐标约 x=-15000 z=12808（主世界）', message)
        self.assertIn('离它最近的在线玩家：Frozen9', message)
        self.assertNotIn('Litxue', message)              # in the Nether

    def test_limits(self):
        self.assertTrue(self.notify({'summary': 'a'}, now=1000).startswith('已经转给'))
        self.assertIn('半小时内已经转给', self.notify({'summary': 'a'}, now=1100))
        self.notify({'summary': 'b'}, text='帮我告诉管理员 b', now=1200)
        self.notify({'summary': 'c'}, text='帮我告诉管理员 c', now=1300)
        self.assertIn('这一小时已经让我转了 3 次', self.notify({'summary': 'd'}, text='帮我告诉管理员 d', now=1400))
        self.assertTrue(self.notify({'summary': 'd'}, text='帮我告诉管理员 d', now=1000 + 3601).startswith('已经转给'))
        self.assertEqual(4, len(self.sent))

    def test_refusals(self):
        self.assertIn('没有要求转给管理员', self.notify({'summary': 'x'}, text='经验球好多'))
        self.assertIn('summary 为空', self.notify({'summary': ''}))
        with mock.patch.object(s, 'ADMIN_NOTIFY_FN', None):
            self.assertIn('没有配置', self.notify({'summary': 'x'}))
        with mock.patch.object(s, 'ADMIN_NOTIFY_FN', return_value=False):
            self.assertIn('发送到管理组失败', self.notify({'summary': 'x'}))
        self.assertTrue(self.notify({'summary': 'x'}).startswith('已经转给'))   # a failed send is not counted
        self.assertEqual([], [m for m in self.sent if 'x' not in m])

    def test_execute_tool_routes_it_for_members(self):
        with mock.patch.object(s, '_notify_admins', return_value='已经转给管理组了。') as notify:
            result = REAL_EXECUTE_TOOL('notify_admins', {'summary': 'x'}, False, current_user_text=ASK,
                                       user_id='2681490724', nickname='returnling', group_id=1001,
                                       selected_server=LEGACY)
        self.assertEqual('已经转给管理组了。', result)
        self.assertEqual(ASK, notify.call_args.args[1])
        self.assertIn(s.NOTIFY_ADMINS, s.TOOLS_MEMBER)
        self.assertIn(s.NOTIFY_ADMINS, s.TOOLS_ADMIN)

    def test_bridge_sends_to_the_admin_groups(self):
        groups = []
        with mock.patch.object(bridge, 'BUG_DIGEST_GROUPS', {2105}), \
                mock.patch.object(bridge, 'send_group_msg', side_effect=lambda t, **kw: groups.append(bridge.REPLY_GROUP.get()) or True):
            self.assertTrue(bridge._notify_admin_groups('hello'))
        self.assertEqual([2105], groups)


class AgentTests(unittest.TestCase):
    setUp = runtime_tests.YachiyoPayloadTests.setUp

    def run_turn(self, responses, tool_result=None):
        sent = []
        replies = iter(responses)

        def model(payload, **kwargs):
            sent.append(copy.deepcopy(payload))
            return next(replies)
        execute = (mock.patch.object(s, 'execute_tool', return_value=tool_result) if tool_result is not None
                   else mock.patch.object(s, 'execute_tool', side_effect=AssertionError('no tool expected')))
        with mock.patch.object(s, '_text_post', side_effect=model), execute as tool:
            answer, _, _ = s.run_agent(ASK, '2681490724', False, 'fixture-group', selected_server=LEGACY,
                                       short_reply=True)
        return answer, sent, tool

    def test_forwarding_runs_the_tool(self):
        answer, sent, tool = self.run_turn(
            [tool_call('notify_admins', {'summary': '经验球堆积', 'attach': 'entities'}), text_reply('已经转给管理员了，坐标只发给他们')],
            tool_result='已经转给管理组了。附上的坐标和玩家位置只发给了管理员，这里不显示。')
        self.assertEqual('已经转给管理员了，坐标只发给他们', answer)
        self.assertEqual('required', sent[0]['tool_choice'])
        self.assertIn('notify_admins', [t['function']['name'] for t in sent[0]['tools']])
        self.assertEqual('notify_admins', tool.call_args.args[0])

    def test_saying_it_was_forwarded_without_the_tool_is_not_sent(self):
        answer, _, _ = self.run_turn(
            [tool_call('server_world', {'topic': 'entities'}), text_reply('好的，已经帮你转给管理员了')],
            tool_result='[怀旧] 全服实体（实时）共 3000 个')
        self.assertTrue(answer.startswith('这次没有转给管理员'))

    def test_social_mode_does_not_claim_it_cannot_read_the_server(self):
        from pathlib import Path
        self.assertIn('不要说读不到、看不到或让对方自己上线看', Path(s.__file__).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
