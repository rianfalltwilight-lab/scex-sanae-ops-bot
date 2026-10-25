"""Exercise real request builders with fake receipts; never send QQ or model calls."""
import ast
import io
import json
import os
from pathlib import Path
import re
import socket
import sys
import types
import unittest
import urllib.request
from unittest import mock

from onebot_utf8 import JSON_CONTENT_TYPE, json_bytes, onebot_headers, utf8_text


ROOT = Path(__file__).resolve().parents[1]
API = 'http://127.0.0.1:3002'
GROUP = 1001


def functions(filename, *names, **extra):
    tree = ast.parse((ROOT / filename).read_text(encoding='utf-8-sig'))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in selected} == set(names)
    namespace = dict(json=json, re=re, sys=sys, urllib=types.SimpleNamespace(request=urllib.request),
        json_bytes=json_bytes, utf8_text=utf8_text, onebot_headers=onebot_headers,
        JSON_CONTENT_TYPE=JSON_CONTENT_TYPE, LLBOT_API=API, WIKI_API=API,
        LLBOT_GROUP=GROUP, GROUP_ID=GROUP, WIKI_BOT_QQ='1002', BOT_NAME='八千代',
        remaining_timeout=lambda seconds: seconds,
        onebot_success=lambda value: (True, {}), onebot_success_utf8=lambda value: (True, {}),
        print=mock.Mock(), PREFIX='[fixture]')
    namespace.update(extra)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(ROOT / filename), 'exec'), namespace)
    return namespace


class OneBotAuthTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.data = {'message_id': 601, 'group_id': GROUP, 'message_type': 'group',
                     'total_space': 1024, 'used_space': 0}
        for patch in (
            mock.patch.dict(os.environ, {'LLBOT_API_TOKEN': 'unit-onebot'}),
            mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('offline test')),
            mock.patch.object(socket, 'create_connection', side_effect=AssertionError('offline test')),
            mock.patch.object(urllib.request, 'urlopen', side_effect=self.open),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def open(self, request, *args, **kwargs):
        self.requests.append(request)
        return io.BytesIO(json_bytes({'status': 'ok', 'retcode': 0, 'data': self.data}))

    def assert_requests(self, actions, bearer='unit-onebot'):
        self.assertEqual([request.full_url.rsplit('/', 1)[-1] for request in self.requests], actions)
        for request in self.requests:
            self.assertEqual(request.get_method(), 'POST')
            self.assertEqual(request.get_header('Authorization'), 'Bearer ' + bearer if bearer else None)
            self.assertEqual(request.get_header('Content-type'), JSON_CONTENT_TYPE)
            json.loads(request.data.decode('utf-8'))

    def test_header_precedence_and_no_global_mutation(self):
        self.assertEqual(onebot_headers()['Authorization'], 'Bearer unit-onebot')
        self.assertEqual(onebot_headers('explicit')['Authorization'], 'Bearer explicit')
        self.assertNotIn('Authorization', onebot_headers(''))
        changed = onebot_headers()
        changed['Authorization'] = 'changed'
        self.assertEqual(onebot_headers()['Authorization'], 'Bearer unit-onebot')
        with mock.patch.dict(os.environ, {'LLBOT_API_TOKEN': ''}):
            self.assertNotIn('Authorization', onebot_headers())

    def test_bridge_group_private_and_both_forward_builders(self):
        runtime = functions('llbot_bridge.py', 'send_group_msg', 'send_private_msg',
            '_send_folded_ai_text', '_send_inventory_reply',
            REPLY_GROUP=types.SimpleNamespace(get=lambda: GROUP),
            REPLY_PERSON=types.SimpleNamespace(get=lambda: None))
        self.assertTrue(runtime['send_group_msg']('中文测试'))
        self.assertTrue(runtime['send_private_msg'](2001, '私聊测试'))
        self.assertTrue(runtime['_send_folded_ai_text']('长文测试', GROUP))
        self.assertTrue(runtime['_send_inventory_reply'](
            types.SimpleNamespace(pages=['清单测试'], page_command='!模组'), GROUP))
        self.assert_requests(['send_group_msg', 'send_private_msg',
                              'send_group_forward_msg', 'send_group_forward_msg'])

    def test_ai_onebot_upload_report_space_and_image_builders(self):
        runtime = functions('sanae_ai.py', '_upload_group_file', '_send_group_report',
                            '_check_group_file_space', '_send_group_image')
        runtime['_upload_group_file']('D:/fixture.txt', '文件.txt')
        runtime['_send_group_report']('报告测试')
        self.assertEqual(runtime['_check_group_file_space'](10), 1024)
        runtime['_send_group_image']('D:/fixture.png', '图片测试')
        self.assert_requests(['upload_group_file', 'send_group_msg',
                              'get_group_file_system_info', 'send_group_msg'])

    def test_media_image_metadata_uses_onebot_token(self):
        import media_transport
        self.assertEqual(media_transport.onebot(API, 'get_image', {'file': 'fixture.jpg'}), self.data)
        self.assert_requests(['get_image'])

    def test_history_and_verified_quote_share_authenticated_transport(self):
        import chat_recall
        import qq_chat_reader
        self.assertEqual(chat_recall.read_api(API, 'get_group_msg_history', {'group_id': GROUP}), self.data)
        self.assertEqual(qq_chat_reader.read_verified_message(API, GROUP, 601), self.data)
        self.assert_requests(['get_group_msg_history', 'get_msg'])

    def test_actions_explicit_token_empty_and_environment(self):
        from social_lite import OneBotActions
        for token, expected in ((None, 'unit-onebot'), ('override', 'override'), ('', None)):
            with self.subTest(token=token):
                self.requests.clear()
                OneBotActions(API, token=token).status()
                self.assert_requests(['get_status'], bearer=expected)

    def test_shop_query_uses_configured_endpoint_and_scoped_payload(self):
        runtime = functions('shop_query.py', '_send')
        receipt = runtime['_send']({'onebot_url': API}, GROUP, '商店测试',
                                   opener=types.SimpleNamespace(open=self.open))
        self.assertEqual(receipt, 601)
        self.assert_requests(['send_group_msg'])
        self.assertTrue(json.loads(self.requests[0].data)['auto_escape'])
        with self.assertRaises(ValueError):
            runtime['_send']({}, GROUP, '拒绝')
        self.assertEqual(len(self.requests), 1)

    def test_existing_monitor_and_relay_senders_share_auth(self):
        entries = [('legacy_monitor.py', 'push_group')]
        for filename, name in entries:
            with self.subTest(filename=filename):
                self.requests.clear()
                runtime = functions(filename, name, CHAT_RELAY_ENABLED=True)
                runtime[name]('通知测试')
                self.assert_requests(['send_group_msg'])

    def test_model_request_keeps_its_own_key(self):
        runtime = functions('sanae_ai.py', '_post')
        runtime['_post']('https://model.invalid/v1/chat/completions', 'unit-model', {'messages': []})
        request = self.requests[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer unit-model')
        self.assertNotIn('unit-onebot', str(request.headers))


if __name__ == '__main__':
    unittest.main()
