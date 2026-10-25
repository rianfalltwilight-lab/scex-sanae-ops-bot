"""Offline conversation regressions; no model API or OneBot messages are sent."""
import copy
import contextlib
import importlib
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ['SCE_BOT_TEST_MODE'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

with mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network disabled')):
    s = importlib.import_module('sanae_ai')
    social = importlib.import_module('social_lite')
    slang = importlib.import_module('slang_learner')
    feedback = importlib.import_module('social_feedback')
    bridge = importlib.import_module('llbot_bridge')


class OfflineCase(unittest.TestCase):
    def setUp(self):
        for target in (mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network disabled')),
                       mock.patch.object(socket, 'create_connection', side_effect=AssertionError('network disabled'))):
            target.start()
            self.addCleanup(target.stop)
        # Replace only in-process caches, leaving any other test's state intact.
        for name in ('_HISTORY', '_CHAT_HISTORY', '_LAST_MEMBER', '_LAST_INTERACTION'):
            if hasattr(s, name):
                patcher = mock.patch.object(s, name, {})
                patcher.start()
                self.addCleanup(patcher.stop)

    def fixture_dir(self):
        directory = tempfile.TemporaryDirectory(prefix='conversation-flow-')
        self.addCleanup(directory.cleanup)
        return Path(directory.name)


class ModelConversationTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.requests = []
        self.next_answer = '嗯，我接着听呢。'

        def model(payload, **_kwargs):
            self.requests.append(copy.deepcopy(payload))
            return {'choices': [{'message': {'content': self.next_answer}}], 'usage': {}}

        for patcher in (mock.patch.object(s, '_text_post', side_effect=model),
                        mock.patch.object(s, '_add_text_usage'),
                        mock.patch.object(s.SHARED_KNOWLEDGE, 'context', return_value=''),
                        mock.patch.object(s, 'execute_tool', side_effect=AssertionError('unexpected tool'))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def ask(self, text, *, uid='member', gid='group-a', privileged=False, **kwargs):
        answer, _, used = s.run_agent(text, uid, privileged, gid, **kwargs)
        self.assertFalse(used)
        return answer

    def request_text(self):
        return '\n'.join(str(row.get('content', '')) for row in self.requests[-1]['messages'][1:])

    def test_serious_and_social_histories_do_not_cross(self):
        self.ask('严肃问题标记：机械功率怎么算')
        self.ask('闲聊锚点：今晚做番茄面', social_only=True)
        self.assertNotIn('机械功率', self.request_text())
        self.ask('面里再加个蛋呢', short_reply=True)
        self.assertIn('今晚做番茄面', self.request_text())
        self.ask('接着解释刚才的公式')
        self.assertIn('机械功率', self.request_text())
        self.assertNotIn('番茄面', self.request_text())

    def test_social_history_scopes_group_user_and_permission(self):
        self.ask('只有这一位看得到的聊天锚点', social_only=True)
        for kwargs in ({'gid': 'group-b'}, {'uid': 'another'}, {'privileged': True}):
            with self.subTest(**kwargs):
                self.ask('这边有什么上下文', social_only=True, **kwargs)
                self.assertNotIn('只有这一位看得到', self.request_text())

    def test_social_history_survives_old_ttl_and_expires_after_two_hours(self):
        with mock.patch.object(s.time, 'time', return_value=10000):
            self.ask('两小时内要记住的聊天', social_only=True)
        with mock.patch.object(s.time, 'time', return_value=17200):
            self.assertIn('两小时内要记住', str(s._get_chat_history('group-a', 'member', False)))
        with mock.patch.object(s.time, 'time', return_value=17201):
            self.assertEqual(s._get_chat_history('group-a', 'member', False), [])

    def test_social_history_character_limit_keeps_complete_recent_turns(self):
        for index in range(14):
            self.next_answer = f'回答{index}：' + '答' * 900
            self.ask(f'聊天{index}：' + '问' * 900, social_only=True)
        self.next_answer = '收到'
        self.ask('核对缓存边界', short_reply=True)
        old = self.requests[-1]['messages'][1:-1]
        self.assertLessEqual(len(old), 24)
        self.assertLessEqual(sum(len(str(row['content'])) for row in old), 18000)
        self.assertEqual(len(old) % 2, 0)
        self.assertNotIn('聊天0：', self.request_text())
        self.assertIn('聊天13：', self.request_text())
        self.assertGreater(len(old), 8)

    def test_twelve_round_limit_retains_more_than_original_four(self):
        for index in range(14):
            self.ask(f'短聊{index}', social_only=True)
        self.ask('继续短聊', short_reply=True)
        self.assertEqual(len(self.requests[-1]['messages'][1:-1]), 24)
        prior_users = [row['content'] for row in self.requests[-1]['messages'][1:-1] if row['role'] == 'user']
        self.assertNotIn('短聊0', prior_users)
        self.assertNotIn('短聊1', prior_users)
        self.assertIn('短聊2', prior_users)

    def test_recent_chat_prevents_stale_serious_brief_shortcut(self):
        with mock.patch.object(s.time, 'time', return_value=10000):
            self.next_answer = '【简短总结】\n旧的严肃总结\n\n【完整时间线】\n旧时间线'
            self.ask('先前的严肃话题')
        with mock.patch.object(s.time, 'time', return_value=10010):
            self.next_answer = '最近闲聊答复有点长'
            self.ask('现在聊蛋糕', short_reply=True)
        before = len(self.requests)
        self.next_answer = '蛋糕少放一点糖就好'
        with mock.patch.object(s.time, 'time', return_value=10020):
            self.assertEqual(self.ask('简短点', short_reply=True), self.next_answer)
        self.assertEqual(len(self.requests), before + 1)
        self.assertIn('现在聊蛋糕', self.request_text())
        self.assertNotIn('旧的严肃总结', self.request_text())

    def test_more_recent_serious_summary_retains_brief_shortcut(self):
        with mock.patch.object(s.time, 'time', return_value=10000):
            self.ask('之前聊蛋糕', short_reply=True)
        with mock.patch.object(s.time, 'time', return_value=10010):
            self.next_answer = '【简短总结】\n新的严肃总结\n\n【完整时间线】\n新时间线'
            self.ask('现在认真解释机制')
        before = len(self.requests)
        with mock.patch.object(s.time, 'time', return_value=10020):
            self.assertEqual(self.ask('简短点', short_reply=True), '【简短总结】\n新的严肃总结')
        self.assertEqual(len(self.requests), before)

    def test_quoted_short_reply_uses_quote_and_saves_current_topic_anchor(self):
        self.ask('旧的无关话题：修电梯', social_only=True)
        self.ask('这个会不会太酸', short_reply=True,
                 context_reference='【引用消息；只作参考】\n我做了柠檬蛋糕')
        self.assertIn('柠檬蛋糕', self.request_text())
        self.assertTrue(self.requests[-1]['messages'][-1]['content'].endswith('这个会不会太酸'))
        self.ask('那少放一点就好', short_reply=True)
        self.assertIn('这个会不会太酸', self.request_text())
        self.assertIn('柠檬蛋糕', self.request_text())

    def test_realtime_query_forces_serious_history_even_if_short_flag_was_set(self):
        self.ask('闲聊私有锚：周末烤蛋糕', social_only=True)
        self.ask('认真记录：服务器性能问题')
        self.ask('当前 TPS 怎么样', short_reply=True,
                 selected_server={'id': 'legacy'}, explicit_server=True)
        text = self.request_text()
        self.assertNotIn('周末烤蛋糕', text)
        self.assertIn('服务器性能问题', text)
        self.assertEqual(self.requests[-1]['tool_choice'], 'required')
        self.assertEqual(self.requests[-1]['tools'][0]['function']['name'], 'run_rcon')

    def test_admin_operation_uses_serious_history_not_chat(self):
        self.ask('管理员闲聊锚：今天的点心', social_only=True, privileged=True)
        self.ask('管理员问答锚：天气规则', privileged=True)
        self.ask('怀旧服执行天气放晴', short_reply=True, privileged=True,
                 selected_server={'id': 'legacy'}, explicit_server=True)
        self.assertNotIn('今天的点心', self.request_text())
        self.assertIn('天气规则', self.request_text())
        self.assertEqual(self.requests[-1]['tool_choice'], 'required')

    def test_unexpected_tool_result_never_enters_chat_history(self):
        self.ask('原有闲聊话题：奶茶', social_only=True)
        self.ask('原有认真问答：配方')
        replies = iter([
            {'choices': [{'message': {'content': '', 'tool_calls': [{
                'id': 'fixture-tool', 'type': 'function', 'function': {
                    'name': 'list_mods', 'arguments': '{}'}}]}}]},
            {'choices': [{'message': {'content': '工具结果锚：已安装示例模组'}}]},
        ])
        with mock.patch.object(s, '_text_post', side_effect=lambda *_a, **_k: next(replies)), \
                mock.patch.object(s, 'execute_tool', return_value='示例模组'):
            answer, _, used = s.run_agent('这次顺带查一下资料', 'member', False,
                                         'group-a', short_reply=True)
        self.assertTrue(used)
        self.assertIn('工具结果锚', answer)
        self.ask('闲聊还记得什么', social_only=True)
        self.assertIn('奶茶', self.request_text())
        self.assertNotIn('工具结果锚', self.request_text())
        self.ask('认真问答继续')
        self.assertIn('工具结果锚', self.request_text())
        self.assertIn('原有认真问答', self.request_text())

    def test_silent_is_not_saved_as_an_assistant_turn(self):
        self.next_answer = '[SILENT]'
        self.ask('第三方的话不必插嘴', social_only=True)
        self.next_answer = '在呢'
        self.ask('早苗在吗', short_reply=True)
        self.assertNotIn('[SILENT]', self.request_text())

    def test_shared_personality_reaches_both_model_branches(self):
        self.ask('陪我聊聊天', social_only=True)
        social_prompt = self.requests[-1]['messages'][0]['content']
        self.ask('解释一个机制')
        answer_prompt = self.requests[-1]['messages'][0]['content']
        self.assertTrue(s.SANAE_PERSONALITY.strip())
        self.assertIn(s.SANAE_PERSONALITY, social_prompt)
        self.assertIn(s.SANAE_PERSONALITY, answer_prompt)

    def test_social_mode_never_advertises_member_or_admin_tools(self):
        for privileged in (False, True):
            with self.subTest(privileged=privileged):
                self.ask('普通聊天', social_only=True, privileged=privileged)
                self.assertFalse(self.requests[-1].get('tools'))


class InvocationCooldownTests(OfflineCase):
    def setUp(self):
        super().setUp()
        clock = mock.patch.object(s.time, 'time', return_value=10000)
        agent = mock.patch.object(s, 'run_agent', return_value=('回答', mock.Mock(calls=0), False))
        quote = mock.patch.object(s, '_with_reply_context', side_effect=lambda raw, **kwargs: raw)
        for patcher in (clock, agent, quote):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clock = s.time.time
        self.agent = s.run_agent

    def reply(self, *, uid='member', gid='group-a', explicit=True, **kwargs):
        return s.sanae_reply('你好呀', uid, group_id=gid, force=True,
                             explicit_interaction=explicit, include_usage_footer=False, **kwargs)

    def test_explicit_cooldown_is_two_and_half_seconds(self):
        self.assertEqual(self.reply(), '回答')
        self.clock.return_value = 10002.49
        self.assertIsNone(self.reply())
        self.clock.return_value = 10002.51
        self.assertEqual(self.reply(), '回答')

    def test_explicit_throttle_is_per_group_and_user(self):
        self.assertEqual(self.reply(), '回答')
        self.assertEqual(self.reply(gid='group-b'), '回答')
        self.assertEqual(self.reply(uid='another'), '回答')
        self.assertFalse(self.agent.call_args.args[2])

    def test_ambient_still_uses_thirty_second_cooldown(self):
        self.assertEqual(self.reply(explicit=False, social_only=True), '回答')
        self.clock.return_value = 10003
        self.assertIsNone(self.reply(explicit=False, social_only=True))
        self.clock.return_value = 10030.01
        self.assertEqual(self.reply(explicit=False, social_only=True), '回答')

    def test_explicit_bypasses_ambient_throttle_without_becoming_admin(self):
        self.assertEqual(self.reply(explicit=False, social_only=True), '回答')
        self.assertEqual(self.reply(short_reply=True), '回答')
        self.assertFalse(self.agent.call_args.args[2])
        self.assertEqual(self.agent.call_args.kwargs['short_reply'], True)

    def test_live_tps_queries_accept_chinese_spacing_but_not_explanation_or_url_words(self):
        for text in ('服务器当前TPS怎么样', '当前 TPS 怎么样'):
            with self.subTest(text=text):
                self.assertTrue(s._has_server_query_intent(text))
        for text in ('TPS是什么', 'TPS 是什么意思', '现在https能用吗', '当前httpsProxy状态'):
            with self.subTest(text=text):
                self.assertFalse(s._has_server_query_intent(text))


class SocialVocabularyTests(OfflineCase):
    def event(self, text='早苗在吗', uid='member'):
        return {'post_type': 'message', 'message_type': 'group', 'group_id': 1001,
                'user_id': uid, 'self_id': '1002', 'raw_message': text,
                'sender': {'nickname': '测试员'}}

    def test_explicit_classifier_covers_name_reply_and_recent_followup_only(self):
        directory = self.fixture_dir()
        layer = social.SocialLite(directory/'state.json', directory/'activity.log',
                                  spontaneous_probability=1, ambient_reply_weight=0)
        with mock.patch.object(layer, '_save'), mock.patch.object(layer, '_activity'):
            self.assertTrue(layer.is_explicit_interaction(self.event()))
            replied = self.event('这下明白了')
            replied['reply'] = {'user_id': '1002'}
            self.assertTrue(layer.is_explicit_interaction(replied))
            self.assertFalse(layer.is_explicit_interaction(self.event('普通闲聊')))
            self.assertFalse(layer.is_explicit_interaction(self.event('然后呢')))
            layer.record_outgoing(1001, '上一句')
            self.assertTrue(layer.is_explicit_interaction(self.event('然后呢')))
            self.assertEqual(layer.spontaneous_probability, 1)
            self.assertEqual(layer.ambient_reply_weight, 0)

    def test_slang_requires_current_confirmed_term_and_ignores_punctuation(self):
        learner = slang.SlangLearner(self.fixture_dir()/'slang.json')
        learner._entries = [
            {'term': 'yyds', 'meaning': '永远的神', 'status': 'confirmed', 'count': 3},
            {'term': '抽象梗', 'meaning': '暂未确认', 'status': 'candidate', 'count': 9},
            {'term': '？', 'meaning': '纯标点', 'status': 'confirmed', 'count': 99}]
        self.assertEqual(learner.context(), '')
        self.assertEqual(learner.context(text='今天吃什么？'), '')
        self.assertEqual(learner.context(text='抽象梗'), '')
        context = learner.context(text='这次真的yyds')
        self.assertIn('yyds：永远的神', context)
        self.assertNotIn('暂未确认', context)
        self.assertNotIn('纯标点', context)

    def test_negative_feedback_does_not_prescribe_even_shorter_replies(self):
        store = feedback.FeedbackStore(self.fixture_dir()/'feedback.json')
        store._items = [{'rating': 'negative'}, {'rating': 'negative'}]
        self.assertNotIn('更短', store.strategy_hint())

    def test_short_line_bubbles_keep_safe_transport_bounds(self):
        self.assertEqual(social.split_social_reply('先喝口水\n我慢慢听你说'), ['先喝口水', '我慢慢听你说'])
        seven = '\n'.join(str(x) for x in range(7))
        self.assertEqual(social.split_social_reply(seven), [seven])


class BridgeConversationTests(OfflineCase):
    def event(self, text, uid='member'):
        return {'post_type': 'message', 'message_type': 'group',
                'group_id': bridge.SOCIAL_LITE_GROUP, 'user_id': uid,
                'self_id': '1002', 'raw_message': text,
                'sender': {'nickname': '测试员'}}

    def test_capability_question_recognizes_direct_requests_only(self):
        for text in ('早苗，你能做什么？', '早苗，你有哪些功能？',
                     '[CQ:at,qq=1002] 你有什么指令', '早苗，功能列表'):
            with self.subTest(text=text):
                self.assertTrue(bridge._sanae_capability_question(self.event(text)))
        for text in ('别人说早苗你能做什么', '早苗，刚才小明问你能做什么',
                     '这些指令是什么时候加的', '[CQ:at,qq=other] 你有什么指令',
                     '早苗，我觉得那些指令挺有意思', '早苗，为什么这个命令会报错'):
            with self.subTest(text=text):
                self.assertFalse(bridge._sanae_capability_question(self.event(text)))

    def test_capability_question_does_not_trigger_from_reply_text(self):
        event = self.event('早苗，这句话挺有意思')
        event['reply'] = {'user_id': 'other', 'raw_message': '早苗你能做什么'}
        self.assertFalse(bridge._sanae_capability_question(event))

    def test_capability_route_uses_existing_privilege_specific_help(self):
        handler = object.__new__(bridge.BridgeHandler)
        for privileged in (False, True):
            with self.subTest(privileged=privileged), contextlib.ExitStack() as stack:
                event = self.event('[CQ:at,qq=1002] 你能做什么？')
                for name, value in {
                    'OWNERS': {'member'} if privileged else set(), 'ADMINS': set(),
                    '_bridge_group_enabled': mock.Mock(return_value=True),
                    '_is_duplicate': mock.Mock(return_value=False),
                    '_pending_natural_operation_reply': mock.Mock(return_value=None),
                    '_natural_server_operation_candidate': mock.Mock(return_value=False),
                    '_fun_mute_reason': mock.Mock(return_value=None),
                    'match_ops_command': mock.Mock(return_value=False),
                    '_command_target': mock.Mock(return_value=(None, event['raw_message'], False, '')),
                }.items():
                    stack.enter_context(mock.patch.object(bridge, name, value))
                for owner, name in ((bridge.SOCIAL_ARCHIVE, 'append'),
                                    (bridge.SOCIAL_MEMORY, 'observe'),
                                    (bridge.SLANG_LEARNER, 'observe')):
                    stack.enter_context(mock.patch.object(owner, name))
                for owner, name in ((bridge.SLANG_LEARNER, 'command'),
                                    (bridge.FEEDBACK_STORE, 'command'),
                                    (bridge.STICKER_CATALOG, 'command')):
                    stack.enter_context(mock.patch.object(owner, name, return_value=None))
                stack.enter_context(mock.patch.object(bridge._network_commands, 'handle', return_value=False))
                stack.enter_context(mock.patch.object(bridge._shop_query, 'is_query', return_value=False))
                help_call = stack.enter_context(mock.patch.object(bridge._rcon_ops, 'cmd_help', return_value='已有帮助原文'))
                worker = stack.enter_context(mock.patch.object(bridge, '_start_worker'))
                queue = stack.enter_context(mock.patch.object(bridge, '_queue_sanae_reply'))
                send = stack.enter_context(mock.patch.object(bridge, 'send_group_msg', return_value=True))
                handler._process_event(event)
                help_call.assert_called_once_with(privileged)
                queue.assert_not_called()
                worker.assert_called_once()
                worker.call_args.args[0]()
                self.assertEqual(send.call_args.args[0], '已有帮助原文')

    def social_bridge_patches(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for owner, name, value in (
            (bridge.SLANG_LEARNER, 'context', ''),
            (bridge.SOCIAL_MEMORY, 'context', ''),
            (bridge.FEEDBACK_STORE, 'strategy_hint', ''),
            (bridge.SOCIAL_LITE, 'expression_style_hint', ''),
            (bridge.STICKER_CATALOG, 'recommend', []),
        ):
            stack.enter_context(mock.patch.object(owner, name, return_value=value))
        stack.enter_context(mock.patch.object(bridge, 'send_group_msg', side_effect=AssertionError('unexpected send')))
        return stack

    def test_social_bridge_passes_explicit_flag_and_current_slang_text(self):
        self.social_bridge_patches()
        for explicit in (False, True):
            with self.subTest(explicit=explicit), \
                    mock.patch.object(bridge.SOCIAL_LITE, 'is_explicit_interaction', return_value=explicit), \
                    mock.patch.object(bridge._sanae_ai, 'sanae_reply', return_value='[SILENT]') as reply:
                event = self.event('早苗，晚饭吃什么')
                bridge.BridgeHandler._social_reply_thread(None, event, '上一句谈晚饭')
                self.assertEqual(reply.call_args.kwargs['explicit_interaction'], explicit)
                self.assertFalse(reply.call_args.kwargs['privileged'])
                self.assertTrue(reply.call_args.kwargs['social_only'])
                bridge.SLANG_LEARNER.context.assert_called_with(text=event['raw_message'])

    def test_mention_bridge_preserves_member_permissions_and_short_flag(self):
        self.social_bridge_patches()
        event = self.event('[CQ:at,qq=1002] 接着说呀')
        with mock.patch.object(bridge, 'OWNERS', set()), \
                mock.patch.object(bridge, 'ADMINS', set()), \
                mock.patch.object(bridge, '_ai_operation_target', return_value=(None, event['raw_message'], False)), \
                mock.patch.object(bridge._sanae_ai, 'sanae_reply', return_value=None) as reply:
            bridge.BridgeHandler._sanae_reply_thread(None, event, True)
        self.assertTrue(reply.call_args.kwargs['explicit_interaction'])
        self.assertTrue(reply.call_args.kwargs['short_reply'])
        self.assertFalse(reply.call_args.kwargs['privileged'])
        bridge.SLANG_LEARNER.context.assert_called_once_with(text=event['raw_message'])

    def test_mention_bridge_does_not_label_live_query_or_admin_action_as_short_chat(self):
        self.social_bridge_patches()
        for text, privileged in (('当前 TPS 怎么样', False), ('怀旧服执行天气放晴', True)):
            event = self.event('[CQ:at,qq=1002] ' + text)
            with self.subTest(text=text), \
                    mock.patch.object(bridge, 'OWNERS', {'member'} if privileged else set()), \
                    mock.patch.object(bridge, 'ADMINS', set()), \
                    mock.patch.object(bridge, '_ai_operation_target', return_value=({'id': 'legacy'}, event['raw_message'], True)), \
                    mock.patch.object(bridge._sanae_ai, 'sanae_reply', return_value=None) as reply:
                bridge.BridgeHandler._sanae_reply_thread(None, event, True)
                self.assertFalse(reply.call_args.kwargs['short_reply'])
                self.assertEqual(reply.call_args.kwargs['privileged'], privileged)


if __name__ == '__main__':
    unittest.main()
