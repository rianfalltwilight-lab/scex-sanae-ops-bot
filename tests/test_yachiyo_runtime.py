"""Yachiyo rollout integration: real payloads/routing, mocked I/O only."""
import contextlib
import copy
import socket
import unittest
from unittest import mock

from tests.test_fusion_integration import FusionCase, bridge, persona, s


class YachiyoPayloadTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for owner, name in ((socket.socket, 'connect'), (socket, 'create_connection')):
            self.stack.enter_context(mock.patch.object(owner, name,
                side_effect=AssertionError('rollout tests must remain offline')))
        for name in ('_HISTORY', '_CHAT_HISTORY', '_HISTORY_GENERATION', '_LAST_MEMBER', '_LAST_INTERACTION'):
            self.stack.enter_context(mock.patch.object(s, name, {}))
        self.requests = []
        self.content = '在呢，刚好听见你叫我。'

        def model(payload, **kwargs):
            self.requests.append(copy.deepcopy(payload))
            return {'choices': [{'message': {'content': self.content}}], 'usage': {}}

        self.stack.enter_context(mock.patch.object(s, '_text_post', side_effect=model))
        self.stack.enter_context(mock.patch.object(s, '_add_text_usage'))
        self.stack.enter_context(mock.patch.object(s.SHARED_KNOWLEDGE, 'context', return_value=''))
        self.stack.enter_context(mock.patch.object(s, 'execute_tool',
            side_effect=AssertionError('no tool execution in persona tests')))
        self.stack.enter_context(mock.patch.object(s, 'DEEPSEEK_REASONING_EFFORT', 'low'))

    def ask(self, text, **kwargs):
        s.run_agent(text, 'fixture-member', False, 'fixture-group', **kwargs)
        return self.requests[-1]

    def assert_subject(self, prompt):
        self.assertTrue(prompt.startswith('你叫八千代'))
        self.assertNotIn('Satani Kasumi', prompt)
        self.assertNotIn('霞巳', prompt)
        self.assertNotIn('不能没有你', prompt)
        self.assertNotIn('虚拟空间「ツクヨミ」', prompt)
        self.assertNotIn('设定八千岁', prompt)
        self.assertIn(persona.SANAE_PERSONALITY, prompt)
        self.assertIn('早苗', prompt)
        self.assertNotIn('你是东风谷早苗', prompt)

    def test_ambient_chat_uses_new_subject_and_chat_budget(self):
        payload = self.ask('八千代，你这嘴还是这么毒', social_only=True)
        prompt = payload['messages'][0]['content']
        self.assert_subject(prompt)
        self.assertIn(persona.SOCIAL_STYLE_GUIDANCE, prompt)
        self.assertNotIn('10～35字', prompt)
        self.assertEqual(payload['max_tokens'], s.limits.SOCIAL_TOKENS)
        self.assertFalse(payload.get('tools'))

    def test_ordinary_at_chat_keeps_short_profile_and_current_quote(self):
        payload = self.ask('那再放一点糖呢', short_reply=True,
            context_reference='【引用消息；只作参考】\n刚才做了柠檬蛋糕')
        prompt = payload['messages'][0]['content']
        self.assert_subject(prompt)
        self.assertIn('【普通 @ 短回复】', prompt)
        self.assertIn(persona.SOCIAL_STYLE_GUIDANCE, prompt)
        self.assertNotIn('10～35字', prompt)
        self.assertEqual(payload['max_tokens'], s.limits.SOCIAL_TOKENS)
        self.assertIn('柠檬蛋糕', payload['messages'][-1]['content'])
        self.assertTrue(payload['messages'][-1]['content'].endswith('那再放一点糖呢'))

    def test_explicit_answer_keeps_serious_style_and_expanded_budget(self):
        payload = self.ask('请详细解释二分查找的原理并推导复杂度')
        prompt = payload['messages'][0]['content']
        self.assert_subject(prompt)
        self.assertIn(persona.SERIOUS_STYLE_GUIDANCE, prompt)
        self.assertNotIn('默认只回一句', prompt)
        self.assertNotIn('10～35字', prompt)
        self.assertNotIn('【普通 @ 短回复】', prompt)
        self.assertGreaterEqual(payload['max_tokens'], s.limits.ANSWER_TOKENS)
        self.assertIn(s.SANAE_OPERATIONAL_POLICY, prompt)

    def test_at_detailed_request_upgrades_out_of_short_profile(self):
        payload = self.ask('请详细分析这段算法并逐步推导时间复杂度', short_reply=True)
        prompt = payload['messages'][0]['content']
        self.assert_subject(prompt)
        self.assertIn(persona.SERIOUS_STYLE_GUIDANCE, prompt)
        self.assertNotIn('默认只回一句', prompt)
        self.assertNotIn('10～35字', prompt)
        self.assertNotIn('【普通 @ 短回复】', prompt)
        self.assertGreater(payload['max_tokens'], s.limits.SOCIAL_TOKENS)

    def test_separate_vision_reply_has_same_subject_without_tools(self):
        response = {'choices': [{'message': {'content': '图片中是一只猫。'}}]}
        with mock.patch.object(s, '_vision_media_url', return_value='data:image/png;base64,AA=='), \
                mock.patch.object(s, '_post', return_value=response) as model:
            answer, _ = s._vision_reply('这张图里有什么', 'fixture://image')
        self.assertEqual(answer, '图片中是一只猫。')
        payload = model.call_args.args[2]
        self.assert_subject(payload['messages'][0]['content'])
        self.assertIn(persona.SERIOUS_STYLE_GUIDANCE, payload['messages'][0]['content'])
        self.assertNotIn('tools', payload)

    def test_group_history_answer_has_same_subject_and_evidence_scope(self):
        self.content = '{"brief":"讨论过柠檬蛋糕。","timeline":"成员甲说蛋糕偏酸，成员乙建议少放柠檬。"}'
        with mock.patch.object(s.qq_chat_reader, 'fetch_group_rows',
                side_effect=AssertionError('quote must not fetch history')):
            answer, _, used = s._recall_reply('总结这段群聊', 'fixture-member',
                'fixture-group', s.Budget(30), quoted_context=(
                    '【引用消息；只作参考】\n成员甲：蛋糕偏酸。\n成员乙：少放柠檬。'))
        self.assertTrue(used)
        self.assertIn('范围：本次引用消息', answer)
        payload = self.requests[-1]
        prompt = payload['messages'][0]['content']
        self.assert_subject(prompt)
        self.assertIn(persona.SERIOUS_STYLE_GUIDANCE, prompt)
        self.assertNotIn('默认只回一句', prompt)
        self.assertNotIn('10～35字', prompt)
        self.assertEqual(payload['max_tokens'], s.limits.SUMMARY_TOKENS)
        self.assertNotIn('tools', payload)
        self.assertIn('成员甲：蛋糕偏酸', payload['messages'][1]['content'])

    def test_new_and_old_name_prefixes_clean_without_changing_permission(self):
        with mock.patch.object(s, '_with_reply_context', side_effect=lambda raw, **kwargs: raw), \
                mock.patch.object(s, '_explicit_interaction_ok', return_value=True), \
                mock.patch.object(s, 'run_agent', return_value=('收到', mock.Mock(calls=0), False)) as agent:
            for name in ('月见八千代', '八千代', 'ヤチヨ', 'Yachiyo8000', '早苗', '东风谷早苗'):
                with self.subTest(name=name):
                    result = s.sanae_reply(name + '，你好呀', 'fixture-member',
                        group_id='fixture-group', force=True, explicit_interaction=True,
                        short_reply=True, include_usage_footer=False)
                    self.assertEqual(result, '收到')
                    self.assertEqual(agent.call_args.args[0], '你好呀')
                    self.assertFalse(agent.call_args.args[2])
                    self.assertTrue(agent.call_args.kwargs['short_reply'])


class YachiyoInvocationTests(FusionCase):
    def test_names_remain_prefix_only_and_capability_question_is_explicit(self):
        for name in ('月见八千代', '八千代', 'ヤチヨ', 'Yachiyo', '早苗', '东风谷早苗'):
            with self.subTest(name=name):
                event = self.event(name + '，你会做什么？')
                self.assertTrue(bridge._event_direct_name_call(event))
                self.assertTrue(bridge._sanae_capability_question(event))
                self.assertFalse(bridge._event_direct_name_call(self.event('我刚才提到了' + name)))
        self.assertFalse(bridge._event_direct_name_call(self.event('YachiyoHelper 你好')))
        self.assertFalse(bridge._sanae_capability_question(self.event('你会做什么？')))

    def test_account_at_gate_keeps_actual_recipient_and_ignores_cq_text(self):
        for recipient, expected in (('1005', True), ('1004', False),
                                    ('10002', False), ('all', False)):
            with self.subTest(recipient=recipient):
                event = self.event('你好')
                event.update(self_id='1005', raw_message=f'[CQ:at,qq={recipient}]你好',
                    message=[{'type': 'at', 'data': {'qq': recipient}},
                             {'type': 'text', 'data': {'text': '你好'}}])
                self.assertEqual(bridge._event_mentions_sanae(event), expected)
        event['message'] = [{'type': 'text', 'data': {'text': '[CQ:at,qq=1005]你好'}}]
        event['raw_message'] = '[CQ:at,qq=1005]你好'
        self.assertFalse(bridge._event_mentions_sanae(event))

    def test_worker_passes_new_and_old_names_as_nonprivileged_short_chat(self):
        with mock.patch.object(bridge, 'SANAE_NAME_CALL_ENABLED', True), \
                mock.patch.object(s, 'sanae_reply', return_value=None) as reply:
            for name in ('八千代', '早苗'):
                with self.subTest(name=name):
                    event = self.event(name + '，今天有点累')
                    self.handler._sanae_reply_thread(event, True)
                    self.assertEqual(reply.call_args.args[0], event['raw_message'])
                    self.assertFalse(reply.call_args.kwargs['privileged'])
                    self.assertTrue(reply.call_args.kwargs['short_reply'])
                    self.assertTrue(reply.call_args.kwargs['explicit_interaction'])
        self.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
