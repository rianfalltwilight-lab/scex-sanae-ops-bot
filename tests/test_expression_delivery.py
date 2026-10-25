"""Real expression reservations through the QQ reply callbacks, without network."""
import json
import tempfile
from pathlib import Path
from unittest import mock

from tests.test_fusion_integration import FusionCase, bridge, s
from expression_memory import ExpressionMemory


class ExpressionDeliveryTests(FusionCase):
    def setUp(self):
        super().setUp()
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.now = 1_800_000_000
        self.gid = bridge.SOCIAL_LITE_GROUP
        self.mem = ExpressionMemory(Path(self.tmp)/'expressions.json', (self.gid,),
                                    clock=lambda: self.now)
        self.stack.enter_context(mock.patch.object(bridge, 'EXPRESSION_MEMORY', self.mem))
        self.stack.enter_context(mock.patch.object(bridge.FEEDBACK_STORE, 'style_override', return_value=''))
        self.stack.enter_context(mock.patch.object(bridge.time, 'sleep'))
        self.stack.enter_context(mock.patch.object(s, '_explicit_interaction_ok', return_value=True))
        self.stack.enter_context(mock.patch.object(s, '_member_cooldown_ok', return_value=True))
        for mid in range(1, 25):
            self.assertTrue(self.mem.observe({
                'post_type':'message', 'message_type':'group', 'group_id':self.gid,
                'user_id':110 if mid % 2 else 120, 'message_id':mid,
                'time':self.now, 'raw_message':'早苗你又嘴硬了' if mid % 2 else '这次总算被你发现了',
            }))
        batch = self.mem.prepare_batch(self.gid)
        self.assertTrue(self.mem.complete_batch(batch, {'expressions':[{
            'situation':'playful_tease', 'technique':'playful_reversal',
            'source_ids':[m['source_id'] for m in batch['messages'][:2]],
        }]}))

    def at(self):
        return self.event('[CQ:at,qq=1002] 你是不是又得意上了？')

    def available(self):
        return bool(self.mem.context(self.gid, '2001', '你是不是又得意上了？'))

    def run_reply(self, reply='也就一点，刚好被你看见。', *, social=False, side_effect=None):
        def generate(*args, **kwargs):
            kwargs['reply_metadata']['model_answer'] = True
            if isinstance(side_effect, BaseException):
                raise side_effect
            return side_effect(*args, **kwargs) if side_effect else reply
        with mock.patch.object(s, 'sanae_reply', side_effect=generate) as model:
            if social:
                self.handler._social_reply_thread(self.at(), '合成聊天上下文')
            else:
                self.handler._sanae_reply_thread(self.at())
        return model

    def test_at_success_starts_cooldown_at_delivery(self):
        def model(*args, **kwargs):
            self.assertIn('善意调侃', kwargs['context_reference'])
            self.now += 40
            return '也就一点，刚好被你看见。'
        self.run_reply(side_effect=model)
        self.send.assert_called_once()
        self.assertFalse(self.available())
        self.now += 1799
        self.assertFalse(self.available())
        self.now += 1
        self.assertTrue(self.available())
        self.assertIsNone(bridge.REPLY_EXPRESSION.get())

    def test_failed_at_send_does_not_consume_cooldown(self):
        self.send.return_value = False
        self.run_reply()
        self.send.assert_called_once()
        self.assertTrue(self.available())

    def test_empty_at_reply_releases_reservation(self):
        self.run_reply(reply=None)
        self.send.assert_not_called()
        self.assertTrue(self.available())

    def test_silent_social_reply_releases_reservation(self):
        self.run_reply(reply='[SILENT]', social=True)
        self.send.assert_not_called()
        self.assertTrue(self.available())

    def test_model_error_notice_does_not_count_as_expression_delivery(self):
        self.run_reply(side_effect=RuntimeError('synthetic fixture'))
        self.send.assert_called_once()
        self.assertTrue(self.available())

    def test_social_error_notice_does_not_consume_expression(self):
        self.run_reply(social=True, side_effect=RuntimeError('synthetic fixture'))
        self.send.assert_called_once()
        self.assertTrue(self.available())

    def test_partial_social_reply_counts_first_visible_text(self):
        self.send.side_effect = [True, False]
        self.run_reply('也就一点。\n刚好被你看见。', social=True)
        self.assertEqual(self.send.call_count, 2)
        self.assertFalse(self.available())

    def test_social_first_send_failure_releases(self):
        self.send.return_value = False
        self.run_reply('也就一点。\n刚好被你看见。', social=True)
        self.send.assert_called_once()
        self.assertTrue(self.available())

    def test_superseded_reply_does_not_consume_cooldown(self):
        event = self.at()
        turn = mock.Mock()
        turn.current.return_value = True
        event['_conversation_turn'] = turn
        def model(*args, **kwargs):
            turn.current.return_value = False
            return '未发送的旧回答'
        with mock.patch.object(s, 'sanae_reply', side_effect=model):
            self.handler._sanae_reply_thread(event)
        self.send.assert_not_called()
        self.assertTrue(self.available())
        turn.finish.assert_called_once()

    def test_journal_failure_after_send_never_resends(self):
        with mock.patch.object(self.mem, '_save', side_effect=OSError('synthetic write failure')):
            self.run_reply()
        self.send.assert_called_once()
        self.assertFalse(self.available())
        self.assertIsNone(bridge.REPLY_EXPRESSION.get())

    def test_real_model_api_error_notice_does_not_consume_expression(self):
        with mock.patch.object(s, '_text_post', side_effect=RuntimeError('synthetic API failure')):
            self.handler._social_reply_thread(self.at(), '合成聊天上下文')
        self.send.assert_called_once()
        self.assertTrue(self.available())

    def test_real_model_result_marks_reply_as_generated(self):
        response = {'choices':[{'message':{'content':'这点得意还得让你批准呀。'}}], 'usage':{}}
        with mock.patch.object(s, '_text_post', return_value=response):
            self.handler._social_reply_thread(self.at(), '合成聊天上下文')
        self.send.assert_called_once()
        self.assertFalse(self.available())

    def test_temporary_gentle_request_does_not_reserve_expression(self):
        with mock.patch.object(bridge.FEEDBACK_STORE, 'style_override', return_value='gentle'), \
                mock.patch.object(self.mem, 'reserve', wraps=self.mem.reserve) as reserve:
            self.run_reply()
        reserve.assert_not_called()
        self.assertTrue(self.available())

    def test_folded_reply_uses_business_receipt_boolean(self):
        for receipt, expected in [({'status':'ok','retcode':0}, True),
                                  ({'status':'failed','retcode':100}, False)]:
            with self.subTest(receipt=receipt):
                response = mock.MagicMock()
                response.__enter__.return_value.read.return_value = json.dumps(receipt).encode()
                _, ticket = self.mem.reserve(self.gid, '2001', '你是不是又得意上了？')
                token = bridge.REPLY_EXPRESSION.set(ticket)
                try:
                    with mock.patch.object(bridge.urllib.request, 'urlopen', return_value=response):
                        self.assertIs(bridge._send_ai_text('合成文本' * 1000, self.gid), expected)
                    self.assertEqual(self.available(), not expected)
                finally:
                    self.mem.release(ticket)
                    bridge.REPLY_EXPRESSION.reset(token)
                    self.now += 1800
