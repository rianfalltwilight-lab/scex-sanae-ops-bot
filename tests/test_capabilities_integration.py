"""Integrated DS capabilities; fixtures never send QQ or external requests."""
import copy
import importlib
import json
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest import mock

os.environ['SCE_BOT_TEST_MODE'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network disabled')):
    s = importlib.import_module('sanae_ai')
OBSERVE_IMAGES = s._observe_images


def response(text='完成', calls=None):
    msg = {'content': text, 'reasoning_content': 'internal reasoning'}
    if calls:
        msg['tool_calls'] = calls
    return {'choices': [{'message': msg, 'finish_reason': 'tool_calls' if calls else 'stop'}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}


class CapabilitiesIntegrationTests(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network disabled')),
                        mock.patch.object(s, 'TEXT_PROVIDER', 'deepseek'),
                        mock.patch.object(s, 'VISION_PROVIDER', 'deepseek'),
                        mock.patch.object(s, '_observe_images', return_value=('', None)),
                        mock.patch.object(s, '_HISTORY', {}), mock.patch.object(s, '_CHAT_HISTORY', {}),
                        mock.patch.object(s, '_LAST_INTERACTION', {}),
                        mock.patch.object(s.SHARED_KNOWLEDGE, 'context', return_value=''),
                        mock.patch.object(s, '_add_text_usage')):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_deepseek_strict_transport_does_not_mutate_original_tools(self):
        tool = copy.deepcopy(s.WEB_SEARCH)
        payload = {'model': s.DS_MODEL, 'messages': [], 'tools': [tool], 'stream': False}
        with mock.patch.object(s, '_post', return_value=response()) as post:
            s._text_post(payload)
        self.assertEqual(post.call_args.args[0], 'https://api.deepseek.com/beta/chat/completions')
        sent = post.call_args.args[2]['tools'][0]['function']
        self.assertTrue(sent['strict'])
        self.assertEqual(set(sent['parameters']['required']), {'query', 'count'})
        self.assertNotIn('strict', tool['function'])
        self.assertEqual(payload['tools'][0], s.WEB_SEARCH)

    def test_stream_transport_receives_schema_and_callback(self):
        callback = mock.Mock()
        with mock.patch.object(s.ds_features, 'stream_post', return_value=response()) as post:
            s._text_post({'tools': [s.WEB_SEARCH], 'stream': True}, on_content=callback)
        self.assertTrue(post.call_args.args[2]['tools'][0]['function']['strict'])
        self.assertIs(post.call_args.kwargs['on_content'], callback)

    def test_other_provider_keeps_existing_transport(self):
        with mock.patch.object(s, 'TEXT_PROVIDER', 'gpt6luna'), mock.patch.object(s, '_post', return_value=response()) as post:
            payload = {'tools': [s.WEB_SEARCH], 'stream': False}
            s._text_post(payload)
        self.assertIs(post.call_args.args[2], payload)

    def test_multiple_images_share_current_turn_with_tools(self):
        seen = []
        def post(payload, **kwargs):
            seen.append(copy.deepcopy(payload))
            return response('第一张红色，第二张蓝色。')
        with mock.patch.object(s, '_vision_media_url', side_effect=lambda url: 'data:image/png;base64,' + url), \
             mock.patch.object(s, '_text_post', side_effect=post):
            answer, _, _ = s.run_agent('比较这两张图', 'u', False, 'g', image_urls=['red', 'blue'], short_reply=True)
        content = seen[0]['messages'][-1]['content']
        self.assertEqual([row['image_url']['url'] for row in content[1:]],
                         ['data:image/png;base64,red', 'data:image/png;base64,blue'])
        self.assertIn('web_search', {row['function']['name'] for row in seen[0]['tools']})
        self.assertGreaterEqual(seen[0]['max_tokens'], s.limits.ANSWER_TOKENS)
        self.assertNotIn('base64', str(s._HISTORY))

    def test_chat_screenshot_stays_in_visual_turn_instead_of_reading_group_history(self):
        seen = []
        def post(payload, **kwargs): seen.append(copy.deepcopy(payload)); return response()
        with mock.patch.object(s, '_vision_media_url', side_effect=lambda url: url), \
             mock.patch.object(s, '_text_post', side_effect=post), \
             mock.patch.object(s.qq_chat_reader, 'fetch_group_rows', side_effect=AssertionError('wrong source')):
            s.run_agent('分析这两张聊天记录截图', 'u', False, 'g', image_urls=['one', 'two'])
        self.assertIsInstance(seen[0]['messages'][-1]['content'], list)

    def test_explicit_high_effort_upgrades_short_mention_budget(self):
        seen = []
        def post(payload, **kwargs): seen.append(copy.deepcopy(payload)); return response()
        with mock.patch.object(s, '_text_post', side_effect=post):
            s.run_agent('深入分析并发事务根因', 'u', False, 'g', short_reply=True)
        self.assertEqual(seen[0]['reasoning_effort'], 'high')
        self.assertGreaterEqual(seen[0]['max_tokens'], 8192)

    def test_failed_images_and_application_image_limit_are_disclosed(self):
        seen = []
        def post(payload, **kwargs):
            seen.append(copy.deepcopy(payload)); return response()
        with mock.patch.object(s, '_vision_media_url', side_effect=ValueError('unreadable')), \
             mock.patch.object(s, '_text_post', side_effect=post):
            s.run_agent('比较截图', 'u', False, 'g', image_urls=[str(i) for i in range(8)])
        source = str(seen[0]['messages'])
        self.assertIn('第 1 张图片未读取', source)
        self.assertIn('另 2 张未读取', source)

    def test_long_material_tail_survives_old_question_limit_without_tools(self):
        seen = []
        def post(payload, **kwargs):
            seen.append(copy.deepcopy(payload));return response()
        query = '逐段分析以下长材料\n' + '日志内容' * 8000 + 'TAIL-MARKER'
        with mock.patch.object(s, '_text_post', side_effect=post):
            s.run_agent(query, 'u', False, 'g', short_reply=True)
        self.assertIn('TAIL-MARKER', seen[0]['messages'][-1]['content'])
        self.assertNotIn('tools', seen[0])
        self.assertEqual(seen[0]['max_tokens'], s.limits.LONG_TOKENS)
        self.assertLess(len(str(s._HISTORY)), s.limits.HISTORY_CHARS)

    def test_serious_effort_changes_without_affecting_ordinary_default(self):
        seen = []
        def post(payload, **kwargs): seen.append(copy.deepcopy(payload)); return response()
        with mock.patch.object(s, '_text_post', side_effect=post):
            s.run_agent('你好', 'u', False, 'g', short_reply=True)
            s.run_agent('深入分析并发事务的根因', 'u', False, 'g')
            s.run_agent('思考强度 max，严密证明', 'u', False, 'g')
        self.assertEqual([p['reasoning_effort'] for p in seen], ['low', 'high', 'max'])
        self.assertGreaterEqual(seen[2]['max_tokens'], 16384)

    def test_current_group_is_trusted_context_and_optional_nulls_are_omissions(self):
        first = response('', [{'id': 'c', 'type': 'function', 'function': {
            'name': 'read_recent_chat', 'arguments': json.dumps({'count': None, 'date': None,
            'player': None, 'keyword': None, 'before_seq': None, 'group_id': 'other-group'})}}])
        with mock.patch.object(s, '_text_post', side_effect=[first, response()]), \
             mock.patch.object(s.qq_chat_reader, 'read_group_chat', return_value='verified records') as read:
            s.run_agent('查一下最近发言', 'u', False, '2103')
        self.assertEqual(read.call_args.args[1], '2103')
        self.assertEqual(read.call_args.kwargs['count'], 50)
        self.assertEqual(read.call_args.kwargs['keyword'], '')

    def test_quoted_message_receives_current_group(self):
        with mock.patch.object(s.qq_chat_reader, 'read_verified_message', return_value={
                'group_id': 7, 'raw_message': '本群引用正文'}) as read:
            raw = s._with_reply_context('[CQ:reply,id=-123] 看看这句', group_id=7)
        read.assert_called_once_with(s.LLBOT_API, 7, -123)
        self.assertIn('本群引用正文', raw)

    def test_direct_forward_requires_verified_outer_message(self):
        with mock.patch.object(s.qq_chat_reader, 'read_qq_message', return_value='verified forward') as read:
            missing = s._with_reply_context('[CQ:forward,id=arbitrary] 总结', group_id=7)
            read.assert_not_called()
            found = s._with_reply_context('[CQ:forward,id=arbitrary] 总结', group_id=7, source_message_id=-123)
        self.assertIn('缺少本群消息身份', missing)
        read.assert_called_once_with(s.LLBOT_API, 7, -123)
        self.assertIn('verified forward', found)

    def test_recall_is_available_to_second_group_without_primary_group_lock(self):
        with mock.patch.object(s.qq_chat_reader, 'fetch_group_rows', return_value=([], {})) as read:
            answer, _, _ = s._recall_reply('总结本群聊天', 'u', 2103, s.Budget())
        self.assertEqual(read.call_args.args[1], 2103)
        self.assertNotIn('没有配置', answer)

    def test_official_fetch_is_exposed_and_source_returned(self):
        with mock.patch.object(s.ds_features, 'official_fetch', return_value={
                'url': 'https://example.com', 'title': 'Doc', 'content': '# Title', 'truncated': False}) as fetch:
            text = s.execute_tool('web_fetch', {'url': 'https://example.com'}, False)
        self.assertIn('# Title', text)
        self.assertIn('https://example.com', text)
        self.assertIn('web_fetch', {t['function']['name'] for t in s.TOOLS_MEMBER})
        fetch.assert_called_once()

    def test_long_stream_prefix_is_not_delivered_twice_or_lost_from_history(self):
        prefix = '第一段内容。' * 100
        tail = '这是最后的结论。'
        parts = []
        def post(payload, **kwargs):
            kwargs['on_content'](prefix)
            return response(prefix + tail)
        with mock.patch.object(s, '_text_post', side_effect=post):
            result = s.sanae_reply('逐段分析\n' + '材料文字' * 1500, 'stream-user',
                force=True, group_id=7, stream_cb=lambda chunk: parts.append(chunk) or True)
        self.assertEqual(parts, [prefix])
        self.assertEqual(result, tail)
        self.assertIn(prefix + tail, str(s._HISTORY))

    def test_image_observations_use_images_only_before_external_tools(self):
        data = {'images': [{'index': 1, 'observations': '红色'}, {'index': 2, 'observations': '蓝色'}]}
        parts = [{'type': 'image_url', 'image_url': {'url': 'first'}},
                 {'type': 'image_url', 'image_url': {'url': 'second'}}]
        with mock.patch.object(s, '_post', return_value=response(json.dumps(data))) as post:
            text, _ = OBSERVE_IMAGES(parts)
        payload = post.call_args.args[2]
        self.assertEqual(payload['thinking'], {'type': 'disabled'})
        self.assertEqual(payload['messages'][-1]['content'][1:], parts)
        self.assertNotIn('tools', payload)
        self.assertIn('红色', text)

    def test_fallback_preserves_tool_schema_and_non_thinking_operation_mode(self):
        requests = []
        first = response('', [{'id': 'r', 'type': 'function', 'function': {
            'name': 'run_rcon', 'arguments': '{"command":"neoforge tps"}'}}])
        responses = iter([first, response(''), response('TPS 已核对')])
        def post(payload, **kwargs):
            requests.append(copy.deepcopy(payload));return next(responses)
        with mock.patch.object(s, '_text_post', side_effect=post), \
             mock.patch.object(s, 'execute_tool', return_value='TPS=20') as execute:
            answer, _, _ = s.run_agent('当前 TPS 怎么样', 'u', False, 'g', selected_server={'id': 'legacy'})
        self.assertEqual(len(requests), 3)
        self.assertIn('tools', requests[2])
        self.assertEqual(requests[2]['thinking'], {'type': 'disabled'})
        self.assertEqual(requests[2]['tool_choice'], 'none')
        self.assertEqual(execute.call_count, 1)
        self.assertIn('已核对', answer)

    def test_stream_with_leading_whitespace_does_not_duplicate_sent_prefix(self):
        prefix = '\n' + '第一段内容。' * 100
        tail = '最后结论。'
        parts = []
        def post(payload, **kwargs):
            kwargs['on_content'](prefix)
            return response(prefix + tail)
        with mock.patch.object(s, '_text_post', side_effect=post):
            result = s.sanae_reply('逐段分析\n' + '材料文字' * 1500, 'space-user',
                force=True, group_id=7, stream_cb=lambda chunk: parts.append(chunk) or True)
        self.assertEqual(parts, [prefix])
        self.assertEqual(result, tail)

    def test_failed_stream_delivery_is_not_marked_as_sent(self):
        delivery = s._StreamDelivery(lambda chunk: False)
        delivery.feed('内容。' * 200)
        self.assertEqual(delivery.sent, '')
        self.assertTrue(delivery.stopped)

    def test_interrupted_stream_is_explicit_and_never_retried(self):
        with mock.patch.object(s, '_text_post', side_effect=s.ds_features.StreamError('EOF', {})) as post:
            answer, _, _ = s.run_agent('长文模式\n' + '材料' * 3000, 'u', False, 'g', stream_cb=lambda _: True)
        self.assertIn('生成中断', answer)
        self.assertEqual(post.call_count, 1)


if __name__ == '__main__':
    unittest.main()
