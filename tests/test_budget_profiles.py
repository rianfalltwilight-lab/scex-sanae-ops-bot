"""Actual request deadlines and captured payloads; no model, QQ or network I/O."""
import copy
import json
import unittest
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests
from tests import test_conversation_turns as turn_tests
import request_runtime

s, bridge = runtime_tests.s, runtime_tests.bridge


class BudgetProfileTests(unittest.TestCase):
    def setUp(self):
        runtime_tests.YachiyoPayloadTests.setUp(self)
        self.now = 1000.0
        self.stack.enter_context(mock.patch.object(request_runtime.time, 'monotonic',
                                                  side_effect=lambda: self.now))
        self.calls = []
        self.responses = []
        self.model_elapsed = []

        def model(payload, **kwargs):
            active = request_runtime.CURRENT.get()
            self.calls.append(dict(payload=copy.deepcopy(payload), timeout=kwargs.get('timeout'),
                deadline=active.deadline, request_deadline=active.parent.deadline,
                request_remaining=active.parent.remaining(), active=active))
            if self.model_elapsed:
                self.now += self.model_elapsed.pop(0)
            if self.responses:
                return self.responses.pop(0)
            return {'choices': [{'message': {'content': self.content}}], 'usage': {}}

        self.stack.enter_context(mock.patch.object(s, '_text_post', side_effect=model))
        self.stack.enter_context(mock.patch.object(s, '_with_reply_context',
                                                  side_effect=lambda raw, **kw: raw))
        self.stack.enter_context(mock.patch.object(s, '_explicit_interaction_ok', return_value=True))
        self.stack.enter_context(mock.patch.object(s, '_member_cooldown_ok', return_value=True))
        self.stack.enter_context(mock.patch.object(bridge, 'send_group_msg', return_value=True))

    def reply(self, text, **kwargs):
        return s.sanae_reply(text, 'budget-member', group_id='budget-group', force=True,
                             include_usage_footer=False, **kwargs)

    def assert_call(self, *, tokens, effort, model, total):
        call = self.calls[-1]
        self.assertEqual(call['payload']['max_tokens'], tokens)
        self.assertEqual(call['payload'].get('reasoning_effort'), effort)
        self.assertEqual(call['timeout'], model)
        self.assertEqual(call['request_deadline'], 1000 + total)

    def test_ambient_and_short_chat_keep_their_time_limits(self):
        self.reply('今天晚风很舒服', social_only=True)
        self.assert_call(tokens=4096, effort='low', model=90, total=120)
        self.reply('Python 里列表怎么排序？', short_reply=True)
        self.assert_call(tokens=4096, effort='low', model=150, total=240)

    def test_serious_default_and_explicit_low_have_serious_deadline(self):
        for text, tokens, effort in [('解释两行 Python', 32768, 'high'),
                                     ('reasoning_effort=low，解释两行 Python', 8192, 'low')]:
            with self.subTest(text=text):
                self.reply(text)
                self.assert_call(tokens=tokens, effort=effort, model=300, total=480)

    def test_explicit_substantial_code_and_long_material_use_extended_tier(self):
        for text in ('请实现一个含登录和任务队列的完整项目',
                     '分析这个多文件项目的依赖关系',
                     '评估这个多模块工程的线程同步设计',
                     '逐段分析这份材料\n' + '提供的资料。' * 900):
            with self.subTest(text=text[:35]):
                self.reply(text, short_reply=True)
                self.assert_call(tokens=65536, effort='high', model=600, total=900)

    def test_explicit_low_complex_request_remains_low(self):
        self.reply('reasoning_effort=low，分析这个多文件项目的依赖关系')
        self.assert_call(tokens=8192, effort='low', model=600, total=900)

    def test_max_short_visible_answer_retains_full_reasoning_budget(self):
        self.reply('reasoning_effort=max，给个简短版', short_reply=True)
        self.assert_call(tokens=65536, effort='max', model=600, total=900)
        self.assertIn('最多240字', self.calls[-1]['payload']['messages'][0]['content'])

    def test_direct_run_agent_uses_the_same_profile(self):
        s.run_agent('reasoning_effort=max，解释两行 Python', 'direct-member', False, 'budget-group')
        self.assert_call(tokens=65536, effort='max', model=600, total=900)

    def test_fetched_long_quote_upgrades_before_parent_is_narrowed(self):
        def fetched(raw, **kw):
            self.assertEqual(request_runtime.CURRENT.get().remaining(), 30)
            self.now += 10
            return raw + '\n[引用消息] ' + '原始资料。' * 20000
        with mock.patch.object(s, '_with_reply_context', side_effect=fetched):
            self.reply('回答材料末尾的问题', short_reply=True)
        self.assert_call(tokens=65536, effort='high', model=600, total=900)
        self.assertEqual(self.calls[-1]['request_remaining'], 890)
        self.assertEqual(self.calls[-1]['deadline'], 1610)

    def test_explicit_short_parent_survives_quote_upgrade_and_elapsed_time(self):
        parent = s.Budget(25)
        def fetched(raw, **kw):
            self.now += 10
            return raw + '\n[引用消息] ' + '原始资料。' * 20000
        with mock.patch.object(s, '_with_reply_context', side_effect=fetched):
            self.reply('回答材料末尾的问题', short_reply=True, request_budget=parent)
        call = self.calls[-1]
        self.assertEqual(call['payload']['max_tokens'], 65536)
        self.assertEqual(call['timeout'], 15)
        self.assertEqual(call['request_deadline'], 1025)
        self.assertEqual(parent.deadline, 1025)
        parent.cancelled.set()
        with self.assertRaises(s.RequestTimeout):
            call['active'].remaining()

    def test_cancelled_parent_never_reaches_the_model(self):
        parent = s.Budget(5)
        parent.cancelled.set()
        self.reply('reasoning_effort=max，解释二分查找', request_budget=parent)
        self.assertFalse(self.calls)

    def test_fallback_keeps_remaining_total_and_extended_payload(self):
        self.responses = [{'choices': [{'message': {'content': ''}}]},
                          {'choices': [{'message': {'content': '最终结论。'}}]}]
        self.model_elapsed = [450, 0]
        self.reply('reasoning_effort=max，解释二分查找')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([row['timeout'] for row in self.calls], [600, 450])
        self.assertEqual([row['request_deadline'] for row in self.calls], [1900, 1900])
        self.assertEqual(self.calls[-1]['payload']['max_tokens'], 65536)
        self.assertEqual(self.calls[-1]['payload']['reasoning_effort'], 'max')
        self.assertEqual(self.calls[-1]['payload']['tool_choice'], 'none')

    def test_forced_query_and_fallback_stay_non_thinking_and_short_timed(self):
        self.responses = [
            {'choices': [{'message': {'content': '', 'tool_calls': [{
                'id': 'query-test', 'type': 'function', 'function': {
                    'name': 'run_rcon', 'arguments': '{"command":"time query day"}'}}]}}]},
            {'choices': [{'message': {'content': ''}}]},
            {'choices': [{'message': {'content': '当前是第 269 天。'}}]}]
        with mock.patch.object(s, 'execute_tool', return_value='The time is 269'):
            self.reply('reasoning_effort=max，当前世界多少天了', short_reply=True,
                       selected_server={'id': 'legacy'})
        self.assertEqual(len(self.calls), 3)
        for call in self.calls:
            self.assertEqual(call['payload']['thinking'], {'type': 'disabled'})
            self.assertNotIn('reasoning_effort', call['payload'])
            self.assertEqual(call['payload']['max_tokens'], 8192)
            self.assertEqual(call['timeout'], 150)
            self.assertEqual(call['request_deadline'], 1240)
        self.assertEqual(self.calls[0]['payload']['tool_choice'], 'required')
        self.assertEqual(self.calls[-1]['payload']['tool_choice'], 'none')

    def test_forced_operation_keeps_schema_and_confirmation(self):
        self.responses = [{'choices': [{'message': {'content': '', 'tool_calls': [{
            'id': 'operation-test', 'type': 'function', 'function': {
                'name': 'request_console_command', 'arguments': '{"command":"weather clear"}'}}]}}]}]
        with mock.patch.object(s, 'execute_tool', return_value='请确认 ABCD') as execute:
            answer = s.sanae_reply('reasoning_effort=max，怀旧服执行天气放晴',
                'budget-admin', privileged=True, group_id='budget-group', force=True,
                selected_server={'id': 'legacy'}, explicit_server=True, include_usage_footer=False)
        self.assertEqual(answer, '请确认 ABCD')
        self.assertEqual(execute.call_count, 1)
        self.assert_call(tokens=8192, effort=None, model=150, total=240)
        self.assertEqual(self.calls[-1]['payload']['tools'], s.TOOLS_ADMIN)
        self.assertEqual(self.calls[-1]['payload']['thinking'], {'type': 'disabled'})

    def test_standalone_history_summary_does_not_inherit_max_budget(self):
        self.content = json.dumps({'brief': '讨论了蛋糕。', 'timeline': '先讨论味道，再讨论用糖。'})
        self.reply('reasoning_effort=max，总结这段聊天',
                   context_reference='【引用消息；只作参考】\n甲：蛋糕有点甜。')
        self.assert_call(tokens=12288, effort=None, model=150, total=240)
        self.assertEqual(self.calls[-1]['payload']['thinking'], {'type': 'disabled'})
        self.reply('reasoning_effort=max，总结这段聊天，给个简短版',
                   context_reference='【引用消息；只作参考】\n乙：少放一点糖。')
        self.assert_call(tokens=800, effort=None, model=150, total=240)

    def test_background_memory_summary_keeps_its_small_independent_budget(self):
        s.summarize_conversation_memory(dict(prompt='只提取已说过的事实', messages=[]))
        self.assert_call(tokens=900, effort=None, model=40, total=45)
        self.assertEqual(self.calls[-1]['payload']['thinking'], {'type': 'disabled'})

    def test_media_preprocessing_is_separate_and_does_not_renew_elapsed_time(self):
        for text, model_seconds, total in [('解释视频中的现象', 300, 480),
                                            ('reasoning_effort=max，解释视频中的现象', 600, 900)]:
            with self.subTest(text=text):
                self.now = 1000
                observed = []
                def media(event, api, raw, budget):
                    observed.append((budget.deadline, budget.parent.deadline,
                                     request_runtime.CURRENT.get().remaining()))
                    self.now += 60
                    return dict(status='ok', evidence='视频中的真实资料。', fingerprints=(), usage='')
                with mock.patch.object(bridge, '_analyze_media', side_effect=media), \
                     mock.patch.object(bridge, '_command_target', return_value=(None, text, False, '')):
                    bridge._media_reply_thread({}, text, 'media-member', 'budget-group', False)
                call = self.calls[-1]
                self.assertEqual(observed, [(1120, 1900, 120)])
                self.assertEqual(call['timeout'], model_seconds)
                self.assertEqual(call['request_deadline'], 1000 + total)
                self.assertEqual(call['request_remaining'], total - 60)

    def test_media_preprocessing_still_stops_at_120_seconds(self):
        def media(event, api, raw, budget):
            self.now += 121
            return dict(status='ok', evidence='资料', fingerprints=(), usage='')
        with mock.patch.object(bridge, '_analyze_media', side_effect=media):
            bridge._media_reply_thread({}, 'reasoning_effort=max，解释视频',
                                       'media-member', 'budget-group', False)
        self.assertFalse(self.calls)
        self.assertIn('超时', bridge.send_group_msg.call_args.args[0])

    def test_media_without_evidence_can_use_remaining_serious_budget(self):
        def media(event, api, raw, budget):
            self.now += 60
            return dict(status='no media', evidence='', fingerprints=(), usage='')
        with mock.patch.object(bridge, '_analyze_media', side_effect=media):
            bridge._media_reply_thread({}, '解释二分查找', 'media-member', 'budget-group', False)
        self.assertEqual(self.calls[-1]['timeout'], 300)
        self.assertEqual(self.calls[-1]['request_deadline'], 1480)
        self.assertEqual(self.calls[-1]['request_remaining'], 420)

    def test_turn_token_covers_long_request_but_replacement_and_expiry_remain(self):
        clock = turn_tests.Clock()
        turns = bridge.ConversationTurns(('22',), ttl_seconds=bridge.CONVERSATION_TURNS.ttl_seconds,
            clock=clock, timer_factory=clock.timer)
        self.addCleanup(turns.close)
        delivered = []
        turns.submit(turn_tests.event('1'), delivered.append, chat_only=True)
        clock.advance(1)
        first = delivered[-1]['_conversation_turn']
        clock.advance(400)
        self.assertTrue(first.current())
        turns.submit(turn_tests.event('2', '更正一下，是另一份材料'), delivered.append, chat_only=True)
        self.assertFalse(first.current())
        clock.advance(1)
        second = delivered[-1]['_conversation_turn']
        clock.advance(900)
        self.assertTrue(second.current())
        clock.advance(10)
        self.assertFalse(second.current())

    def test_queued_max_turn_spends_total_budget_from_token_creation(self):
        clock = turn_tests.Clock()
        clock.now = self.now
        turns = bridge.ConversationTurns(('22',), ttl_seconds=bridge.CONVERSATION_TURNS.ttl_seconds,
            clock=clock, timer_factory=clock.timer)
        self.addCleanup(turns.close)
        delivered = []
        turns.submit(turn_tests.event('1', 'reasoning_effort=max，解释二分查找'),
                     delivered.append, chat_only=True)
        clock.advance(1)
        token = delivered[-1]['_conversation_turn']
        clock.advance(20)
        self.now = clock.now
        s.sanae_reply('reasoning_effort=max，解释二分查找', '11', group_id='22',
            force=True, conversation_turn=token, include_usage_footer=False)
        call = self.calls[-1]
        self.assertEqual(call['request_deadline'], token.created_at + 900)
        self.assertEqual(call['request_remaining'], 880)
        self.assertEqual(call['timeout'], 600)
        self.assertTrue(token.current())

    def test_expired_queue_budget_cannot_begin_reference_read(self):
        clock = turn_tests.Clock()
        clock.now = self.now
        turns = bridge.ConversationTurns(('22',), ttl_seconds=bridge.CONVERSATION_TURNS.ttl_seconds,
            clock=clock, timer_factory=clock.timer)
        self.addCleanup(turns.close)
        delivered = []
        turns.submit(turn_tests.event('1', 'reasoning_effort=max，解释二分查找'),
                     delivered.append, chat_only=True)
        clock.advance(1)
        token = delivered[-1]['_conversation_turn']
        clock.advance(901)
        self.now = clock.now
        self.assertTrue(token.current())
        s.sanae_reply('reasoning_effort=max，解释二分查找', '11', group_id='22',
                     force=True, conversation_turn=token, include_usage_footer=False)
        s._with_reply_context.assert_not_called()
        self.assertFalse(self.calls)


if __name__ == '__main__':
    unittest.main()
