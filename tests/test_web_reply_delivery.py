"""Offline delivery of sourced web answers; no model or QQ requests."""
import contextlib
import re
from unittest import mock

from tests.test_fusion_integration import FusionCase, bridge, s


class WebReplyDeliveryTests(FusionCase):
    def setUp(self):
        super().setUp()
        self.sleep = self.stack.enter_context(mock.patch.object(bridge.time, 'sleep'))
        self.stack.enter_context(mock.patch.object(bridge.FEEDBACK_STORE, 'style_override', return_value=''))
        self.sticker = self.stack.enter_context(mock.patch.object(bridge, '_auto_sticker'))
        self.body = '\n\n'.join([
            '这次主要修了连接恢复。' + '重复加载原有网络时，现在会复用缓存。' * 12,
            '1.21.1 对应 2.4.0，仍需要 NeoForge。' + '旧配置保持可读，不需要重建世界。' * 12,
            '原文：https://example.com/releases/v2.4.0?mc=1.21.1\n只有项目自测，没有整包对比数据。'])
        self.suffix = '\n和你吹牛逼用了 2,200 tok，约 ¥0.0034，饭钱结一下'

    def event_for_web(self):
        event = self.event(f'[CQ:at,qq={bridge.SANAE_QQ}] 查查新版改了什么')
        event['self_id'] = bridge.SANAE_QQ
        return event

    def test_bridge_routes_web_to_semantic_bubbles_without_social_learning(self):
        event = self.event_for_web()
        def model(*args, **kwargs):
            kwargs['reply_metadata'].update(model_answer=True, social_reply=False,
                web_reply=True, reply_body=self.body)
            return self.body + self.suffix
        with mock.patch.object(s, 'sanae_reply', side_effect=model):
            self.handler._sanae_reply_thread(event)
        chunks = [call.args[0] for call in self.send.call_args_list]
        self.assertGreater(len(chunks), 1)
        self.assertLessEqual(len(chunks), 8)
        self.assertTrue(chunks[0].startswith('[CQ:at,qq=2001] '))
        self.assertNotIn('[AI]', ''.join(chunks))
        self.assertEqual(sum('[CQ:at,' in part for part in chunks), 1)
        self.assertEqual(sum(self.suffix in part for part in chunks), 1)
        self.assertTrue(chunks[-1].endswith(self.suffix))
        reconstructed = chunks[0].split('] ', 1)[1] + ''.join(chunks[1:])
        parts = bridge.split_informative_reply(self.body)
        expected = ''.join(part.removesuffix('。') for part in parts[:-1]) + parts[-1] + self.suffix
        self.assertEqual(re.sub(r'\s+', '', reconstructed), re.sub(r'\s+', '', expected))
        self.sticker.assert_not_called()
        self.lessons.record_reply.assert_not_called()
        self.assertEqual(self.sleep.call_count, len(chunks)-1)

    def test_failed_or_uncertain_delivery_stops_without_replaying(self):
        for outcome in (False, TimeoutError('synthetic transport uncertainty')):
            with self.subTest(outcome=outcome):
                self.send.reset_mock()
                self.sleep.reset_mock()
                self.send.side_effect = [True, outcome]
                self.assertFalse(bridge._send_web_answer(self.event_for_web(), self.body,
                    self.body+self.suffix, bridge.SOCIAL_LITE_GROUP))
                self.assertEqual(self.send.call_count, 2)
                self.assertEqual(self.sleep.call_count, 1)

    def test_cancel_between_bubbles_stops_unsent_tail(self):
        event = self.event_for_web()
        turn = mock.Mock()
        turn.current.side_effect = [True, True, False]
        event['_conversation_turn'] = turn
        self.assertFalse(bridge._send_web_answer(event, self.body, self.body,
                                               bridge.SOCIAL_LITE_GROUP))
        self.send.assert_called_once()

    def test_eight_parts_have_footer_on_eighth_without_ninth_message(self):
        parts = ['段落' + str(i) for i in range(8)]
        body = '\n\n'.join(parts)
        with mock.patch.object(bridge, 'split_informative_reply', return_value=parts):
            self.assertTrue(bridge._send_web_answer(self.event_for_web(), body,
                body+self.suffix, bridge.SOCIAL_LITE_GROUP))
        self.assertEqual(self.send.call_count, 8)
        self.assertTrue(self.send.call_args.args[0].endswith(self.suffix))

    def test_oversize_atom_or_footer_uses_complete_existing_fold_path(self):
        for body, suffix in [('```\n'+'x'*3600+'\n```',''), ('x'*3490,self.suffix)]:
            with self.subTest(length=len(body)), mock.patch.object(bridge, '_send_ai_text', return_value=True) as fallback:
                self.assertTrue(bridge._send_web_answer(self.event_for_web(), body,
                    body+suffix, bridge.SOCIAL_LITE_GROUP, prefix='[CQ:at,qq=2001] '))
                fallback.assert_called_once_with('[CQ:at,qq=2001] '+body+suffix, bridge.SOCIAL_LITE_GROUP)
        self.send.assert_not_called()

    def test_metadata_mismatch_preserves_full_reply(self):
        with mock.patch.object(bridge, '_send_ai_text', return_value=True) as fallback:
            bridge._send_web_answer(self.event_for_web(), '旧正文', '实际完整正文', bridge.SOCIAL_LITE_GROUP)
        fallback.assert_called_once_with('实际完整正文', bridge.SOCIAL_LITE_GROUP)

    def test_non_web_tool_reply_remains_original_route(self):
        def model(*args, **kwargs):
            kwargs['reply_metadata'].update(model_answer=True, social_reply=False,
                web_reply=False, reply_body=self.body)
            return self.body+self.suffix
        with mock.patch.object(s, 'sanae_reply', side_effect=model), mock.patch.object(
                bridge, '_send_ai_text', return_value=True) as send_ai, mock.patch.object(
                bridge, '_auto_sticker', side_effect=lambda *args, **kwargs: contextlib.nullcontext(None)):
            self.handler._sanae_reply_thread(self.event_for_web())
        send_ai.assert_called_once()
        self.send.assert_not_called()
