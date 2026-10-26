"""Recall intent boundaries and real agent routing with all I/O mocked."""
import unittest
from unittest import mock

import chat_recall
from tests import test_yachiyo_runtime as runtime_tests


FEEDBACK = '你刚才提议的断句我真改了，同事说比原来好读很多。'
REFERENCE = '八千代本人：这一大句拆在“直到天亮”后面，喘口气再接。'


class ChatRecallIntentTests(unittest.TestCase):
    def test_adopted_advice_feedback_is_not_history_query(self):
        self.assertFalse(chat_recall.is_recall(FEEDBACK))
        self.assertFalse(chat_recall.is_recall('八千代，' + FEEDBACK))

    def test_personal_frequency_does_not_cross_clause_boundaries(self):
        for separator in ('，', ',', '。', '.', '！', '!', '？', '?', '；', ';', '\n', '\r\n'):
            with self.subTest(separator=repr(separator)):
                self.assertFalse(chat_recall.is_recall(
                    '我真改了' + separator + '同事说比原来好读很多'))
                self.assertFalse(chat_recall.is_recall(
                    '我真改了' + separator + '同事经常说这版好读'))
                self.assertFalse(chat_recall.is_recall(
                    '我最近改了' + separator + '同事说啥都好'))
                self.assertFalse(chat_recall.is_recall(
                    '我经常改稿' + separator + '同事说这版好读'))

    def test_personal_recall_questions_still_match(self):
        for text in (
            '我最近说了多少次咖啡？',
            '我是不是咖啡说得太多了',
            '本人提过咖啡几次',
            '统计一下我说“咖啡”的次数',
            '我经常说什么？',
            '我常常提咖啡吗',
            '我平时聊什么',
            '我最近发了啥',
            '八千代，我最近说了多少次咖啡？',
            '先别接着聊，我最近说了多少次咖啡？',
        ):
            with self.subTest(text=text):
                self.assertTrue(chat_recall.is_recall(text))

    def test_personal_count_question_can_continue_after_comma(self):
        for text in ('我说咖啡，次数多吗', '我提过咖啡，具体多少次', '我最近说了，什么？'):
            with self.subTest(text=text):
                self.assertTrue(chat_recall.is_recall(text))

    def test_group_and_quoted_analysis_still_match(self):
        for text in (
            '总结昨天的群聊',
            '群友最近都聊了什么',
            '帮我总结一下，群里昨天聊了什么',
            '[CQ:reply,id=42] 分析这段对话',
            '[CQ:forward,id=fixture] 总结一下',
            '帮我点评一下\n[引用消息]\n甲：这个断句不错。',
        ):
            with self.subTest(text=text):
                self.assertTrue(chat_recall.is_recall(text))


class ChatRecallRouteTests(unittest.TestCase):
    setUp = runtime_tests.YachiyoPayloadTests.setUp

    def test_feedback_uses_natural_chat_and_keeps_current_reference(self):
        s = runtime_tests.s
        self.assertIs(s.chat_recall, chat_recall)
        with mock.patch.object(s, '_recall_reply',
                side_effect=AssertionError('feedback must not enter history route')), \
                mock.patch.object(s.qq_chat_reader, 'fetch_group_rows',
                    side_effect=AssertionError('feedback must not read QQ history')):
            answer, _, _ = s.run_agent(FEEDBACK, 'fixture-member', False,
                'fixture-group', short_reply=True, context_reference=REFERENCE)
        self.assertEqual(answer, self.content)
        self.assertEqual(len(self.requests), 1)
        payload = self.requests[0]
        self.assertEqual(payload['max_tokens'], s.limits.SOCIAL_TOKENS)
        self.assertEqual(payload['reasoning_effort'], 'low')
        self.assertIn(REFERENCE, payload['messages'][-1]['content'])
        self.assertTrue(payload['messages'][-1]['content'].endswith(FEEDBACK))

    def test_actual_history_query_keeps_recall_route(self):
        s = runtime_tests.s
        query = '我最近说了多少次咖啡？'
        with mock.patch.object(s, '_recall_reply',
                return_value=('离线历史结果', s._Usage(), False)) as recall:
            answer, _, _ = s.run_agent(query, 'fixture-member', False,
                'fixture-group', short_reply=True)
        self.assertEqual(answer, '离线历史结果')
        recall.assert_called_once()
        self.assertEqual(recall.call_args.args[0], query)
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
