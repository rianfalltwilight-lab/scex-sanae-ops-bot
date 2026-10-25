"""Offline regressions for bounded excerpts and companion prompt wiring."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import context_policy as limits
from reply_lessons import ReplyLessons
from social_lite import SocialLite
from tests.test_fusion_integration import FusionCase, bridge, s


def turn(question, answer):
    return [{'role': 'user', 'content': question}, {'role': 'assistant', 'content': answer}]


class ChatExcerptsTests(unittest.TestCase):
    def test_long_old_pair_does_not_erase_earlier_anchor_or_recent_turn(self):
        recent = turn('现在面试结束了', '那先歇会儿。')
        messages = turn('明天有面试', '我记得。') + turn('开头' + '问' * 22000 + '末尾', '答' * 22000) + recent
        before = copy.deepcopy(messages)
        retained = limits.chat_history_tail(messages)
        self.assertEqual(messages, before)
        self.assertEqual(retained[-2:], recent)
        self.assertEqual(retained[:2], messages[:2])
        self.assertIn('旧对话原文摘录', retained[2]['content'])
        self.assertTrue(retained[2]['content'].startswith('开头'))
        self.assertTrue(retained[2]['content'].endswith('末尾'))
        self.assertLessEqual(sum(len(x['content']) for x in retained), limits.CHAT_HISTORY_CHARS)

    def test_oversized_latest_turn_remains_bounded_and_role_paired(self):
        messages = turn('较早的事', '记得。') + turn('前' + '问' * 30000 + '后', '很短的回答')
        retained = limits.chat_history_tail(messages)
        self.assertEqual([x['role'] for x in retained], ['user', 'assistant'] * 2)
        self.assertEqual(retained[-1]['content'], '很短的回答')
        self.assertLessEqual(sum(len(x['content']) for x in retained), limits.CHAT_HISTORY_CHARS)
        self.assertEqual(limits.chat_history_tail(retained), retained)

    def test_small_turns_stay_verbatim_and_keep_twelve_round_limit(self):
        messages = [row for i in range(20) for row in turn(f'问题{i}', f'回答{i}')]
        self.assertEqual(limits.chat_history_tail(messages), messages[-24:])


class CompanionWiringTests(FusionCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix='companion-wiring-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.social = SocialLite(root / 'state.json', root / 'activity.log', self_id=bridge.SANAE_QQ)
        self.stack.enter_context(mock.patch.object(bridge, 'SOCIAL_LITE', self.social))
        self.stack.enter_context(mock.patch.object(bridge.FEEDBACK_STORE, 'style_override', return_value=''))

    def capture_prompt(self, event, ambient=False):
        with mock.patch.object(s, 'sanae_reply', return_value=None) as model:
            if ambient:
                self.handler._social_reply_thread(event, '合成上下文')
            else:
                self.handler._sanae_reply_thread(event)
        return model.call_args.kwargs['context_reference']

    def test_both_reply_routes_include_closing_and_source_message_id(self):
        for ambient in (False, True):
            with self.subTest(ambient=ambient):
                event = self.event(f'[CQ:at,qq={bridge.SANAE_QQ}] 我去睡了，晚安')
                self.social.observe_closing(event)
                prompt = self.capture_prompt(event, ambient)
                self.assertIn('一句自然短答', prompt)
                self.assertIn('别追加问题', prompt)
                self.assertEqual(self.store.context.call_args.kwargs['message_id'], event['message_id'])

    def test_gentle_route_keeps_explicit_listening_but_no_normal_lessons(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ReplyLessons(Path(temp) / 'lessons.json', allowed_group_ids=[bridge.SOCIAL_LITE_GROUP])
            event = self.event(f'[CQ:at,qq={bridge.SANAE_QQ}] 先听我说，不要分析，也别追问')
            event['time'] = store.clock()
            self.assertTrue(store.observe(event, directed=True))
            with mock.patch.object(bridge, 'REPLY_LESSONS', store), mock.patch.object(
                    bridge.FEEDBACK_STORE, 'style_override', return_value='gentle'):
                for ambient in (False, True):
                    prompt = self.capture_prompt(event, ambient)
                    self.assertIn('不主动分析', prompt)
                    self.assertIn('不连续追问', prompt)
                    self.assertNotIn('可选说话心得', prompt)

    def test_dispatch_marks_direct_preference_and_observes_closing(self):
        event = self.event(f'[CQ:at,qq={bridge.SANAE_QQ}] 先听我说')
        with mock.patch.object(self.social, 'observe_closing') as closing:
            self.handler._process_event(event)
        closing.assert_called_once_with(event)
        self.assertTrue(self.lessons.observe.call_args.kwargs['directed'])

    def test_recall_only_reopens_its_source_and_forget_only_its_user(self):
        first = self.event('晚安', uid='2001', mid='601')
        other = self.event('晚安', uid='2002', mid='602')
        for event in (first, other):
            self.social.observe_closing(event)
        recall = dict(post_type='notice', notice_type='group_recall',
                      group_id=first['group_id'], user_id='2001', message_id='999')
        self.handler._process_event(recall)
        self.assertEqual(set(self.social._group(first['group_id'])['closing']), {'2001', '2002'})
        recall['message_id'] = '601'
        self.handler._process_event(recall)
        self.assertEqual(set(self.social._group(first['group_id'])['closing']), {'2002'})
        self.social.observe_closing(first)
        self.store.command.return_value = '已忘记'
        self.handler._process_event(self.event('!忘记 全部', uid='2001', mid='603'))
        self.assertEqual(set(self.social._group(first['group_id'])['closing']), {'2002'})


if __name__ == '__main__':
    unittest.main()
