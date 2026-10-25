"""Only emitted prose bubble endings lose their Chinese full stop; offline."""
import contextlib
import os
import unittest
from unittest import mock

import model_display

with mock.patch.dict(os.environ, {'GEMINI_API_KEY': 'test-placeholder',
                                  'GPT6LUNA_API_KEY': 'test-placeholder'}):
    from tests.test_fusion_integration import FusionCase, bridge, s


class ReplyPeriodTests(unittest.TestCase):
    def test_only_final_period_changes_and_trailing_whitespace_stays(self):
        self.assertEqual(model_display.strip_reply_period('第一句。第二句。 \t\r\n'),
                         '第一句。第二句 \t\r\n')

    def test_other_punctuation_and_ellipses_stay(self):
        for text in ('这样？', '这样！', '这样……', '这样...', 'Version 1.2.'):
            with self.subTest(text=text):
                self.assertEqual(model_display.strip_reply_period(text), text)

    def test_closed_code_stays_and_following_prose_can_change(self):
        code = '```text\n代码中的句号。\n```'
        self.assertEqual(model_display.strip_reply_period(code), code)
        self.assertEqual(model_display.strip_reply_period(code + '\n最后一句。'),
                         code + '\n最后一句')
        self.assertEqual(model_display.strip_reply_period('`代码。`'), '`代码。`')
        self.assertEqual(model_display.strip_reply_period('`代码。`。'), '`代码。`')

    def test_unclosed_code_and_indented_code_stay(self):
        for text in ('说明。\n```text\n代码。', '~~~text\n代码。', '`未闭合代码。',
                     '    # 原样保留。 \n', '\t原样保留。'):
            with self.subTest(text=text):
                self.assertEqual(model_display.strip_reply_period(text), text)

    def test_mention_does_not_hide_indented_code(self):
        text = '[CQ:at,qq=2001] ' + '    # 原样保留。'
        self.assertEqual(model_display.strip_reply_period(text), text)
        self.assertEqual(model_display.strip_reply_period('[CQ:at,qq=2001] 正文。'),
                         '[CQ:at,qq=2001] 正文')

    def test_quoted_original_text_keeps_its_period(self):
        for text in ('> 引用原文。', '说明。\n> 引用原文。',
                     '[CQ:at,qq=2001] > 引用原文。'):
            with self.subTest(text=text):
                self.assertEqual(model_display.strip_reply_period(text), text)


class ReplyPeriodDeliveryTests(FusionCase):
    def setUp(self):
        super().setUp()
        self.gid = bridge.SOCIAL_LITE_GROUP
        self.prefix = '[CQ:at,qq=2001] '
        self.stack.enter_context(mock.patch.object(bridge.time, 'sleep'))
        self.stack.enter_context(mock.patch.object(bridge, '_expression_sent'))
        self.stack.enter_context(mock.patch.object(bridge, '_expression_context', return_value=''))
        self.stack.enter_context(mock.patch.object(bridge.FEEDBACK_STORE, 'style_override', return_value=''))
        self.stack.enter_context(mock.patch.object(bridge, '_auto_sticker',
            side_effect=lambda *args, **kwargs: contextlib.nullcontext(None)))

    def sent(self):
        return [call.args[0] for call in self.send.call_args_list]

    def test_social_trims_each_actual_bubble_and_keeps_internal_periods(self):
        body = '先喝水。\n慢慢来。内部也保留。'
        self.assertTrue(bridge._send_social_answer(self.event('你好'), '你好', body, body,
            self.gid, prefix=self.prefix, learn=True, stickers=False))
        self.assertEqual(self.sent(), [self.prefix + '先喝水', '慢慢来。内部也保留'])
        self.assertEqual(self.lessons.record_reply.call_args.args[1],
                         '先喝水\n慢慢来。内部也保留')

    def test_web_paragraphs_trim_only_their_new_bubble_endings(self):
        paragraphs = ['甲方依据。' * 55, '乙方依据。' * 55]
        body = '\n\n'.join(paragraphs)
        self.assertTrue(bridge._send_web_answer(self.event('查查资料'), body, body,
                                               self.gid, prefix=self.prefix))
        self.assertEqual(self.sent(), [self.prefix + paragraphs[0][:-1], paragraphs[1][:-1]])
        self.lessons.record_reply.assert_not_called()

    def test_normal_send_preserves_internal_periods_and_code(self):
        self.assertTrue(bridge._send_ai_text(self.prefix + '第一句。第二句。', self.gid))
        self.assertTrue(bridge._send_ai_text(self.prefix + '    # 原样保留。', self.gid))
        self.assertEqual(self.sent(), [self.prefix + '第一句。第二句',
                                     self.prefix + '    # 原样保留。'])

    def test_stream_display_trims_without_changing_raw_prefix_subtraction(self):
        raw = '已经核对。' * 105
        tail = '还有最后一点。'
        recorded = []

        def agent(*args, **kwargs):
            delivery = s._StreamDelivery(kwargs['stream_cb'])
            delivery.feed(raw)
            recorded.append(delivery.sent)
            usage = s._Usage()
            usage.model_answer = True
            usage.streamed_text = delivery.sent
            return raw + tail, usage, False

        with mock.patch.object(s, 'run_agent', side_effect=agent), \
                mock.patch.object(s, '_with_reply_context', side_effect=lambda text, **kwargs: text), \
                mock.patch.object(s, '_explicit_interaction_ok', return_value=True), \
                mock.patch.object(s, '_member_cooldown_ok', return_value=True):
            event = self.event(f'[CQ:at,qq={bridge.SANAE_QQ}] 请详细解释')
            event['self_id'] = bridge.SANAE_QQ
            self.handler._sanae_reply_thread(event, True)
        self.assertEqual(recorded, [raw])
        self.assertEqual(self.sent(), [self.prefix + raw[:-1], self.prefix + tail[:-1]])


if __name__ == '__main__':
    unittest.main()
