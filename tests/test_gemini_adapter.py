"""Native Gemini request/replay regressions with a mocked HTTP transport."""
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
with mock.patch.dict(os.environ, {'GEMINI_API_KEY': 'test-placeholder',
                                  'GPT6LUNA_API_KEY': 'test-placeholder'}), \
        mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network disabled')):
    s = importlib.import_module('sanae_ai')


class GeminiAdapterTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.response = {'candidates': [{'content': {'parts': [{'text': '完成'}]}}]}
        opener = mock.Mock()

        def open_request(request, **kwargs):
            self.requests.append(json.loads(request.data.decode('utf-8')))
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(
                self.response, ensure_ascii=False).encode('utf-8')
            return response

        opener.open.side_effect = open_request
        for patcher in (
                mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network disabled')),
                mock.patch.object(socket, 'create_connection', side_effect=AssertionError('network disabled')),
                mock.patch.object(s, 'TEXT_PROVIDER', 'gemini'),
                mock.patch.object(s, 'GEMINI_KEY', 'test-placeholder'),
                mock.patch.object(s.urllib.request, 'build_opener', return_value=opener)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_tool_choice_reaches_native_transport_including_final_none(self):
        for choice, expected in ((None, 'AUTO'), ('auto', 'AUTO'),
                                 ('required', 'ANY'), ('none', 'NONE')):
            with self.subTest(choice=choice):
                payload = {'messages': [{'role': 'user', 'content': '直接给出结论。'}],
                           'tools': [s.WEB_SEARCH]}
                if choice is not None:
                    payload['tool_choice'] = choice
                original = copy.deepcopy(payload)
                result = s._text_post(payload)
                sent = self.requests[-1]
                self.assertEqual(sent['toolConfig']['functionCallingConfig']['mode'], expected)
                self.assertEqual(sent['tools'][0]['functionDeclarations'][0]['name'], 'web_search')
                self.assertEqual(result['choices'][0]['message']['content'], '完成')
                self.assertEqual(payload, original)

    def test_background_memory_requests_json_without_tools(self):
        memory = {'summary': '成员喜欢红茶。'}
        self.response = {'candidates': [{'content': {'parts': [
            {'text': json.dumps(memory, ensure_ascii=False)}]}}]}
        batch = {'prompt': '返回记忆 JSON。', 'messages': ['我喜欢红茶'],
                 'existing_events': []}
        result = s.summarize_conversation_memory(batch)
        sent = self.requests[-1]
        self.assertEqual(json.loads(result), memory)
        self.assertEqual(sent['generationConfig']['responseMimeType'], 'application/json')
        self.assertEqual(sent['generationConfig']['maxOutputTokens'], 900)
        self.assertEqual(sent['systemInstruction']['parts'], [{'text': batch['prompt']}])
        self.assertEqual(json.loads(sent['contents'][0]['parts'][0]['text']),
                         {'messages': batch['messages'], 'existing_events': []})
        self.assertNotIn('tools', sent)
        self.assertNotIn('toolConfig', sent)

    def test_plain_reply_keeps_text_output_and_existing_generation_defaults(self):
        s._text_post({'messages': [{'role': 'user', 'content': '你好'}]})
        generation = self.requests[-1]['generationConfig']
        self.assertNotIn('responseMimeType', generation)
        self.assertEqual(generation['maxOutputTokens'], 800)
        self.assertEqual(generation['thinkingConfig'], {'thinkingLevel': s.GEMINI_THINKING_LEVEL})

    def test_requested_json_removes_only_complete_outer_fence_and_preserves_parts(self):
        for opening in ('```json', '```'):
            with self.subTest(opening=opening):
                text = opening + '\n{"ok":true}\n```'
                parts = [{'text': text, 'thoughtSignature': 'c2ln'}]
                self.response = {'candidates': [{'content': {'parts': parts}}]}
                result = s._text_post({'messages': [{'role': 'user', 'content': '返回 JSON。'}],
                                      'response_format': {'type': 'json_object'}})
                message = result['choices'][0]['message']
                self.assertEqual(message['content'], '{"ok":true}')
                self.assertEqual(json.loads(message['content']), {'ok': True})
                self.assertEqual(message['_gemini_parts'], parts)

    def test_requested_json_rejects_invalid_nonobject_and_unknown_wrappers(self):
        for text in ('{"ok":', '[]', 'true', '```json\n{"ok":true}',
                     '```javascript\n{"ok":true}\n```',
                     '说明\n```json\n{"ok":true}\n```'):
            with self.subTest(text=text):
                self.response = {'candidates': [{'content': {'parts': [{'text': text}]}}]}
                with self.assertRaises(ValueError):
                    s._text_post({'messages': [{'role': 'user', 'content': '返回 JSON。'}],
                                  'response_format': {'type': 'json_object'}})

    def test_normal_text_keeps_fenced_code_unchanged(self):
        for text in ('```json\n{"ok":true}\n```', '```python\nprint("ok")\n```'):
            with self.subTest(text=text):
                parts = [{'text': text, 'thoughtSignature': 'c2ln'}]
                self.response = {'candidates': [{'content': {'parts': parts}}]}
                result = s._text_post({'messages': [{'role': 'user', 'content': '给出代码。'}]})
                message = result['choices'][0]['message']
                self.assertEqual(message['content'], text)
                self.assertEqual(message['_gemini_parts'], parts)

    def test_signatures_and_parallel_tool_results_survive_final_replay(self):
        parts = [
            {'text': '内部思考', 'thought': True, 'thoughtSignature': 'dGhvdWdodA=='},
            {'functionCall': {'id': 'call-a', 'name': 'web_search', 'args': {'query': '甲'}},
             'thoughtSignature': 'c2lnLWE='},
            {'functionCall': {'id': 'call-b', 'name': 'web_search', 'args': {'query': '乙'}}},
        ]
        self.response = {'candidates': [{'content': {'parts': parts}}]}
        first = s._text_post({'messages': [{'role': 'user', 'content': '查两项资料。'}],
                              'tools': [s.WEB_SEARCH], 'tool_choice': 'required'})
        message = first['choices'][0]['message']
        self.assertEqual(message['content'], '')
        messages = [{'role': 'user', 'content': '查两项资料。'},
                    {'role': 'assistant', **message}]
        for call, output in zip(message['tool_calls'], ('甲结果', '乙结果')):
            messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': output})
        self.response = {'candidates': [{'content': {'parts': [{'text': '两项均已核对。'}]}}]}
        result = s._text_post({'messages': messages, 'tools': [s.WEB_SEARCH],
                              'tool_choice': 'none'})
        sent = self.requests[-1]
        self.assertEqual(sent['contents'][1], {'role': 'model', 'parts': parts})
        self.assertEqual(sent['contents'][2], {'role': 'user', 'parts': [
            {'functionResponse': {'id': 'call-a', 'name': 'web_search', 'response': {'output': '甲结果'}}},
            {'functionResponse': {'id': 'call-b', 'name': 'web_search', 'response': {'output': '乙结果'}}},
        ]})
        self.assertEqual(sent['toolConfig']['functionCallingConfig']['mode'], 'NONE')
        self.assertEqual(result['choices'][0]['message']['content'], '两项均已核对。')

    def test_idless_call_replays_signature_and_result_without_fabricated_provider_id(self):
        part = {'functionCall': {'name': 'web_search', 'args': {'query': '测试'}},
                'thoughtSignature': 'c2ln'}
        normalized = s._normalize_gemini_response({'candidates': [{'content': {'parts': [part]}}]})
        message = normalized['choices'][0]['message']
        call = message['tool_calls'][0]
        s._text_post({'messages': [
            {'role': 'assistant', **message},
            {'role': 'tool', 'tool_call_id': call['id'], 'name': call['function']['name'],
             'content': '测试结果'},
        ], 'tools': [s.WEB_SEARCH], 'tool_choice': 'none'})
        contents = self.requests[-1]['contents']
        self.assertEqual(contents[0]['parts'], [part])
        self.assertEqual(contents[1]['parts'], [{'functionResponse': {
            'name': 'web_search', 'response': {'output': '测试结果'}}}])


if __name__ == '__main__':
    unittest.main()
