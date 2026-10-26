"""Passive observation must not become a reply or long-term memory."""
from unittest import mock

from tests.test_fusion_integration import FusionCase, bridge, s


class SilentVisionBridgeTests(FusionCase):
    def setUp(self):
        super().setUp()
        self.vision = mock.Mock()
        self.vision.context.return_value = ''
        self.stack.enter_context(mock.patch.object(bridge, 'SILENT_VISION', self.vision))
        self.stack.enter_context(mock.patch.object(bridge, 'SILENT_VISION_ENABLED', True))
        self.stack.enter_context(mock.patch.object(bridge.FEEDBACK_STORE, 'style_override', return_value=''))

    def photo(self, text='', **kw):
        event = self.event(text, **kw)
        event['raw_message'] += '[CQ:image,file=fixture.jpg]'
        event['message'].append({'type': 'image', 'data': {'file': 'fixture.jpg'}})
        return event

    def test_unmentioned_photo_enqueues_without_send_or_reply_worker(self):
        event = self.photo()
        with mock.patch.object(bridge, 'SANAE_MENTION_ONLY', False), \
             mock.patch.object(bridge, 'SOCIAL_LITE_ENABLED', True), \
             mock.patch.object(bridge.SOCIAL_LITE, 'record_incoming'):
            self.handler._process_event(event)
        self.vision.observe.assert_called_once_with(event)
        self.ai_worker.assert_not_called()
        self.queue.assert_not_called()
        self.send.assert_not_called()
        self.worker.assert_not_called()
        self.assertNotIn('观察', str(self.archive.call_args))

    def test_current_mentioned_photo_keeps_single_direct_vision_route(self):
        event = self.photo('[CQ:at,qq=1002] 看看这个')
        self.handler._process_event(event)
        self.vision.observe.assert_not_called()
        self.queue.assert_called_once()

    def test_text_duplicate_gate_does_not_drop_a_second_distinct_photo(self):
        first = self.photo(mid='501')
        second = self.photo(mid='502')
        second['message'][-1]['data']['file'] = 'second.jpg'
        second['raw_message'] = '[CQ:image,file=second.jpg]'
        with mock.patch.object(bridge, '_is_duplicate', return_value=True):
            self.handler._process_event(first)
            self.handler._process_event(second)
        self.assertEqual(self.vision.observe.call_args_list, [mock.call(first), mock.call(second)])
        self.send.assert_not_called()
        self.ai_worker.assert_not_called()

    def test_named_photo_uses_existing_image_capable_route(self):
        event = self.photo('早苗看看这个')
        with mock.patch.object(bridge, 'SANAE_NAME_CALL_ENABLED', True), \
             mock.patch.object(bridge, 'SOCIAL_LITE_ENABLED', True), \
             mock.patch.object(bridge, 'SANAE_MENTION_ONLY', False), \
             mock.patch.object(bridge, '_schedule_social_reply') as social:
            self.handler._process_event(event)
        self.vision.observe.assert_not_called()
        self.queue.assert_called_once()
        social.assert_not_called()

    def test_other_groups_self_and_commands_do_not_observe(self):
        with mock.patch.object(bridge, 'LLBOT_GROUPS', frozenset({bridge.SOCIAL_LITE_GROUP, 12345})):
            for event in (self.photo(gid=12345), self.photo(uid='1004'), self.photo('!记忆')):
                self.handler._process_event(event)
        self.vision.observe.assert_not_called()

    def test_disabled_switch_blocks_observation_and_context(self):
        with mock.patch.object(bridge, 'SILENT_VISION_ENABLED', False):
            self.handler._process_event(self.photo())
            self.assertEqual(bridge._silent_image_context(self.event('肉'), '肉'), '')
        self.vision.observe.assert_not_called()
        self.vision.context.assert_not_called()

    def test_enqueue_failure_stays_silent_and_does_not_break_text_intake(self):
        self.vision.observe.side_effect = RuntimeError('fixture')
        event = self.photo('炖了好久的肉')
        self.handler._process_event(event)
        self.store.observe.assert_called_once_with(event)
        self.send.assert_not_called()
        self.ai_worker.assert_not_called()
        self.queue.assert_not_called()

    def test_followup_at_and_ambient_receive_reference_without_rewriting_question(self):
        self.vision.context.return_value = '【低可信图片观察】案板上有肉，不能确定种类。'
        event = self.event('[CQ:at,qq=1002] 炖了好久的肉')
        with mock.patch.object(s, 'sanae_reply', return_value=None) as model:
            self.handler._sanae_reply_thread(event)
            explicit = model.call_args
            self.handler._social_reply_thread(event, '先前聊过武器配置；现在炖了好久的肉')
            ambient = model.call_args
        for call in (explicit, ambient):
            self.assertEqual(call.args[0], event['raw_message'])
            self.assertIn('案板上有肉', call.kwargs['context_reference'])
        self.send.assert_not_called()
        self.store.observe.assert_not_called()

    def test_context_failure_does_not_cancel_normal_reply_generation(self):
        self.vision.context.side_effect = RuntimeError('fixture')
        with mock.patch.object(s, 'sanae_reply', return_value=None) as model:
            self.handler._sanae_reply_thread(self.event('[CQ:at,qq=1002] 晚上好'))
        model.assert_called_once()
        self.send.assert_not_called()

    def test_forget_and_recall_clear_visual_sources(self):
        self.store.command.return_value = '已忘记。'
        event = self.event('!忘记 全部')
        self.handler._process_event(event)
        self.vision.forget.assert_called_once_with(event['group_id'], event['user_id'])
        self.handler._process_event({'post_type': 'notice', 'notice_type': 'group_recall',
            'group_id': event['group_id'], 'user_id': event['user_id'], 'message_id': '501'})
        self.vision.retract.assert_called_once_with(event['group_id'], '501')
