"""Real reply routes and delivery receipts, with model/network calls replaced."""
import contextlib
import json
import tempfile
from pathlib import Path
from unittest import mock

from tests.test_fusion_integration import FusionCase, bridge, s
from social_lite import split_social_reply
from reply_lessons import ReplyLessons

REAL_SEND = bridge.send_group_msg

class LivelinessDeliveryTests(FusionCase):
    def setUp(self):
        super().setUp()
        self.stack.enter_context(mock.patch.object(bridge.time, 'sleep'))
        self.stack.enter_context(mock.patch.object(bridge.FEEDBACK_STORE, 'style_override', return_value=''))
        self.outgoing = self.stack.enter_context(mock.patch.object(bridge.SOCIAL_LITE, 'record_outgoing'))
        self.stack.enter_context(mock.patch.object(s, '_with_reply_context', side_effect=lambda raw, **kw: raw))
        self.stack.enter_context(mock.patch.object(s, '_explicit_interaction_ok', return_value=True))
        self.stack.enter_context(mock.patch.object(s, '_member_cooldown_ok', return_value=True))
        self.stack.enter_context(mock.patch.object(s.SHARED_KNOWLEDGE, 'context', return_value=''))
        for key in ('_HISTORY', '_CHAT_HISTORY', '_HISTORY_GENERATION'):
            self.stack.enter_context(mock.patch.object(s, key, {}))
        self.body = '兜了一圈，第一版赢了。\n这评审挺会给人安排旅游。'

    def reply(self, question='第三版改完了，最后还是用了第一版。', *, social=False):
        event = self.event('[CQ:at,qq=1002] ' + question)
        with mock.patch.object(s, '_text_post', return_value={
                'choices': [{'message': {'content': self.body}}], 'usage': {}}):
            if social:
                self.handler._social_reply_thread(event, '仅有当前合成消息')
            else:
                self.handler._sanae_reply_thread(event)
        return event

    def test_at_and_ambient_share_short_cadence_and_only_body_is_learned(self):
        for ambient in (False, True):
            with self.subTest(ambient=ambient):
                self.send.reset_mock()
                self.lessons.record_reply.reset_mock()
                self.reply(social=ambient)
                self.assertEqual(self.send.call_count, 2)
                lines = [c.args[0] for c in self.send.call_args_list]
                self.assertTrue(lines[0].endswith(self.body.splitlines()[0].rstrip("。")))
                self.assertTrue(lines[1].startswith(self.body.splitlines()[1].rstrip("。")))
                self.assertEqual(self.lessons.record_reply.call_args.args[1], "\n".join(line.rstrip("。") for line in self.body.splitlines()))
                self.assertEqual(self.lessons.record_reply.call_args.kwargs['expected_generation'], 0)
                self.assertNotIn('用量', self.lessons.record_reply.call_args.args[1])

    def test_explicit_high_is_not_split_or_learned(self):
        self.reply('reasoning_effort=high，请解释这个算法')
        self.send.assert_called_once()
        self.assertIn(self.body.rstrip("。"), self.send.call_args.args[0])
        self.lessons.record_reply.assert_not_called()
        self.lessons.context.assert_not_called()

    def test_tool_result_is_not_split_or_learned(self):
        event = self.event('[CQ:at,qq=1002] 当前世界多少天了')
        def answer(*a, **kw):
            kw['reply_metadata'].update(model_answer=True, social_reply=False, reply_body=self.body)
            return self.body
        with mock.patch.object(s, 'sanae_reply', side_effect=answer):
            self.handler._sanae_reply_thread(event)
        self.send.assert_called_once()
        self.lessons.record_reply.assert_not_called()

    def test_failed_first_or_second_bubble_never_learns_or_retries(self):
        for responses, count in (([False], 1), ([True, False], 2)):
            with self.subTest(responses=responses):
                self.send.reset_mock()
                self.send.side_effect = responses
                self.reply()
                self.assertEqual(self.send.call_count, count)
                self.lessons.record_reply.assert_not_called()

    def test_replacement_between_bubbles_stops_unsent_tail(self):
        event = self.event('当前消息')
        turn = mock.Mock()
        turn.current.side_effect = [True, False]
        event['_conversation_turn'] = turn
        bridge._send_social_answer(event, '当前消息', self.body, self.body,
                                  event['group_id'], learn=True)
        self.send.assert_called_once()
        self.lessons.record_reply.assert_not_called()

    def test_one_sticker_is_separate_within_three_messages(self):
        sticker = {'image': '[CQ:image,file=synthetic]', 'delivered': False}
        with mock.patch.object(bridge, '_auto_sticker', side_effect=lambda *a, **kw: contextlib.nullcontext(sticker)):
            self.reply(social=True)
        self.assertEqual(self.send.call_count, 3)
        self.assertNotIn('CQ:image', self.send.call_args_list[0].args[0])
        self.assertEqual(self.send.call_args_list[-1].args[0].count('CQ:image'), 1)
        self.assertTrue(sticker['delivered'])

    def test_sticker_failure_preserves_all_text_and_successful_learning(self):
        self.body = '这一版行。\n颜色也顺眼了。\n字号刚好。\n间距也够。\n这次就定了。\n先去歇会儿。\n别熬太晚。\n也别又改回去。'
        sticker = {'image': '[CQ:image,file=synthetic]', 'delivered': False}
        self.send.side_effect = [True] * 7 + [False]
        with mock.patch.object(bridge, '_auto_sticker', side_effect=lambda *a, **kw: contextlib.nullcontext(sticker)):
            self.reply(social=True)
        self.assertEqual(self.send.call_count, 8)
        self.assertIn('别熬太晚。\n也别又改回去', self.send.call_args_list[6].args[0])
        self.assertEqual(self.lessons.record_reply.call_args.args[1], "\n".join(line.rstrip("。") for line in self.body.splitlines()[:6]) + "\n别熬太晚。\n也别又改回去")
        self.outgoing.assert_called_once()
        self.assertFalse(sticker['delivered'])

    def test_at_and_ambient_allow_eight_short_text_bubbles(self):
        self.body = '\n'.join(['这是第' + str(i) + '句。' for i in range(8)])
        for ambient in (False, True):
            with self.subTest(ambient=ambient):
                self.send.reset_mock()
                self.reply(social=ambient)
                self.assertEqual(self.send.call_count, 8)
                self.assertEqual(self.lessons.record_reply.call_args.args[1], "\n".join(line.rstrip("。") for line in self.body.splitlines()))

    def test_forget_or_recall_during_final_receipt_cannot_resurrect_outgoing(self):
        self.body = '这一版行。'
        for action in ('forget', 'recall'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temp:
                store = ReplyLessons(Path(temp) / 'lessons.json',
                    allowed_group_ids=[bridge.SOCIAL_LITE_GROUP])
                self.outgoing.reset_mock()
                def send(_message, **kw):
                    if action == 'forget':
                        store.forget(bridge.SOCIAL_LITE_GROUP, '2001')
                    else:
                        store.retract(bridge.SOCIAL_LITE_GROUP, '501')
                    return True
                self.send.side_effect = send
                with mock.patch.object(bridge, 'REPLY_LESSONS', store):
                    self.reply(social=True)
                self.outgoing.assert_not_called()

    def test_at_cq_is_removed_before_lesson_lookup(self):
        self.reply('小猫今天又来蹭饭了')
        self.assertEqual(self.lessons.context.call_args.args[2], '小猫今天又来蹭饭了')

    def test_journal_error_after_delivery_does_not_send_error_or_retry(self):
        self.lessons.record_reply.side_effect = OSError('synthetic disk failure')
        self.reply(social=True)
        self.assertEqual(self.send.call_count, 2)

    def test_gentle_override_requests_only_safe_listening_context(self):
        with mock.patch.object(bridge.FEEDBACK_STORE, 'style_override', return_value='gentle'):
            self.reply()
        self.assertTrue(self.lessons.context.call_args.kwargs['gentle'])

    def test_forget_and_recall_reach_lessons(self):
        event = self.event('!忘记 全部')
        self.store.command.return_value = '已忘记'
        self.handler._process_event(event)
        self.lessons.forget.assert_called_once_with(event['group_id'], event['user_id'])
        recall = dict(post_type='notice', notice_type='group_recall', group_id=event['group_id'],
                      user_id=event['user_id'], message_id='501')
        self.handler._process_event(recall)
        self.lessons.retract.assert_called_once_with(event['group_id'], '501')

    def test_real_receipt_ids_are_collected_without_changing_boolean_send_contract(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {'status':'ok', 'retcode':0, 'data':{'message_id':9001}}).encode()
        ids = []
        tokens = [(bridge.REPLY_MESSAGE_IDS, bridge.REPLY_MESSAGE_IDS.set(ids)),
                  (bridge.REPLY_PERSON, bridge.REPLY_PERSON.set('2001')),
                  (bridge.REPLY_GROUP, bridge.REPLY_GROUP.set(bridge.SOCIAL_LITE_GROUP))]
        try:
            with mock.patch.object(bridge.urllib.request, 'urlopen', return_value=response), \
                 mock.patch.object(bridge.FEEDBACK_STORE, 'remember_reply'):
                self.assertIs(REAL_SEND('合成文本'), True)
            self.assertEqual(ids, ['9001'])
        finally:
            for var, token in reversed(tokens): var.reset(token)

    def test_split_bounds_preserve_content_without_fragment_spam(self):
        nine_lines = '\n'.join('短句'+str(i) for i in range(9))
        for text in ('一\n二\n三\n四', nine_lines, '甲'*70+'\n'+'乙'*70, '```py\nx=1\n```', '1. 第一步\n2. 第二步', 'if x:\n    print(x)'):
            self.assertEqual(split_social_reply(text), [text])
        self.assertEqual(split_social_reply(nine_lines, max_lines=20), [nine_lines])
        self.assertEqual(split_social_reply('好\n这回确实顺手'), ['好', '这回确实顺手'])
