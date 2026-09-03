#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from social_lite import OneBotActions, SocialLite, split_social_reply


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode('utf-8')


class SocialLiteTests(unittest.TestCase):
    def make(self, **kwargs):
        # The production workspace is writable in the Windows service account;
        # use it for the fixture because this runner may deny %TEMP% writes.
        root = Path(tempfile.mkdtemp(prefix='social-lite-', dir=str(Path(__file__).resolve().parents[1])))
        return SocialLite(root / 'state.json', root / 'activity.log', **kwargs)

    def event(self, text='今天天气怎么样？', uid='10001'):
        return {
            'post_type': 'message', 'message_type': 'group', 'group_id': 1001,
            'user_id': uid, 'self_id': '1002', 'raw_message': text,
            'sender': {'nickname': '测试员'},
        }

    def test_gate_cooldown_and_persistence(self):
        social = self.make(cooldown_seconds=60)
        event = self.event()
        ok, reason = social.should_reply(event)
        self.assertTrue(ok)
        self.assertIn('question', reason)
        with mock.patch.object(social, '_save') as save:
            social.record_incoming(event)
            social.record_outgoing(event['group_id'], '收到啦')
            self.assertGreaterEqual(save.call_count, 2)
        ok, reason = social.should_reply(self.event('还有别的吗？', '10002'))
        self.assertFalse(ok)
        self.assertEqual(reason, 'cooldown')
        status = social.status(event['group_id'])
        self.assertEqual(status['replyCount'], 1)
        self.assertGreaterEqual(status['recent'], 2)

    def test_ambient_reply_weight_reduces_only_unsolicited_replies(self):
        quiet = self.make(ambient_reply_weight=0.0)
        ok, reason = quiet.should_reply(self.event('今天天气怎么样？'))
        self.assertFalse(ok)
        self.assertEqual(reason, 'ambient-weight')
        # Explicitly addressing Sanae remains reliable at any ambient weight.
        ok, reason = quiet.should_reply(self.event('早苗，你在吗？'))
        self.assertTrue(ok)
        self.assertIn('direct', reason)

    def test_debounce_coalesces_and_calls_once(self):
        social = self.make(cooldown_seconds=1)
        calls = []
        done = threading.Event()

        def callback(event, context):
            calls.append((event['raw_message'], context))
            done.set()

        with mock.patch.object(social, '_save'):
            self.assertTrue(social.maybe_schedule(self.event('第一条？'), callback, 0.1)[0])
            self.assertTrue(social.maybe_schedule(self.event('第二条？', '10002'), callback, 0.1)[0])
        self.assertTrue(done.wait(2))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], '第二条？')
        self.assertIn('第一条？', calls[0][1])

    def test_onebot_payloads_are_utf8_and_bounded(self):
        actions = OneBotActions('http://127.0.0.1:3002')
        seen = {}

        def fake_open(request, timeout):
            seen['url'] = request.full_url
            seen['headers'] = dict(request.header_items())
            seen['body'] = request.data
            return _Response({'status': 'ok', 'retcode': 0, 'data': [{'group_id': 7}]})

        with mock.patch('social_lite.urllib.request.urlopen', fake_open):
            result = actions.history('7', count=1000)
        self.assertEqual(result, [{'group_id': 7}])
        self.assertEqual(json.loads(seen['body'].decode('utf-8')), {'group_id': 7, 'count': 100})
        self.assertIn('application/json; charset=utf-8', seen['headers']['Content-type'])

    def test_onebot_mute_uses_bounded_five_minute_payload(self):
        actions = OneBotActions('http://127.0.0.1:3002')
        seen = {}

        def fake_open(request, timeout):
            seen['body'] = json.loads(request.data.decode('utf-8'))
            seen['url'] = request.full_url
            return _Response({'status': 'ok', 'retcode': 0, 'data': None})

        with mock.patch('social_lite.urllib.request.urlopen', fake_open):
            actions.mute(1001, 1004, 300)
        self.assertTrue(seen['url'].endswith('/set_group_ban'))
        self.assertEqual(seen['body'], {
            'group_id': 1001, 'user_id': 1004, 'duration': 300})

    def test_expression_style_hint_is_derived_and_decisional(self):
        social = self.make()
        with mock.patch.object(social, '_save'):
            for text in ('当', '然', '可', '以'):
                social.record_incoming(self.event(text))
        hint = social.expression_style_hint(1001, self.event('早苗'))
        self.assertIn('表达风格信号', hint)
        self.assertIn('自行判断是否镜像', hint)
        self.assertNotIn('当然可以', hint)

    def test_expression_style_hint_ignores_single_normal_message(self):
        social = self.make()
        with mock.patch.object(social, '_save'):
            social.record_incoming(self.event('这是一个完整的问题，请帮我解释一下。'))
        self.assertEqual('', social.expression_style_hint(1001, self.event('早苗')))

    def test_multiline_reply_is_split_for_transport_with_safe_bounds(self):
        self.assertEqual(['当', '然', '可', '以'], split_social_reply('当\n然\n可\n以'))
        self.assertEqual(['这是一句正常的完整回复。'], split_social_reply('这是一句正常的完整回复。'))
        self.assertEqual(['a\nb\nc\nd\ne\nf\ng'], split_social_reply('a\nb\nc\nd\ne\nf\ng'))


if __name__ == '__main__':
    unittest.main()
