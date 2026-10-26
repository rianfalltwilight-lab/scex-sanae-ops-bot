"""Offline fusion wiring regressions; no API, QQ sends, or production writes."""
import contextlib
import copy
import importlib
import io
import json
import os
import re
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


os.environ['SCE_BOT_TEST_MODE'] = '1'
# The bridge's default activity log is the production one; tests must never
# append synthetic replies there.
_TEST_ACTIVITY_LOG = Path(os.environ.get('SOCIAL_LITE_ACTIVITY', Path(tempfile.gettempdir()) / 'sanae-test-social-activity.log'))
os.environ.setdefault('SOCIAL_LITE_ACTIVITY', str(_TEST_ACTIVITY_LOG))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network disabled')):
    s = importlib.import_module('sanae_ai')
    bridge = importlib.import_module('llbot_bridge')
    persona = importlib.import_module('sanae_persona')
# Another test module may have imported the bridge first.
bridge.SOCIAL_LITE.activity_path = _TEST_ACTIVITY_LOG


class FusionCase(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for owner, name in ((socket.socket, 'connect'), (socket, 'create_connection')):
            self.stack.enter_context(mock.patch.object(owner, name,
                side_effect=AssertionError('network disabled')))
        self.store = mock.Mock()
        self.store.command.return_value = None
        self.store.observe.return_value = True
        self.store.context.return_value = '有来源的本人记忆标记'
        self.store.prepare_batch.return_value = None
        self.stack.enter_context(mock.patch.object(bridge, 'CONVERSATION_MEMORY', self.store))
        self.lessons = mock.Mock()
        self.lessons.context.return_value = ''
        self.lessons.generation.return_value = 0
        self.lessons.observe.return_value = False
        self.lessons.prepare_batch.return_value = None
        self.lessons.commit_if_generation.side_effect = lambda gid, uid, gen, callback, **kw: callback() or True
        self.stack.enter_context(mock.patch.object(bridge, 'REPLY_LESSONS', self.lessons))
        self.send = self.stack.enter_context(mock.patch.object(bridge, 'send_group_msg', return_value=True))
        self.worker = self.stack.enter_context(mock.patch.object(bridge, '_start_worker', return_value=True))
        self.ai_worker = self.stack.enter_context(mock.patch.object(bridge, '_start_ai_worker', return_value=True))
        self.queue = self.stack.enter_context(mock.patch.object(bridge, '_queue_sanae_reply', return_value=True))
        self.history_clear = self.stack.enter_context(mock.patch.object(s, 'clear_conversation_history'))
        self.recent_clear = self.stack.enter_context(mock.patch.object(bridge.SOCIAL_LITE, 'forget_recent'))
        self.archive = self.stack.enter_context(mock.patch.object(bridge.SOCIAL_ARCHIVE, 'append'))
        for owner, name, value in (
            (bridge.FEEDBACK_STORE, 'command', None),
            (bridge.FEEDBACK_STORE, 'strategy_hint', ''),
            (bridge.STICKER_CATALOG, 'command', None),
            (bridge.STICKER_CATALOG, 'recommend', []),
            (bridge.SOCIAL_LITE, 'expression_style_hint', ''),
            (bridge.SOCIAL_LITE, 'closing_hint', ''),
            (bridge.SOCIAL_LITE, 'observe_closing', None),
            (bridge.SOCIAL_LITE, 'maybe_schedule', (False, 'test-disabled')),
            (bridge._shop_query, 'is_query', False),
            (bridge._network_commands, 'handle', False),
            (bridge._rcon_ops, 'is_known_command', False),
            (s, 'parse_bluemap_request', None),
        ):
            self.stack.enter_context(mock.patch.object(owner, name, return_value=value))
        for name, value in {
            'OWNERS': set(), 'ADMINS': set(), 'SANAE_MENTION_ONLY': True,
            'RELAY_G2S_ENABLED': False, '_is_duplicate': mock.Mock(return_value=False),
            '_fun_mute_observe': mock.Mock(return_value=None),
            '_pending_natural_operation_reply': mock.Mock(return_value=None),
            '_natural_server_operation_candidate': mock.Mock(return_value=False),
            '_selection_reply': mock.Mock(return_value=None),
            '_binding_command': mock.Mock(return_value=None),
            '_media_command': mock.Mock(return_value=None),
            '_media_request_reason': mock.Mock(return_value=None),
            'match_ops_command': mock.Mock(return_value=None),
        }.items():
            self.stack.enter_context(mock.patch.object(bridge, name, value))
        self.stack.enter_context(mock.patch.object(bridge, '_command_target',
            side_effect=lambda raw, *_: (None, raw, False, '')))
        self.stack.enter_context(mock.patch.object(bridge, '_ai_operation_target',
            side_effect=lambda raw, *_: (None, raw, False)))
        self.handler = object.__new__(bridge.BridgeHandler)

    def event(self, text, uid='2001', gid=None, mid='501'):
        segments = [{'type': 'at', 'data': {'qq': qq}} for qq in re.findall(r'\[CQ:at,qq=(\d+)\]', text)]
        segments.append({'type': 'text', 'data': {'text': re.sub(r'\[CQ:at,qq=\d+\]', '', text)}})
        return {'post_type': 'message', 'message_type': 'group',
                'group_id': bridge.SOCIAL_LITE_GROUP if gid is None else gid,
                'user_id': uid, 'self_id': '1002', 'message_id': mid,
                'raw_message': text, 'message': segments,
                'sender': {'nickname': '测试成员'}}

    def dispatch_workers(self):
        for call in self.worker.call_args_list:
            callback = call.args[0]
            args = call.kwargs.get('args', call.args[1] if len(call.args) > 1 else ())
            callback(*args)


class BridgeFusionTests(FusionCase):
    def test_memory_command_replies_once_without_archiving_or_learning_it(self):
        event = self.event('!记忆')
        self.store.command.return_value = '你保存的偏好有一条。'
        self.handler._process_event(event)
        self.store.command.assert_called_once_with(event)
        self.store.observe.assert_not_called()
        self.archive.assert_not_called()
        self.queue.assert_not_called()
        self.worker.assert_called_once()
        self.dispatch_workers()
        self.assertEqual(self.send.call_args.args[0], '你保存的偏好有一条。')

    def test_failed_memory_command_reports_failure_without_model_or_mc_relay(self):
        event = self.event('[CQ:at,qq=1002] !记住 叫我小林')
        self.store.command.side_effect = OSError('fixture write failed')
        with mock.patch.object(bridge, 'RELAY_G2S_ENABLED', True), \
                mock.patch.object(bridge, '_chat_relay_enabled', return_value=True), \
                mock.patch.object(s, '_text_post', side_effect=AssertionError('unexpected model call')):
            self.handler._process_event(event)
        self.store.command.assert_called_once_with(event)
        self.store.observe.assert_not_called()
        self.archive.assert_not_called()
        self.queue.assert_not_called()
        self.ai_worker.assert_not_called()
        self.worker.assert_called_once()
        self.dispatch_workers()
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0], '这次记忆操作没有完成，请稍后再试。')

    def test_forget_command_clears_only_current_person_history_and_group_excerpts(self):
        event = self.event('!忘记 全部')
        self.store.command.return_value = '已忘记你在本群的记忆。'
        self.handler._process_event(event)
        self.history_clear.assert_called_once_with(event['group_id'], event['user_id'])
        self.recent_clear.assert_called_once_with(event['group_id'], user_id=event.get('user_id'), clear_closing=event.get('post_type') != 'notice')
        self.store.observe.assert_not_called()
        self.archive.assert_not_called()

    def test_explicit_at_forget_also_clears_short_history_and_group_excerpts(self):
        event = self.event('[CQ:at,qq=1002] !忘记 全部')
        self.store.command.return_value = '已忘记你的全部。'
        self.handler._process_event(event)
        self.history_clear.assert_called_once_with(event['group_id'], event['user_id'])
        self.recent_clear.assert_called_once_with(event['group_id'], user_id=event.get('user_id'), clear_closing=event.get('post_type') != 'notice')
        self.store.observe.assert_not_called()

    def test_recall_notice_invalidates_derived_memory_and_conversation_caches(self):
        event = {'post_type': 'notice', 'notice_type': 'group_recall',
                 'group_id': bridge.SOCIAL_LITE_GROUP, 'user_id': '2001', 'message_id': '501'}
        self.handler._process_event(event)
        self.store.retract.assert_called_once_with(event['group_id'], '501')
        # Continuity over privacy: no history or group-excerpt wipe on recall.
        self.history_clear.assert_not_called()
        self.recent_clear.assert_not_called()
        self.store.observe.assert_not_called()
        self.send.assert_not_called()

    def test_recall_without_sender_keeps_group_cache(self):
        event = {'post_type': 'notice', 'notice_type': 'group_recall',
                 'group_id': bridge.SOCIAL_LITE_GROUP, 'message_id': '501'}
        self.handler._process_event(event)
        self.history_clear.assert_not_called()
        self.recent_clear.assert_not_called()

    def test_other_group_recall_cannot_invalidate_current_group_memory(self):
        self.handler._process_event({'post_type': 'notice', 'notice_type': 'group_recall',
                                     'group_id': 2104, 'user_id': '2001', 'message_id': '501'})
        self.store.retract.assert_not_called()
        self.history_clear.assert_not_called()
        self.recent_clear.assert_not_called()

    def test_real_current_message_observed_without_model_or_inline_summary(self):
        event = self.event('我喜欢柠檬茶')
        with mock.patch.object(s, '_text_post', side_effect=AssertionError('unexpected model call')):
            self.handler._process_event(event)
        self.store.observe.assert_called_once_with(event)
        self.store.prepare_batch.assert_called_once_with(event['group_id'])
        self.send.assert_not_called()
        self.ai_worker.assert_not_called()

    def test_completed_batch_is_scheduled_off_event_and_existing_ai_queues(self):
        event = self.event('我喜欢柠檬茶')
        batch = {'group_id': event['group_id'], 'messages': [{'message_id': '501'}]}
        self.store.prepare_batch.return_value = batch
        with mock.patch.object(bridge.threading, 'Thread') as thread, \
                mock.patch.object(s, 'summarize_conversation_memory') as model:
            self.handler._process_event(event)
        model.assert_not_called()
        self.ai_worker.assert_not_called()
        self.worker.assert_not_called()
        thread.assert_called_once()
        self.assertIs(thread.call_args.kwargs['target'], bridge._summarize_conversation_memory)
        self.assertEqual(thread.call_args.kwargs['args'], (batch,))
        self.assertTrue(thread.call_args.kwargs['daemon'])
        thread.return_value.start.assert_called_once()

    def test_background_summary_completes_same_batch_without_reply_delivery(self):
        batch = {'messages': [{'message_id': '501', 'text': '来源示例'}]}
        with mock.patch.object(s, 'summarize_conversation_memory', return_value='{"summaries": []}') as model:
            bridge._summarize_conversation_memory(batch)
        model.assert_called_once_with(batch)
        self.store.complete_batch.assert_called_once_with(batch, '{"summaries": []}')
        self.send.assert_not_called()

    def test_failed_background_summary_releases_batch_without_fabricated_result(self):
        batch = {'messages': []}
        with mock.patch.object(s, 'summarize_conversation_memory', side_effect=TimeoutError('fixture')):
            bridge._summarize_conversation_memory(batch)
        self.store.complete_batch.assert_called_once_with(batch, None)
        self.send.assert_not_called()

    def test_bot_self_event_is_not_memory_input(self):
        event = self.event('我喜欢柠檬茶', uid='1004')
        self.handler._process_event(event)
        self.store.observe.assert_not_called()
        self.store.command.assert_not_called()

    def test_unconfigured_group_does_not_observe_or_read_person_memory(self):
        event = self.event('我喜欢柠檬茶', gid=2104)
        self.handler._process_event(event)
        self.store.observe.assert_not_called()
        self.store.command.assert_not_called()
        self.store.context.assert_not_called()

    def test_live_query_does_not_depend_on_memory_store(self):
        event = self.event('!tps')
        self.store.command.side_effect = OSError('unavailable fixture')
        self.store.observe.side_effect = OSError('unavailable fixture')
        target = {'id': 'legacy'}
        with mock.patch.object(bridge, '_match_query', return_value='tps'), \
                mock.patch.object(bridge, '_command_target', return_value=(target, '!tps', True, '')):
            self.handler._process_event(event)
        self.store.command.assert_not_called()
        self.store.observe.assert_not_called()
        self.worker.assert_called_once()
        self.assertEqual(self.worker.call_args.args[0].__name__, '_rcon_query_and_reply')

    def test_admin_operation_does_not_become_person_or_episode_memory(self):
        event = self.event('[CQ:at,qq=1002] 怀旧服执行天气放晴')
        with mock.patch.object(bridge, 'OWNERS', {'2001'}), \
                mock.patch.object(bridge, '_is_ai_operation_turn', return_value=True):
            self.handler._process_event(event)
        self.store.observe.assert_not_called()
        self.store.prepare_batch.assert_not_called()
        self.queue.assert_called_once()

    def test_mention_context_is_scoped_and_does_not_change_clean_message(self):
        event = self.event('[CQ:at,qq=1002] 今天喝什么')
        with mock.patch.object(s, 'sanae_reply', return_value=None) as reply:
            self.handler._sanae_reply_thread(event, True)
        self.store.context.assert_called_once_with(event['group_id'], event['user_id'], event['raw_message'], message_id=event['message_id'])
        self.assertEqual(reply.call_args.args[0], event['raw_message'])
        self.assertIn('有来源的本人记忆标记', reply.call_args.kwargs['context_reference'])
        self.assertFalse(reply.call_args.kwargs['privileged'])
        self.assertTrue(reply.call_args.kwargs['short_reply'])

    def test_ops_reply_does_not_inject_person_memory(self):
        event = self.event('[CQ:at,qq=1002] 怀旧服执行天气放晴')
        with mock.patch.object(bridge, 'OWNERS', {'2001'}), \
                mock.patch.object(bridge, '_is_ai_operation_turn', return_value=True), \
                mock.patch.object(s, 'sanae_reply', return_value=None) as reply:
            self.handler._sanae_reply_thread(event, True)
        self.store.context.assert_not_called()
        self.assertEqual(reply.call_args.kwargs['context_reference'], '')
        self.assertTrue(reply.call_args.kwargs['privileged'])
        self.assertFalse(reply.call_args.kwargs['short_reply'])

    def test_ambient_context_uses_current_person_and_preserves_social_mode(self):
        event = self.event('今天喝什么')
        with mock.patch.object(s, 'sanae_reply', return_value='[SILENT]') as reply:
            self.handler._social_reply_thread(event, '最近只是讨论午餐')
        self.store.context.assert_called_once_with(event['group_id'], event['user_id'], event['raw_message'], message_id=event['message_id'])
        self.assertIn('有来源的本人记忆标记', reply.call_args.kwargs['context_reference'])
        self.assertTrue(reply.call_args.kwargs['social_only'])
        self.assertFalse(reply.call_args.kwargs['privileged'])
        self.send.assert_not_called()


class ModelFusionTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for owner, name in ((socket.socket, 'connect'), (socket, 'create_connection')):
            self.stack.enter_context(mock.patch.object(owner, name,
                side_effect=AssertionError('network disabled')))
        for name in ('_HISTORY', '_CHAT_HISTORY', '_HISTORY_GENERATION', '_LAST_MEMBER', '_LAST_INTERACTION'):
            self.stack.enter_context(mock.patch.object(s, name, {}))

    def test_background_model_call_has_fixed_budget_no_tools_and_no_history(self):
        batch = {'prompt': '仅依据来源摘要。', 'messages': [
            {'message_id': '501', 'user_id': '2001', 'text': '只有测试源正文'}]}
        expected = '{"summaries": []}'
        calls = []

        def bounded(callback, **kwargs):
            calls.append(kwargs)
            return callback()

        response = {'choices': [{'message': {'content': expected}}],
                    'usage': {'prompt_tokens': 12, 'completion_tokens': 5}}
        output = io.StringIO()
        with mock.patch.object(s, 'bounded_call', side_effect=bounded), \
                mock.patch.object(s, '_text_post', return_value=response) as model, \
                mock.patch.object(s, 'run_agent', side_effect=AssertionError('agent forbidden')), \
                contextlib.redirect_stdout(output):
            result = s.summarize_conversation_memory(batch)
        self.assertEqual(result, expected)
        model.assert_called_once()
        payload = model.call_args.args[0]
        self.assertEqual(payload['max_tokens'], 900)
        self.assertNotIn('tools', payload)
        self.assertNotIn('tool_choice', payload)
        self.assertEqual(json.loads(payload['messages'][1]['content']), batch['messages'])
        self.assertEqual(calls[0]['seconds'], 45)
        self.assertLessEqual(calls[0]['budget'].remaining(), 45)
        self.assertEqual(s._HISTORY, {})
        self.assertEqual(s._CHAT_HISTORY, {})
        self.assertNotIn('只有测试源正文', output.getvalue())

    def test_clear_history_scopes_group_user_and_invalidates_old_writers(self):
        pair = [{'role': 'user', 'content': '旧偏好'}, {'role': 'assistant', 'content': '收到'}]
        for group, user in (('a', 'u'), ('a', 'v'), ('b', 'u')):
            s._set_history(group, user, False, pair)
            s._set_chat_history(group, user, False, pair)
        generation = s._HISTORY_GENERATION.get('a', 0)
        s.clear_conversation_history('a', 'u')
        s._set_history('a', 'u', False, pair, expected_generation=generation)
        s._set_chat_history('a', 'u', False, pair, expected_generation=generation)
        self.assertEqual(s._get_history('a', 'u', False), [])
        self.assertEqual(s._get_chat_history('a', 'u', False), [])
        self.assertTrue(s._get_history('a', 'v', False))
        self.assertTrue(s._get_chat_history('b', 'u', False))

    def test_expression_selector_only_runs_in_chat_and_ops_policy_stays_present(self):
        requests = []

        def model(payload, **kwargs):
            requests.append(copy.deepcopy(payload))
            return {'choices': [{'message': {'content': '测试回答'}}], 'usage': {}}

        with mock.patch.object(s, '_text_post', side_effect=model), \
                mock.patch.object(s, '_add_text_usage'), \
                mock.patch.object(s.SHARED_KNOWLEDGE, 'context', return_value=''), \
                mock.patch.object(s, 'execute_tool', side_effect=AssertionError('unexpected tool')), \
                mock.patch.object(s, 'situational_expression_hint', return_value='仅当前闲聊的表达标记') as select:
            s.run_agent('闲聊测试', 'u', False, 'g', social_only=True)
            select.assert_called_once_with('闲聊测试')
            self.assertIn('仅当前闲聊的表达标记', requests[-1]['messages'][0]['content'])
            self.assertIn(s.SANAE_CHAT_SAFETY_POLICY, requests[-1]['messages'][0]['content'])
            self.assertNotIn(s.SANAE_OPERATIONAL_POLICY, requests[-1]['messages'][0]['content'])
            self.assertFalse(requests[-1].get('tools'))
            select.reset_mock()
            s.run_agent('今晚吃点什么', 'u', False, 'g', short_reply=True)
            select.assert_called_once_with('今晚吃点什么')
            self.assertIn('仅当前闲聊的表达标记', requests[-1]['messages'][0]['content'])
            self.assertIn(s.SANAE_OPERATIONAL_POLICY, requests[-1]['messages'][0]['content'])
            self.assertNotIn(s.SANAE_CHAT_SAFETY_POLICY, requests[-1]['messages'][0]['content'])
            self.assertEqual(requests[-1]['tools'], s.TOOLS_MEMBER)
            select.reset_mock()
            s.run_agent('认真解释机制', 'u', False, 'g')
            select.assert_not_called()
            self.assertNotIn('仅当前闲聊的表达标记', requests[-1]['messages'][0]['content'])
            self.assertIn(s.SANAE_OPERATIONAL_POLICY, requests[-1]['messages'][0]['content'])

    def test_forget_during_model_call_prevents_finished_answer_restoring_history(self):
        pair = [{'role': 'user', 'content': '旧的已忘记内容'},
                {'role': 'assistant', 'content': '旧的呼应'}]
        s._set_chat_history('g', 'u', False, pair)

        def model(payload, **kwargs):
            s.clear_conversation_history('g', 'u')
            return {'choices': [{'message': {'content': '完成较早开始的一轮'}}], 'usage': {}}

        with mock.patch.object(s, '_text_post', side_effect=model), \
                mock.patch.object(s, '_add_text_usage'), \
                mock.patch.object(s.SHARED_KNOWLEDGE, 'context', return_value=''), \
                mock.patch.object(s, 'execute_tool', side_effect=AssertionError('unexpected tool')):
            s.run_agent('接着聊', 'u', False, 'g', social_only=True)
        self.assertEqual(s._get_chat_history('g', 'u', False), [])
        self.assertEqual(s._get_history('g', 'u', False), [])

    def test_forget_recent_clears_bot_echoes_and_pending_callbacks_only_in_group(self):
        with tempfile.TemporaryDirectory(prefix='fusion-social-forget-') as root:
            store = bridge.SocialLite(Path(root) / 'state.json', Path(root) / 'activity.log')
            with store._lock:
                store._group('a')['recent'] = [{'sender': 'user', 'text': '忘记的内容'},
                                               {'sender': 'bot', 'text': '重复旧偏好'}]
                store._group('b')['recent'] = [{'sender': 'user', 'text': '其它群保留'}]
                store._pending['a'] = [{'message_id': '501'}]
                store._pending['b'] = [{'message_id': '502'}]
                timer = mock.Mock()
                store._timers['a'] = timer
            store.forget_recent('a')
            self.assertEqual(store._group('a')['recent'], [])
            self.assertTrue(store._group('b')['recent'])
            self.assertNotIn('a', store._pending)
            self.assertNotIn('a', store._timers)
            self.assertIn('b', store._pending)
            timer.cancel.assert_called_once()


if __name__ == '__main__':
    unittest.main()
