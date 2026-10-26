"""Offline DeepSeek contract tests; all transport and DNS answers are mocked."""
import copy
import io
import json
import socket
import unittest
from unittest import mock

import deepseek_features as features


def frame(delta=None, *, finish=None, usage=None, index=0):
    value = {'id': 'completion-test', 'model': 'deepseek-flash', 'choices': [
        {'index': index, 'delta': delta or {}, 'finish_reason': finish}]}
    if usage is not None:
        value['usage'] = usage
    return ('data: ' + json.dumps(value, ensure_ascii=False) + '\n\n').encode('utf-8')


def response(*parts, done=True):
    return io.BytesIO(b': heartbeat\n\n' + b''.join(parts) + (b'data: [DONE]\n\n' if done else b''))


class OfflineCase(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network disabled')),
                        mock.patch.object(socket, 'create_connection', side_effect=AssertionError('network disabled'))):
            patcher.start()
            self.addCleanup(patcher.stop)


class StrictToolsTests(OfflineCase):
    def tool(self):
        return {'type': 'function', 'function': {'name': 'inspect', 'description': 'Read only',
            'parameters': {'type': 'object', 'required': ['state'], 'properties': {
                'state': {'type': 'string', 'enum': ['open', 'closed'], 'minLength': 1, 'maxLength': 30},
                'count': {'type': 'integer', 'minimum': 1, 'maximum': 20, 'default': 5},
                'filters': {'type': 'array', 'minItems': 1, 'maxItems': 3, 'items': {
                    'type': 'object', 'properties': {'word': {'type': 'string', 'maxLength': 10}},
                    'required': ['word']}}}}}}

    def test_strict_is_deep_copy_with_all_properties_required(self):
        original = [self.tool()]
        before = copy.deepcopy(original)
        converted = features.strict_tools(original)
        self.assertEqual(original, before)
        function = converted[0]['function']
        self.assertTrue(function['strict'])
        schema = function['parameters']
        self.assertEqual(schema['required'], ['state', 'count', 'filters'])
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(schema['properties']['state'], {'type': 'string', 'enum': ['open', 'closed']})
        count = schema['properties']['count']['anyOf']
        self.assertEqual(count[0], before[0]['function']['parameters']['properties']['count'])
        self.assertEqual(count[1], {'type': 'null'})
        filters = schema['properties']['filters']['anyOf'][0]
        self.assertNotIn('minItems', filters)
        self.assertNotIn('maxItems', filters)
        self.assertFalse(filters['items']['additionalProperties'])
        self.assertEqual(filters['items']['required'], ['word'])
        self.assertNotIn('maxLength', filters['items']['properties']['word'])

    def test_optional_enum_uses_nullable_branch_without_changing_enum(self):
        tool = self.tool()
        tool['function']['parameters']['required'] = []
        result = features.strict_tools([tool])[0]['function']['parameters']['properties']['state']
        self.assertEqual(result, {'anyOf': [{'type': 'string', 'enum': ['open', 'closed']}, {'type': 'null'}]})

    def test_no_parameter_function_and_nested_definitions(self):
        tool = {'type': 'function', 'function': {'name': 'ping'}}
        self.assertEqual(features.strict_tools([tool])[0]['function']['parameters'],
                         {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False})
        tool['function']['parameters'] = {'type': 'object', '$def': {
            'row': {'type': 'object', 'properties': {'x': {'type': 'string', 'minLength': 2}}}}}
        definition = features.strict_tools([tool])[0]['function']['parameters']['$def']['row']
        self.assertFalse(definition['additionalProperties'])
        self.assertEqual(definition['required'], ['x'])

    def test_schema_keywords_do_not_delete_same_named_argument(self):
        tool = {'type': 'function', 'function': {'name': 'names', 'parameters': {
            'type': 'object', 'properties': {'maxItems': {'type': 'integer', 'minimum': 1}},
            'required': ['maxItems']}}}
        result = features.strict_tools([tool])[0]['function']['parameters']
        self.assertIn('maxItems', result['properties'])

    def test_strip_only_optional_nulls_preserves_defaults_and_falsey_values(self):
        schema = {'type': 'object', 'required': ['required'], 'properties': {
            'required': {'type': 'string'}, 'optional': {'type': 'integer', 'default': 12},
            'rows': {'type': 'array', 'items': {'type': 'object', 'properties': {
                'skip': {'type': 'string'}, 'keep': {'type': 'boolean'}}}}}}
        original = {'required': None, 'optional': None, 'zero': 0, 'false': False, 'empty': '',
                    'rows': [{'skip': None, 'keep': False}]}
        saved = copy.deepcopy(original)
        self.assertEqual(features.strip_optional_nulls(original, schema), {
            'required': None, 'zero': 0, 'false': False, 'empty': '', 'rows': [{'keep': False}]})
        self.assertEqual(original, saved)


class StreamTests(OfflineCase):
    def test_collects_interleaved_tool_fragments_reasoning_and_terminal_usage(self):
        stream = response(
            frame({'role': 'assistant', 'reasoning_content': 'private reasoning'}),
            frame({'tool_calls': [{'index': 1, 'id': 'second', 'type': 'function',
                'function': {'name': 'read_', 'arguments': '{"count":'}}]}),
            frame({'tool_calls': [{'index': 0, 'id': 'first', 'function': {'name': 'sta', 'arguments': '{}'}},
                                  {'index': 1, 'function': {'name': 'chat', 'arguments': '2}'}}]}),
            frame({'tool_calls': [{'index': 0, 'function': {'name': 'tus'}}]}),
            frame(finish='tool_calls', usage={'prompt_tokens': 20, 'completion_tokens': 5,
                                            'prompt_cache_hit_tokens': 8}))
        callbacks = []
        result = features.collect_sse(stream, callbacks.append)
        message = result['choices'][0]['message']
        self.assertTrue(stream.closed)
        self.assertEqual(callbacks, [])
        self.assertEqual(message['reasoning_content'], 'private reasoning')
        self.assertEqual(message['tool_calls'], [
            {'id': 'first', 'type': 'function', 'function': {'name': 'status', 'arguments': '{}'}},
            {'id': 'second', 'type': 'function', 'function': {'name': 'read_chat', 'arguments': '{"count":2}'}}])
        self.assertEqual(result['choices'][0]['finish_reason'], 'tool_calls')
        self.assertEqual(result['usage']['prompt_cache_hit_tokens'], 8)

    def test_callback_only_complete_content_and_final_tail_stays_pending(self):
        stream = response(frame({'reasoning_content': 'NEVER SEND', 'content': '先喝'}),
                          frame({'content': '口水。第二'}), frame({'content': '句还没说完\n最后一点'}),
                          frame(finish='stop', usage={'completion_tokens': 7}))
        callbacks = []
        result = features.collect_sse(stream, callbacks.append)
        self.assertEqual(callbacks, ['先喝口水。', '第二句还没说完\n'])
        self.assertEqual(result['_stream']['pending_content'], '最后一点')
        self.assertEqual(result['_stream']['delivered_content_chars'], sum(map(len, callbacks)))
        self.assertEqual(result['choices'][0]['message']['content'], ''.join(callbacks) + '最后一点')
        self.assertNotIn('NEVER SEND', ''.join(callbacks))

    def test_usage_only_compatibility_chunk_and_multiline_sse(self):
        stream = io.BytesIO(b'data: {"choices": [],\n' + b'data: "usage": {"prompt_tokens": 9}}\n\n'
                            + frame({'content': 'Done.'}, finish='stop') + b'data: [DONE]\n\n')
        result = features.collect_sse(stream)
        self.assertEqual(result['usage']['prompt_tokens'], 9)
        self.assertEqual(result['choices'][0]['message']['content'], 'Done.')
        self.assertEqual(result['_stream']['pending_content'], 'Done.')

    def test_length_preserved_and_incomplete_fragment_not_emitted(self):
        callbacks = []
        stream = response(frame({'content': '完整。截断到一半'}, finish='length'))
        result = features.collect_sse(stream, callbacks.append)
        self.assertEqual(result['choices'][0]['finish_reason'], 'length')
        self.assertEqual(callbacks, ['完整。'])
        self.assertEqual(result['_stream']['pending_content'], '截断到一半')

    def test_abnormal_finish_raises_with_partial_and_closes(self):
        stream = response(frame({'content': '已生成的内容'}, finish='insufficient_system_resource',
                                usage={'completion_tokens': 3}))
        with self.assertRaises(features.StreamError) as error:
            features.collect_sse(stream)
        self.assertTrue(stream.closed)
        partial = error.exception.partial
        self.assertEqual(partial['choices'][0]['finish_reason'], 'insufficient_system_resource')
        self.assertEqual(partial['choices'][0]['message']['content'], '已生成的内容')
        self.assertEqual(partial['usage']['completion_tokens'], 3)

    def test_eof_or_invalid_json_is_not_a_successful_completion(self):
        for stream in (response(frame({'content': 'partial'}), done=False),
                       io.BytesIO(frame({'content': 'partial'}) + b'data: {invalid}\n\n')):
            with self.subTest(stream=stream), self.assertRaises(features.StreamError) as error:
                features.collect_sse(stream)
            self.assertTrue(stream.closed)
            self.assertEqual(error.exception.partial['choices'][0]['message']['content'], 'partial')

    def test_deadline_or_callback_error_keeps_partial_without_retry(self):
        for fail_callback in (False, True):
            stream = response(frame({'content': '生成完整句。'}, finish='stop'))
            active = mock.Mock(side_effect=TimeoutError('expired')) if not fail_callback else mock.Mock()
            callback = mock.Mock(side_effect=RuntimeError('delivery failed')) if fail_callback else None
            with self.subTest(fail_callback=fail_callback), self.assertRaises(features.StreamError):
                features.collect_sse(stream, on_content=callback, check_active=active)
            self.assertTrue(stream.closed)
            if callback:
                callback.assert_called_once_with('生成完整句。')

    def test_transport_sets_stream_usage_and_keeps_original_payload(self):
        payload = {'model': 'deepseek-flash', 'messages': [{'role': 'user', 'content': '你好'}],
                   'stream': False, 'reasoning_effort': 'low'}
        before = copy.deepcopy(payload)
        stream = response(frame({'content': '好了。'}, finish='stop'))
        opener = mock.Mock(return_value=stream)
        result = features.stream_post('https://api.deepseek.com/beta/chat/completions', 'test-only',
                                      payload, timeout=12, opener=opener)
        self.assertEqual(payload, before)
        request = opener.call_args.args[0]
        sent = json.loads(request.data.decode('utf-8'))
        self.assertTrue(sent['stream'])
        self.assertTrue(sent['stream_options']['include_usage'])
        self.assertEqual(opener.call_args.kwargs['timeout'], 12)
        self.assertEqual(result['choices'][0]['message']['content'], '好了。')
        self.assertTrue(stream.closed)


class EffortTests(OfflineCase):
    def test_normal_chat_and_questions_remain_low(self):
        for text in ('早苗在吗？', '今天吃什么？？？', '并发是什么意思？', '1+1等于几？'):
            with self.subTest(text=text):
                self.assertEqual(features.choose_effort(text), 'low')

    def test_explicit_levels_and_complex_analysis(self):
        expected = {'推理强度：max 请核实证明': 'max', 'effort=high 分析这份报告': 'high',
                    'reasoning_effort low': 'low', '请用最大推理强度检查': 'max',
                    '请逐步推导这个结论': 'high', '分析并发事务死锁的根因并设计回滚方案': 'high',
                    '不要深度思考，简单说': 'low'}
        for text, effort in expected.items():
            with self.subTest(text=text):
                self.assertEqual(features.choose_effort(text), effort)


class FetchTests(OfflineCase):
    @staticmethod
    def resolver(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))]

    def test_fetch_uses_fixed_api_and_bounds_markdown_title_and_source(self):
        post = mock.Mock(return_value={'title': '标' * 300, 'content': '# Heading\n' + '文' * 80})
        result = features.official_fetch('https://EXAMPLE.com/article#section', post=post,
            key='test-only', timeout=7, max_chars=20, resolver=self.resolver)
        post.assert_called_once_with(features.FETCH_ENDPOINT, 'test-only',
                                     {'url': 'https://example.com/article'}, timeout=7)
        self.assertEqual(len(result['title']), 240)
        self.assertEqual(result['content'], ('# Heading\n' + '文' * 80)[:20])
        self.assertEqual(result['source_host'], 'example.com')
        self.assertTrue(result['truncated'])

    def test_private_credentials_local_hosts_and_bad_schemes_never_post(self):
        post = mock.Mock()
        urls = ('http://127.0.0.1/a', 'https://10.0.0.1', 'http://[::1]/', 'http://169.254.169.254/',
                'http://localhost/', 'http://host.local/', 'https://user:secret@example.com/',
                'file:///etc/passwd', 'https://example.com/\nheader', 'http://192.168.1.1/',
                'https://example.com/' + 'x' * 8192)
        for url in urls:
            with self.subTest(url=url[:80]), self.assertRaises(ValueError):
                features.official_fetch(url, post=post, key='test-only', resolver=self.resolver)
        post.assert_not_called()

    def test_dns_with_any_private_answer_is_rejected(self):
        resolver = mock.Mock(return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.1', 443))])
        post = mock.Mock()
        with self.assertRaises(ValueError):
            features.official_fetch('https://example.com/', post=post, key='test-only', resolver=resolver)
        post.assert_not_called()

    def test_default_public_hostname_ignores_local_tun_fakeip(self):
        post = mock.Mock(return_value={'title': 'Docs', 'content': '# Official page'})
        fake_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.4', 443))]
        with mock.patch.object(socket, 'getaddrinfo', return_value=fake_dns) as dns:
            result = features.official_fetch('https://api-docs.deepseek.com/',
                                             post=post, key='test-only')
        dns.assert_not_called()
        self.assertEqual(result['source_host'], 'api-docs.deepseek.com')
        post.assert_called_once_with(features.FETCH_ENDPOINT, 'test-only',
                                     {'url': 'https://api-docs.deepseek.com/'}, timeout=20)

    def test_default_rejects_fakeip_literals_and_internal_domains_without_dns(self):
        post = mock.Mock()
        urls = ('http://198.18.0.4/', 'http://198.19.255.254/', 'http://127.0.0.1./',
                'http://localhost./', 'http://printer.local/', 'http://router.lan/',
                'https://server.internal/', 'http://device.home.arpa/', 'http://127.1/')
        with mock.patch.object(socket, 'getaddrinfo') as dns:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    features.official_fetch(url, post=post, key='test-only')
        dns.assert_not_called()
        post.assert_not_called()

    def test_fetch_response_and_content_bound_are_validated(self):
        post = mock.Mock(return_value={'title': '', 'content': None})
        with self.assertRaises(ValueError):
            features.official_fetch('https://example.com', post=post, key='test-only', resolver=self.resolver)
        post.reset_mock()
        with self.assertRaises(ValueError):
            features.official_fetch('https://example.com', post=post, key='test-only', max_chars=0,
                                    resolver=self.resolver)
        post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
