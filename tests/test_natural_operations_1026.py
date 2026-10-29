"""Round 18: admins operate the server in plain words; members get a clear no. Offline.

10-26 22:19-22:21 in the main group, owner FixtureAdmin:
- "现在服务器是什么天气？" returned the whole /weather and /gamerule catalog.
- "你直接告诉我是什么天气" got "没法直接告诉你" (1.21 has no weather query command).
- "把服务器调成白天": "调成" was not a recognised verb, so `time set day` was refused; she then
  wrote "请回复确认" although no confirmation existed, and his "确认" did nothing.
- request_console_command could never run in that group: with the group default resolved
  first, the only enabled server was not treated as unambiguous.
"""
import copy
import unittest
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests
from tests.test_server_round8 import REAL_EXECUTE_TOOL, text_reply, tool_call
import command_catalog
import rcon_ops

s, bridge = runtime_tests.s, runtime_tests.bridge
LEGACY = {'id': 'legacy', 'name': 'Legacy', 'prefix': '[怀旧]'}


class IntentTests(unittest.TestCase):
    def test_operation_requests(self):
        for text in ('把服务器调成白天', '[CQ:at,qq=1002] 把服务器调成白天', '能不能把天气调成晴天',
                     '能把时间调到晚上吗？', '给 Alice 64 个钻石', '帮我把在线的 Alice 踢了', '帮我清理一下掉落物',
                     '来场雨', 'time set day', '/weather clear', '把EMT123321的OP下了', '重启一下服务器',
                     '帮我在游戏里发个公告：今晚十点维护'):
            with self.subTest(text=text):
                self.assertTrue(s._has_operation_request(text))

    def test_questions_and_chat_are_not_operations(self):
        for text in ('现在服务器是什么天气？', '你直接告诉我是什么天气', '给我讲个笑话', '给你看个图片', '现在几点了',
                     '早苗服务器里现在是白天还是晚上', '外面下雨了', '确认', '帮我查一下任务进度', '今天天气真好',
                     '早苗你吃饭了吗？', 'time set day 是什么意思', '怎么把时间调成白天'):
            with self.subTest(text=text):
                self.assertFalse(s._has_operation_request(text))

    def test_low_risk_change_verbs(self):
        for text in ('把服务器调成白天', '时间调到晚上', '天气换成晴天', '能不能把天气调成雨天', '来场雷雨'):
            with self.subTest(text=text):
                self.assertTrue(s._has_mutation_intent(text))
        for text in ('现在是白天吗？', '为什么调成白天了', '雨已经下了'):
            with self.subTest(text=text):
                self.assertFalse(s._has_mutation_intent(text))

    def test_in_game_weather_and_time_questions_are_queries(self):
        for text in ('现在服务器是什么天气？', '服里下雨了吗', '早苗服务器里现在是白天还是晚上', '游戏里几点了'):
            with self.subTest(text=text):
                self.assertTrue(s._has_server_query_intent(text))
        for text in ('今天天气真好', '外面下雨了吗', '现在几点了'):
            with self.subTest(text=text):
                self.assertFalse(s._has_server_query_intent(text))

    def test_confirmation_words(self):
        self.assertTrue(s._has_confirmation_intent('确定'))
        self.assertFalse(s._has_confirmation_intent('你确定吗？'))
        for text in ('确认', '确认。', '好的，确认', '执行吧', '取消', '算了', '[CQ:at,qq=1002] 确认'):
            with self.subTest(text=text):
                self.assertTrue(bridge._short_operation_reply(text))
        for text in ('确认一下明天几点开会', '我确认过了他没上线', '算了不聊了，去睡觉'):
            with self.subTest(text=text):
                self.assertFalse(bridge._short_operation_reply(text))


class TargetAndProfileTests(unittest.TestCase):
    def test_a_lone_server_is_unambiguous_even_through_the_group_default(self):
        with mock.patch.object(bridge, 'get_selected', return_value=(None, '未设置')), \
                mock.patch.object(bridge, 'get_group_default', return_value=(LEGACY, '')), \
                mock.patch.object(bridge, 'list_servers', return_value=[LEGACY]):
            target, _, unambiguous = bridge._ai_operation_target('把服务器调成白天', 1001, 'u')
        self.assertEqual('legacy', target['id'])
        self.assertTrue(unambiguous)
        with mock.patch.object(bridge, 'get_selected', return_value=(None, '未设置')), \
                mock.patch.object(bridge, 'get_group_default', return_value=(LEGACY, '')), \
                mock.patch.object(bridge, 'list_servers', return_value=[LEGACY, {'id': 'two'}]):
            _, _, unambiguous = bridge._ai_operation_target('把服务器调成白天', 1001, 'u')
        self.assertFalse(unambiguous)

    def profile(self, text, privileged=True, pending=None):
        with mock.patch.object(s._rcon_ops, 'pending_confirmation', return_value=pending):
            return s.request_profile(text, short_reply=True, selected_server=LEGACY, privileged=privileged,
                                     explicit_server=True, user_id='owner')

    def test_operation_profile(self):
        self.assertTrue(self.profile('把服务器调成白天')['operation_tool_required'])
        kick = self.profile('帮我把在线的 Alice 踢了')
        self.assertTrue(kick['operation_tool_required'])
        self.assertFalse(kick['read_only_tool_required'])
        self.assertFalse(self.profile('给我讲个笑话')['operation_tool_required'])
        self.assertFalse(self.profile('把服务器调成白天', privileged=False)['operation_tool_required'])
        # "确认/算了" only while this admin has a command waiting.
        self.assertFalse(self.profile('算了，不聊了')['operation_tool_required'])
        self.assertTrue(self.profile('确认', pending={'code': 'ABCD'})['operation_tool_required'])
        weather = self.profile('现在服务器是什么天气？')
        self.assertTrue(weather['read_only_tool_required'])
        self.assertFalse(weather['operation_tool_required'])

    def test_bridge_operation_turns(self):
        with mock.patch.object(bridge._rcon_ops, 'pending_confirmation', return_value=None):
            self.assertTrue(bridge._is_ai_operation_turn('把服务器调成白天', True, 'owner'))
            self.assertFalse(bridge._is_ai_operation_turn('给我讲个笑话', True, 'owner'))
            self.assertFalse(bridge._is_ai_operation_turn('算了', True, 'owner'))
            self.assertFalse(bridge._natural_server_operation_candidate('给你看个图片', True, at_call=True))
            self.assertTrue(bridge._natural_server_operation_candidate('把服务器调成白天', True, at_call=True))
        with mock.patch.object(bridge._rcon_ops, 'pending_confirmation', return_value={'code': 'ABCD'}):
            self.assertTrue(bridge._is_ai_operation_turn('算了', True, 'owner'))


class WeatherTests(unittest.TestCase):
    def fake(self, raining, thundering):
        def run(command):
            if 'thundering' in command:
                return 'Test passed\n' if thundering else 'Test failed\n'
            return 'Test passed\n' if raining else 'Test failed\n'
        return run

    def test_weather_status_from_predicates(self):
        self.assertEqual('[天气] 主世界现在是晴天（实时查询）', rcon_ops.weather_status(self.fake(False, False)))
        self.assertEqual('[天气] 主世界现在在下雨（雪地群系是下雪）（实时查询）', rcon_ops.weather_status(self.fake(True, False)))
        self.assertEqual('[天气] 主世界现在是雷雨（实时查询）', rcon_ops.weather_status(self.fake(True, True)))
        self.assertEqual('read', command_catalog.classify('weather query'))

    def test_run_rcon_weather_query_for_everyone(self):
        calls = []

        def query(server, command):
            calls.append(command)
            return 'Test failed\n' if 'thundering' in command else 'Test passed\n'
        with mock.patch.object(s, 'query_server', side_effect=query):
            member = REAL_EXECUTE_TOOL('run_rcon', {'command': 'weather query'}, False, selected_server=LEGACY)
            admin = REAL_EXECUTE_TOOL('run_rcon', {'command': 'weather query'}, True, selected_server=LEGACY)
        self.assertEqual('[天气] 主世界现在在下雨（雪地群系是下雪）（实时查询）', member)
        self.assertEqual(member, admin)
        self.assertTrue(all(c.startswith('execute if predicate {condition:"minecraft:weather_check"') for c in calls))

    def test_cmd_weather_query_is_answered_not_sent(self):
        sent = []
        result = rcon_ops.run_console_direct('weather query', 'admin',
                                             query_fn=lambda c: sent.append(c) or 'Test failed\n', risk='read')
        self.assertIn('主世界现在是晴天', result)
        self.assertNotIn('weather query', sent)


class OperationToolTests(unittest.TestCase):
    def test_low_risk_change_runs_and_is_audited(self):
        with mock.patch.object(s, 'query_server', return_value='Set the time to 1000') as query, \
                mock.patch.object(s._rcon_ops, 'audit_ai_command') as audit:
            out = REAL_EXECUTE_TOOL('run_rcon', {'command': 'time set day'}, True, allow_mutation=True,
                                    selected_server=LEGACY, user_id='owner')
            refused = REAL_EXECUTE_TOOL('run_rcon', {'command': 'time set day'}, False, allow_mutation=True,
                                        selected_server=LEGACY, user_id='member')
        self.assertEqual('Set the time to 1000', out)
        query.assert_called_once_with(LEGACY, 'time set day')
        audit.assert_called_once_with('owner', 'time set day', 'Set the time to 1000')
        self.assertIn('需要管理员', refused)

    def test_stop_and_restart_use_their_own_confirmation(self):
        # Owner decision 10-28: the same confirmation as `!restart` / `!stop`, not the !cmd one.
        for command in ('stop', 'restart'):
            with self.subTest(command=command), mock.patch.object(
                    s._rcon_ops, 'dispatch', return_value='[高危确认] 即将重启服务器。\n请在 90 秒内发送：!确认 ABCD') as dispatch:
                result = REAL_EXECUTE_TOOL('request_console_command', {'command': command}, True,
                                           current_user_text='重启一下服务器', selected_server=LEGACY,
                                           user_id='owner', explicit_server=True)
                self.assertIn('回复八千代：确认', result)
                self.assertEqual(command, dispatch.call_args.args[0])

    def test_natural_request_says_a_plain_reply_is_enough(self):
        text = '[高危确认] 即将执行 RCON 命令：give Alice minecraft:diamond 64。\n请在 90 秒内发送：!确认 ABCD\n如需取消：!取消确认'
        with mock.patch.object(s._command_catalog, 'validate', return_value=(True, '/give <targets> <item>', '')), \
                mock.patch.object(s._rcon_ops, 'dispatch', return_value=text):
            result = REAL_EXECUTE_TOOL('request_console_command', {'command': 'give Alice minecraft:diamond 64'}, True,
                                       current_user_text='给 Alice 64 个钻石', selected_server=LEGACY,
                                       user_id='owner', explicit_server=True)
        self.assertIn('回复八千代：确认 ABCD', result)
        self.assertIn('直接回复“确认”或“取消”就行', result)


class AgentTests(unittest.TestCase):
    setUp = runtime_tests.YachiyoPayloadTests.setUp

    def run_turn(self, text, responses, privileged=True, tool_result=None, pending=None):
        sent = []
        replies = iter(responses)

        def model(payload, **kwargs):
            sent.append(copy.deepcopy(payload))
            return next(replies)
        execute = (mock.patch.object(s, 'execute_tool', return_value=tool_result) if tool_result is not None
                   else mock.patch.object(s, 'execute_tool', side_effect=AssertionError('no tool expected')))
        with mock.patch.object(s, '_text_post', side_effect=model), execute as tool, \
                mock.patch.object(s._rcon_ops, 'pending_confirmation', return_value=pending):
            answer, usage, used = s.run_agent(text, 'owner' if privileged else 'member', privileged, 'fixture-group',
                                              selected_server=LEGACY, explicit_server=True)
        return answer, sent, tool

    def test_daytime_runs_once_and_is_reported_in_words(self):
        answer, sent, tool = self.run_turn(
            '把服务器调成白天',
            [tool_call('run_rcon', {'command': 'time set day'}), text_reply('调好了，现在是白天')],
            tool_result='Set the time to 1000')
        self.assertEqual('调好了，现在是白天', answer)
        self.assertEqual('required', sent[0]['tool_choice'])
        self.assertNotIn('tool_choice', sent[1])
        self.assertIn('【本轮管理员操作】', sent[0]['messages'][0]['content'])
        self.assertEqual(('run_rcon', {'command': 'time set day'}), (tool.call_args.args[0], tool.call_args.args[1]))
        self.assertTrue(tool.call_args.kwargs['allow_mutation'])

    def test_a_made_up_confirmation_step_is_not_sent(self):
        # 22:20:55: "按规定需要确认一下。您确定要将服务器时间设置为白天吗？如果确认，请回复“确认”"
        fake = text_reply('按规定需要确认一下。您确定要将服务器时间设置为白天吗？如果确认，请回复“确认”')
        answer, sent, _ = self.run_turn(
            '把服务器调成白天', [tool_call('run_rcon', {'command': 'time set day'}), fake, fake],
            tool_result='当前这条用户消息没有明确要求修改服务器状态，已拒绝执行写入型 RCON 命令。')
        self.assertTrue(answer.startswith('刚才没有真的发起确认'))
        self.assertEqual(3, len(sent))                     # one correction pass, then the guard
        self.assertIn('实际上都没有发生', sent[2]['messages'][-1]['content'])

    def test_a_claimed_change_without_a_tool_is_not_sent(self):
        claim = text_reply('已经帮你把时间调成白天了')
        answer, _, _ = self.run_turn(
            '把服务器调成白天', [tool_call('run_rcon', {'command': 'time set day'}), claim, claim],
            tool_result='该命令需要确认码，不能直接执行：请改用 request_console_command 发起确认。')
        self.assertTrue(answer.startswith('这条操作没有执行'))

    def test_a_written_out_confirmation_gets_one_more_try_with_the_tool(self):
        # Replay of "帮我清理一下掉落物": she typed "[高危确认] ... 请确认是否执行" herself.
        pending = '[高危确认] 即将执行 RCON 命令：kill @e[type=item]。\n请在 90 秒内回复八千代：确认 ABCD'
        answer, sent, tool = self.run_turn(
            '帮我清理一下掉落物',
            [tool_call('search_server_commands', {'query': 'kill'}),
             text_reply('[高危确认] 即将执行 RCON 命令：kill @e[type=item]\n请确认是否执行。'),
             tool_call('request_console_command', {'command': 'kill @e[type=item]'})],
            tool_result=pending)
        self.assertEqual(pending, answer)
        self.assertEqual('request_console_command', tool.call_args.args[0])
        self.assertEqual(3, len(sent))

    def test_members_get_a_plain_no(self):
        answer, sent, _ = self.run_turn('把服务器调成白天', [text_reply('好的，已经给你调成白天啦')], privileged=False)
        self.assertEqual('这个我做不了哦，改服务器的事得管理员来', answer)
        self.assertIn('但对方不是管理员', sent[0]['messages'][0]['content'])
        honest, _, _ = self.run_turn('把服务器调成白天', [text_reply('这个得找管理员哦，我只听管理员的')],
                                     privileged=False)
        self.assertEqual('这个得找管理员哦，我只听管理员的', honest)

    def test_weather_question_gets_an_answer_not_the_catalog(self):
        answer, _, _ = self.run_turn(
            '现在服务器是什么天气？',
            [tool_call('search_server_commands', {'query': 'weather'}), text_reply('现在是晴天')],
            tool_result='本服已注册命令匹配：\n/weather (clear|rain|thunder)\n/gamerule (...)')
        self.assertEqual('现在是晴天', answer)


class PendingReplyTests(unittest.TestCase):
    def event(self, text, uid='owner'):
        return {'user_id': uid, 'group_id': 1001, 'raw_message': text, 'sender': {'card': 'FixtureAdmin'}}

    def run_reply(self, text, pending, uid='owner'):
        sent = []
        with mock.patch.object(bridge, 'OWNERS', {'owner'}), mock.patch.object(bridge, 'ADMINS', set()), \
                mock.patch.object(bridge._rcon_ops, 'pending_confirmation', return_value=pending), \
                mock.patch.object(bridge._rcon_ops, 'dispatch', return_value='[控制台返回] time set day\nSet the time to 1000') as dispatch, \
                mock.patch.object(bridge, 'send_group_msg', side_effect=lambda t, **kw: sent.append(t)):
            bridge._pending_operation_reply_thread(self.event(text, uid))
        return sent, dispatch

    def test_plain_confirm_runs_the_callers_own_command(self):
        sent, dispatch = self.run_reply('确认', {'code': 'ABCD', 'server': LEGACY, 'label': '执行 RCON 命令：give'})
        self.assertEqual('确认 ABCD', dispatch.call_args.args[0])
        self.assertEqual('owner', dispatch.call_args.args[2])
        self.assertEqual('legacy', dispatch.call_args.kwargs['query_fn'].server_id)
        self.assertEqual(LEGACY, dispatch.call_args.kwargs['server'])
        self.assertTrue(sent[0].startswith('[CQ:at,qq=owner] [怀旧] [控制台返回]'))

    def test_cancel_expired_and_members(self):
        _, dispatch = self.run_reply('算了', {'code': 'ABCD', 'server': LEGACY})
        self.assertEqual('取消确认', dispatch.call_args.args[0])
        sent, dispatch = self.run_reply('确认', None)
        dispatch.assert_not_called()
        self.assertIn('已经过了 90 秒', sent[0])
        sent, dispatch = self.run_reply('确认', {'code': 'ABCD', 'server': LEGACY}, uid='member')
        dispatch.assert_not_called()
        self.assertEqual([], sent)


    def test_routing_sends_a_plain_confirm_straight_to_the_handler(self):
        import contextlib
        handler = object.__new__(bridge.BridgeHandler)
        for raw, direct in (('确认', True), ('确认，顺便看看在线几个人', False)):
            with self.subTest(raw=raw), contextlib.ExitStack() as stack:
                event = {'post_type': 'message', 'message_type': 'group', 'group_id': 1001,
                         'user_id': 'owner', 'message_id': 9, 'raw_message': raw,
                         'message': [{'type': 'text', 'data': {'text': raw}}], 'sender': {'card': 'FixtureAdmin'}}
                for name, value in {
                    'OWNERS': {'owner'}, 'ADMINS': set(), 'SANAE_MENTION_ONLY': False,
                    '_bridge_group_enabled': mock.Mock(return_value=True),
                    '_is_duplicate': mock.Mock(return_value=False),
                    '_pending_natural_operation_reply': mock.Mock(return_value=True),
                    '_natural_server_operation_candidate': mock.Mock(return_value=False),
                    '_fun_mute_observe': mock.Mock(return_value=None),
                    '_bug_observe': mock.Mock(return_value=None),
                    '_command_target': mock.Mock(return_value=(None, raw, False, '')),
                }.items():
                    stack.enter_context(mock.patch.object(bridge, name, value))
                stack.enter_context(mock.patch.object(bridge.SOCIAL_ARCHIVE, 'append'))
                for owner, name in ((bridge.FEEDBACK_STORE, 'command'), (bridge.STICKER_CATALOG, 'command')):
                    stack.enter_context(mock.patch.object(owner, name, return_value=None))
                stack.enter_context(mock.patch.object(bridge._network_commands, 'handle', return_value=False))
                stack.enter_context(mock.patch.object(bridge._shop_query, 'is_query', return_value=False))
                worker = stack.enter_context(mock.patch.object(bridge, '_start_worker'))
                queue = stack.enter_context(mock.patch.object(bridge, '_queue_sanae_reply'))
                stack.enter_context(mock.patch.object(bridge, 'send_group_msg', return_value=True))
                handler._process_event(event)
                if direct:
                    self.assertIn(bridge._pending_operation_reply_thread,
                                  [call.args[0] for call in worker.call_args_list])
                    queue.assert_not_called()
                else:
                    queue.assert_called_once()


if __name__ == '__main__':
    unittest.main()
