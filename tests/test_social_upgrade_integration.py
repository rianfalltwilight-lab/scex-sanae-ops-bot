"""Exercise real routing, receipt attribution and stale delivery with no network."""
import json
import tempfile
from pathlib import Path
from unittest import mock

from tests.test_fusion_integration import FusionCase, bridge, s
from tests.test_conversation_turns import Clock
from conversation_turns import ConversationTurns
from social_feedback import FeedbackStore

QUEUE = bridge._queue_sanae_reply
SEND = bridge.send_group_msg


class UpgradeIntegrationTests(FusionCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.turns = ConversationTurns((bridge.SOCIAL_LITE_GROUP,), clock=self.clock,
                                       timer_factory=self.clock.timer)
        self.addCleanup(self.turns.close)
        self.stack.enter_context(mock.patch.object(bridge, 'CONVERSATION_TURNS', self.turns))
        self.expression = mock.Mock()
        self.expression.observe.return_value = False
        self.expression.reserve.return_value = ('', None)
        self.stack.enter_context(mock.patch.object(bridge, 'EXPRESSION_MEMORY', self.expression))
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.feedback = FeedbackStore(Path(self.tmp)/'feedback.json', group_id=bridge.SOCIAL_LITE_GROUP)
        self.stack.enter_context(mock.patch.object(bridge, 'FEEDBACK_STORE', self.feedback))

    def at(self, text, mid='1', uid='2001'):
        return self.event('[CQ:at,qq=1002] '+text, mid=mid, uid=uid)

    def merged(self, event):
        events = []
        self.assertTrue(self.turns.submit(event, events.append, chat_only=True))
        self.clock.advance(1)
        return events[-1]

    def test_at_burst_is_one_worker_and_retains_source_order(self):
        callback = mock.Mock()
        self.assertTrue(QUEUE(callback, self.at('今天吃面', '1')))
        self.clock.advance(.6)
        self.assertTrue(QUEUE(callback, self.at('加个蛋', '2')))
        self.ai_worker.assert_not_called()
        self.clock.advance(1)
        self.ai_worker.assert_called_once()
        merged = self.ai_worker.call_args.kwargs['args'][0]
        self.assertEqual(merged['_conversation_source_ids'], ('1', '2'))
        self.assertLess(merged['raw_message'].index('吃面'), merged['raw_message'].index('加个蛋'))
        self.assertEqual(bridge._ai_reply_trigger(merged), 'mention-self')

    def test_different_people_get_separate_workers(self):
        for uid in ('2001', '2002'):
            QUEUE(mock.Mock(), self.at('今天吃面', mid=uid, uid=uid))
        self.clock.advance(1)
        self.assertEqual(self.ai_worker.call_count, 2)

    def test_qa_tools_media_and_other_groups_stay_immediate(self):
        events = [self.at(text, str(i)) for i, text in enumerate(
            ('什么是玻色子', '用 Python 算 2 的100次方', '搜索天气', '重启服务器'), 1)]
        image = self.at('看看这个', '5')
        image['message'].append({'type': 'image', 'data': {'url': 'https://invalid.test/a.png'}})
        events.append(image)
        other = self.at('今天吃面', '6'); other['group_id'] = 2104
        events.append(other)
        for event in events:
            QUEUE(mock.Mock(), event)
        self.assertEqual(self.ai_worker.call_count, len(events))
        self.assertFalse(self.clock.timers)
        for call in self.ai_worker.call_args_list:
            self.assertNotIn('_conversation_turn', call.kwargs['args'][0])

    def test_generating_reply_superseded_by_correction_is_not_sent(self):
        old = self.merged(self.at('今天吃面', '1'))
        def model(*args, **kwargs):
            self.turns.submit(self.event('不对，我说错了，今天吃饭', mid='2'),
                              mock.Mock(), chat_only=True, directed=False)
            return '旧的面条回答'
        with mock.patch.object(s, 'sanae_reply', side_effect=model):
            self.handler._sanae_reply_thread(old)
        self.send.assert_not_called()
        self.assertFalse(old['_conversation_turn'].current())

    def test_other_person_does_not_suppress_current_reply(self):
        old = self.merged(self.at('今天吃面', '1'))
        def model(*args, **kwargs):
            self.turns.submit(self.at('今天吃饭', '2', '2002'), mock.Mock(), chat_only=True)
            return '给你的面加个蛋。'
        with mock.patch.object(s, 'sanae_reply', side_effect=model):
            self.handler._sanae_reply_thread(old)
        self.send.assert_called_once()
        self.assertIn('面加个蛋', self.send.call_args.args[0])

    def test_expression_read_failure_does_not_break_chat(self):
        self.expression.reserve.side_effect = OSError('fixture read failure')
        with mock.patch.object(s, 'sanae_reply', return_value='我接住了。'):
            self.handler._sanae_reply_thread(self.at('你好呀'))
        self.assertIn('我接住了', self.send.call_args.args[0])

    def test_real_onebot_receipt_then_quoted_feedback(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            'status':'ok','retcode':0,'data':{'message_id':987}}).encode()
        group = bridge.REPLY_GROUP.set(bridge.SOCIAL_LITE_GROUP)
        person = bridge.REPLY_PERSON.set('2001')
        try:
            with mock.patch.object(bridge.urllib.request, 'urlopen', return_value=response):
                self.assertTrue(SEND('fixture response'))
        finally:
            bridge.REPLY_PERSON.reset(person)
            bridge.REPLY_GROUP.reset(group)
        self.assertIsNotNone(self.feedback.resolve_target('987'))
        event = self.at('!反馈 踩 这次太刻薄', '2')
        event['raw_message'] = '[CQ:reply,id=987]'+event['raw_message']
        event['message'].insert(0, {'type':'reply','data':{'id':'987'}})
        self.handler._process_event(event)
        self.dispatch_workers()
        self.assertIn('已关联', self.send.call_args.args[0])
        self.assertEqual(self.feedback._items[-1]['messageId'], '987')
        self.assertIn('收住损话', self.feedback.strategy_hint('2001'))
        self.assertNotIn('收住损话', self.feedback.strategy_hint('2002'))
        self.queue.assert_not_called()

    def test_receipt_storage_failure_never_reports_successful_send_as_failed(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"status":"ok","retcode":0,"data":{"message_id":988}}'
        group = bridge.REPLY_GROUP.set(bridge.SOCIAL_LITE_GROUP)
        person = bridge.REPLY_PERSON.set('2001')
        try:
            with mock.patch.object(bridge.urllib.request, 'urlopen', return_value=response), \
                    mock.patch.object(self.feedback, '_save', side_effect=OSError('fixture')):
                self.assertTrue(SEND('fixture response'))
        finally:
            bridge.REPLY_PERSON.reset(person)
            bridge.REPLY_GROUP.reset(group)

    def test_temporary_request_is_current_person_only_not_permanent_memory(self):
        event = self.at('今天别损我', '3')
        self.handler._process_event(event)
        self.assertIn('短期明确要求', self.feedback.strategy_hint('2001'))
        self.assertNotIn('短期明确要求', self.feedback.strategy_hint('2002'))
        self.assertFalse(self.feedback._items)
        with mock.patch.object(s, 'sanae_reply', return_value='我知道了。') as reply:
            self.handler._sanae_reply_thread(self.at('我这次又忘带钥匙了', '4'))
        self.assertEqual(reply.call_args.kwargs['social_style_override'], 'gentle')
        self.expression.reserve.assert_not_called()

    def test_social_name_correction_retains_social_callback(self):
        first = self.event('早苗，今天吃面', mid='1')
        with mock.patch.object(bridge.SOCIAL_LITE, 'record_incoming'), \
                mock.patch.object(bridge.SOCIAL_LITE, 'should_reply', return_value=(True, 'direct')), \
                mock.patch.object(bridge.SOCIAL_LITE, '_context', return_value='当前话题'):
            self.assertTrue(bridge._schedule_social_reply(self.handler._social_reply_thread, first)[0])
            self.clock.advance(1)
            old = self.ai_worker.call_args.kwargs['args'][0]
            wrong_callback = mock.Mock()
            self.assertTrue(self.turns.submit(self.event('不对，我说错了，今天吃饭', mid='2'),
                wrong_callback, chat_only=True, directed=False))
            self.clock.advance(1)
        self.assertFalse(old['_conversation_turn'].current())
        wrong_callback.assert_not_called()
        self.assertEqual(self.ai_worker.call_args.args[0], self.handler._social_reply_thread)
        self.assertIn('今天吃饭', self.ai_worker.call_args.kwargs['args'][0]['raw_message'])

    def test_ambient_keeps_original_cooldown_path_without_token(self):
        with mock.patch.object(bridge.SOCIAL_LITE, 'record_incoming'), \
                mock.patch.object(bridge.SOCIAL_LITE, 'should_reply', return_value=(True, 'question')), \
                mock.patch.object(bridge.SOCIAL_LITE, 'is_explicit_interaction', return_value=False), \
                mock.patch.object(bridge.SOCIAL_LITE, '_context', return_value='当前话题'):
            bridge._schedule_social_reply(self.handler._social_reply_thread, self.event('今天吃面', mid='8'))
        self.assertNotIn('_conversation_turn', self.ai_worker.call_args.kwargs['args'][0])

    def test_forget_and_recall_reach_expression_and_turn_sources(self):
        old = self.merged(self.at('今天吃面', '1'))
        self.store.command.return_value = '已忘记你的全部。'
        self.handler._process_event(self.event('!忘记 全部', mid='2'))
        self.expression.forget.assert_called_once_with(bridge.SOCIAL_LITE_GROUP, '2001')
        self.assertFalse(old['_conversation_turn'].current())
        self.handler._process_event({'post_type':'notice','notice_type':'group_recall',
            'group_id':bridge.SOCIAL_LITE_GROUP,'user_id':'2001','message_id':'3'})
        self.expression.retract.assert_called_once_with(bridge.SOCIAL_LITE_GROUP, '3')

    def test_new_expression_batch_uses_background_thread_only(self):
        batch = {'messages': [], 'prompt': 'fixture', 'group_id': bridge.SOCIAL_LITE_GROUP}
        self.expression.observe.return_value = True
        self.expression.prepare_batch.return_value = batch
        with mock.patch.object(bridge.threading, 'Thread') as thread:
            self.handler._process_event(self.event('今天天气挺好', mid='7'))
        self.assertEqual(thread.call_args.kwargs['target'], bridge._learn_expressions)
        self.assertEqual(thread.call_args.kwargs['args'], (batch,))
        self.ai_worker.assert_not_called()

    def test_expression_learning_off_neither_observes_nor_injects(self):
        self.expression.observe.return_value = True
        self.expression.reserve.return_value = ('〖学到的表达〗fixture', 'ticket')
        with mock.patch.object(bridge, 'EXPRESSION_LEARNING_ENABLED', False), \
                mock.patch.object(bridge.threading, 'Thread') as thread:
            self.handler._process_event(self.event('今天天气挺好', mid='8'))
            self.assertEqual('', bridge._expression_context(
                bridge.SOCIAL_LITE_GROUP, '2001', '今天天气挺好'))
        self.expression.observe.assert_not_called()
        self.expression.prepare_batch.assert_not_called()
        self.expression.reserve.assert_not_called()
        self.assertNotIn(bridge._learn_expressions,
                         [call.kwargs.get('target') for call in thread.call_args_list])
