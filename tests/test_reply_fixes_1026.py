"""Reply defects seen in the main group on 10-26 afternoon, offline.

- Gemini replies broken at stray newlines ("……你要" / "是再开这种玩笑"): the relay's plain
  endpoint joins stream chunks with "\\n"; the bot now reads the stream and joins chunks itself.
- "（已对该用户执行5分钟禁言）" with no mute behind it.
- Her own quoted character card taken for a member's work (quote showed only "FixtureBot").
- A member whose card contains 群主 taken for the group owner.
- "统计30007发言记录" answered with a summary of everyone.
"""
import copy
import json
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests
import chat_recall
import qq_chat_reader

s = runtime_tests.s

SSE = ('data: {"candidates": [{"content": {"parts": [{"text": "伯爵茶是一款以红茶为基底，添加佛"}]}}],'
       ' "usageMetadata": {"promptTokenCount": 49, "candidatesTokenCount": 0}}\n\n'
       'data: {"candidates": [{"content": {"parts": [{"text": "手柑调配的茶"}]}}]}\n\n'
       'data: {"candidates": [{"content": {"parts": [{"text": "，口感清爽。", "thoughtSignature": "sig-1"}]},'
       ' "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 49, "candidatesTokenCount": 0},'
       ' "modelVersion": "relay-x"}\n\n')
SSE_TOOL = ('data: {"candidates": [{"content": {"parts": [{"text": "想一想", "thought": true}]}}]}\n\n'
            'data: {"candidates": [{"content": {"parts": [{"text": "再想", "thought": true}]}}]}\n\n'
            'data: {"candidates": [{"content": {"parts": [{"functionCall": {"name": "server_trends", "args": {"window": "1h"}},'
            ' "thoughtSignature": "sig-2"}]}, "finishReason": "STOP"}]}\n\n')


class StreamMergeTests(unittest.TestCase):
    def test_chunks_join_without_newlines_and_usage_stays_honest(self):
        raw = s._gemini_body(SSE.encode('utf-8'))
        parts = raw['candidates'][0]['content']['parts']
        self.assertEqual([{'text': '伯爵茶是一款以红茶为基底，添加佛手柑调配的茶，口感清爽。', 'thoughtSignature': 'sig-1'}], parts)
        self.assertEqual('relay-x', raw['modelVersion'])
        normalized = s._normalize_gemini_response(raw)
        self.assertEqual('伯爵茶是一款以红茶为基底，添加佛手柑调配的茶，口感清爽。', normalized['choices'][0]['message']['content'])
        # The relay streams no output token count; the footer must not claim 0.
        self.assertFalse(normalized['_usage_complete'])

    def test_thoughts_and_function_calls_keep_their_parts(self):
        raw = s._gemini_body(SSE_TOOL)
        parts = raw['candidates'][0]['content']['parts']
        self.assertEqual({'text': '想一想再想', 'thought': True}, parts[0])
        self.assertEqual('sig-2', parts[1]['thoughtSignature'])
        message = s._normalize_gemini_response(raw)['choices'][0]['message']
        self.assertEqual('', message['content'])
        self.assertEqual(('server_trends', {'window': '1h'}),
                         (message['tool_calls'][0]['function']['name'],
                          json.loads(message['tool_calls'][0]['function']['arguments'])))

    def test_function_call_pieces_are_put_back_together(self):
        # 10-26 ~18:20: name in one chunk, arguments in the next with an empty name.
        body = ('data: {"candidates": [{"content": {"parts": [{"functionCall": {"name": "run_rcon", "args": {}}}]}}]}\n\n'
                'data: {"candidates": [{"content": {"parts": [{"functionCall": {"name": "", "args": {"command": "list"}}}]}}]}\n\n'
                'data: {"candidates": [{"content": {"parts": [{"functionCall": {"name": "server_events",'
                ' "args": {"kind": "join"}}}]}, "finishReason": "STOP"}]}\n\n')
        parts = s._gemini_body(body)['candidates'][0]['content']['parts']
        self.assertEqual([{'functionCall': {'name': 'run_rcon', 'args': {'command': 'list'}}},
                          {'functionCall': {'name': 'server_events', 'args': {'kind': 'join'}}}], parts)
        calls = s._normalize_gemini_response(s._gemini_body(body))['choices'][0]['message']['tool_calls']
        self.assertEqual([('run_rcon', {'command': 'list'}), ('server_events', {'kind': 'join'})],
                         [(c['function']['name'], json.loads(c['function']['arguments'])) for c in calls])

    def test_plain_json_bodies_still_parse(self):
        body = {'candidates': [{'content': {'parts': [{'text': '完成'}]}}]}
        self.assertEqual(body, s._gemini_body(json.dumps(body, ensure_ascii=False)))
        listed = s._gemini_body(json.dumps([body, body], ensure_ascii=False))
        self.assertEqual('完成完成', listed['candidates'][0]['content']['parts'][0]['text'])

    def test_stream_endpoint_and_fallback(self):
        self.assertTrue(s._gemini_endpoint('https://relay.example', 'm', stream=True)
                        .endswith('/v1beta/models/m:streamGenerateContent?alt=sse'))
        urls = []

        def response(text):
            handle = mock.MagicMock()
            handle.__enter__.return_value.read.return_value = text.encode('utf-8')
            return handle

        def missing_stream(request, **kwargs):
            urls.append(request.full_url)
            if 'stream' in request.full_url:
                raise urllib.error.HTTPError(request.full_url, 404, 'no route', {}, None)
            return response(json.dumps({'candidates': [{'content': {'parts': [{'text': '好'}]}}]}))
        opener = mock.Mock()
        opener.open.side_effect = missing_stream
        with mock.patch.object(s.urllib.request, 'build_opener', return_value=opener), \
                mock.patch.object(s, 'GEMINI_KEY', 'test-placeholder'), mock.patch.object(s, 'GEMINI_STREAM', True):
            result = s._gemini_post({'model': 'm', 'messages': [{'role': 'user', 'content': 'x'}]})
            self.assertEqual('好', result['choices'][0]['message']['content'])
            self.assertEqual(2, len(urls))
            # 10-26 ~18:00: streaming answered 500 while the plain route worked.
            urls.clear()

            def stream_down(request, **kwargs):
                urls.append(request.full_url)
                if 'stream' in request.full_url:
                    raise urllib.error.HTTPError(request.full_url, 500, 'Upstream gateway error', {}, None)
                return response(json.dumps({'candidates': [{'content': {'parts': [{'text': '在'}]}}]}))
            opener.open.side_effect = stream_down
            self.assertEqual('在', s._gemini_post({'model': 'm', 'messages': [{'role': 'user', 'content': 'x'}]})
                             ['choices'][0]['message']['content'])
            self.assertEqual(2, len(urls))
            for code in (400, 429):
                opener.open.side_effect = lambda request, code=code, **kw: (_ for _ in ()).throw(
                    urllib.error.HTTPError(request.full_url, code, 'no', {}, None))
                with self.subTest(code=code), self.assertRaises(urllib.error.HTTPError):
                    s._gemini_post({'model': 'm', 'messages': [{'role': 'user', 'content': 'x'}]})
            opener.open.side_effect = lambda request, **kw: response(SSE)
            content = s._gemini_post({'model': 'm', 'messages': [{'role': 'user', 'content': 'x'}]})
            self.assertNotIn('\n', content['choices'][0]['message']['content'])


class FakeMuteTests(unittest.TestCase):
    def test_detector(self):
        for text in ('（已对该用户执行5分钟禁言）\n既然说了不行', '（禁言 5分钟）先冷静', '我已经把你禁言了', '我执行了禁言'):
            with self.subTest(text=text):
                self.assertTrue(s._claims_unexecuted_mute(text))
        for text in ('要是再开这种玩笑，我可要把你禁言五分钟了', '这条规则（玩笑禁言）只在主群生效', '我没有执行禁言',
                     '除非你真的故意刷屏，不然我才懒得动那个权限呢', '这回没禁言你哦，真要禁的话会直接动手，不会光在嘴上说。'):
            with self.subTest(text=text):
                self.assertFalse(s._claims_unexecuted_mute(text))
        # Right after a real joke mute she may talk about it.
        with mock.patch.object(s, 'RECENT_MUTE_FN', lambda gid: True):
            self.assertFalse(s._claims_unexecuted_mute('我已经把他禁言了', 1001))
        with mock.patch.object(s, 'RECENT_MUTE_FN', lambda gid: False):
            self.assertTrue(s._claims_unexecuted_mute('我已经把他禁言了', 1001))

    setUp = runtime_tests.YachiyoPayloadTests.setUp

    def test_reply_is_replaced_and_not_learned(self):
        model = mock.Mock(return_value={'choices': [{'message': {'content': '（已对该用户执行5分钟禁言）\n既然说了不行'}}]})
        with mock.patch.object(s, '_text_post', model), mock.patch.object(s, 'RECENT_MUTE_FN', None):
            answer, usage, _used = s.run_agent('那我要是发违禁词你会禁吗？涩图', 'fixture-member', False,
                                               'fixture-group', short_reply=True)
        self.assertEqual('这回没禁言你哦，真要禁的话会直接动手，不会光在嘴上说。', answer)
        self.assertFalse(usage.model_answer)
        self.assertEqual('fake-action', usage.failure)
        self.assertIn('不要写“（禁言 N 分钟）”', s.SANAE_CHAT_SAFETY_POLICY)


class SelfQuoteTests(unittest.TestCase):
    def test_the_bots_own_quoted_message_is_marked(self):
        rows = [{'user_id': 1002, 'sender': {'user_id': 1002, 'card': 'FixtureBot'}, 'time': 1793002538,
                 'message_id': 1, 'message': [{'type': 'text', 'data': {'text': '群主角色卡'}}]},
                {'user_id': 2006, 'sender': {'user_id': 2006, 'card': 'Charlie'}, 'time': 1793002539,
                 'message_id': 2, 'message': [{'type': 'text', 'data': {'text': '哈哈'}}]}]
        reader = qq_chat_reader._Reader('http://127.0.0.1:9', 1001)     # no request is made here
        own, other = (reader.record(row) for row in rows)
        self.assertEqual('FixtureBot（八千代本人：这是你自己之前发的消息）', own['nickname'])
        self.assertEqual('Charlie', other['nickname'])


class TargetedChatStatsTests(unittest.TestCase):
    def rows(self):
        base = 1793000000
        return [{'group_id': 1001, 'user_id': 30007, 'sender': {'card': '阿明'}, 'time': base + 1,
                 'message_id': 1, 'raw_message': '今晚开荒'},
                {'group_id': 1001, 'user_id': 30002, 'sender': {'card': '小红'}, 'time': base + 2,
                 'message_id': 2, 'raw_message': '带我一个'},
                {'group_id': 1001, 'user_id': 30007, 'sender': {'card': '阿明'}, 'time': base + 3,
                 'message_id': 3, 'raw_message': '八点见'}]

    def test_a_named_qq_is_counted_instead_of_the_asker(self):
        sample = chat_recall.evidence(self.rows(), 1001, '2008', '早苗，统计30007发言记录',
                                      now=1793000100)
        self.assertEqual(('', 2, '30007'), (sample['direct'], sample['stats']['sample_messages'],
                                                 sample['stats']['target']))
        self.assertIn('只含问题点名的 QQ 30007（名片 阿明）的发言', sample['context'])
        self.assertIn('今晚开荒', sample['context'])
        self.assertNotIn('带我一个', sample['context'])
        missing = chat_recall.evidence(self.rows(), 1001, '2008', '统计 30003 的发言', now=1793000100)
        self.assertTrue(missing['direct'].startswith('这次可见的群聊记录里没有 QQ 30003 的文本发言。'))
        # No QQ number: the old behaviour (all members, asker marked).
        plain = chat_recall.evidence(self.rows(), 1001, '30002', '总结一下今天聊了什么', now=1793000100)
        self.assertNotIn('target', plain['stats'])
        self.assertIn('提问者', plain['context'])


class GroupIdentityTests(unittest.TestCase):
    MEMBERS = [{'user_id': 4733, 'card': 'FixtureOwner', 'role': 'owner'},
               {'user_id': 2008, 'card': '自称群主的成员', 'role': 'member'},
               {'user_id': 2833, 'card': 'FixtureAdmin', 'role': 'admin'}]

    def setUp(self):
        s._GROUP_ROLES.clear()
        self.addCleanup(s._GROUP_ROLES.clear)

    def test_note_names_the_real_owner_only_when_roles_come_up(self):
        api = mock.Mock(return_value=self.MEMBERS)
        with mock.patch.object(s.chat_recall, 'read_api', api):
            note = s._group_identity_note(1001, '2008', '生成群主角色卡json形式', '自称群主的成员')
            again = s._group_identity_note(1001, '2833', '管理员在吗', 'FixtureAdmin')
            plain = s._group_identity_note(1001, '30002', '今晚开荒', '小红')
            card_only = s._group_identity_note(1001, '2008', '[文件]', '自称群主的成员')
        self.assertIn('本群群主是「FixtureOwner」。', note)
        self.assertIn('当前发言者「自称群主的成员」在群里是普通成员。', note)
        self.assertIn('不要把别人认成群主', note)
        self.assertIn('当前发言者「FixtureAdmin」在群里是管理员。', again)
        self.assertEqual('', plain)
        self.assertIn('普通成员', card_only)                  # a card with 群主 is enough to add the note
        api.assert_called_once()                             # cached
        s._GROUP_ROLES.clear()
        with mock.patch.object(s.chat_recall, 'read_api', side_effect=OSError('down')):
            self.assertEqual('', s._group_identity_note(1001, '1', '群主是谁', ''))

    def test_prompts_carry_the_new_rules(self):
        source = Path(s.__file__).read_text(encoding='utf-8')
        self.assertIn('identity_note = _group_identity_note(group_id, user_id, user_text, nickname)', source)
        self.assertIn('结果末尾的“本次已在线”已经算好，直接照着回答', source)

    def test_events_tool_works_out_current_sessions_from_the_live_list(self):
        import tempfile
        import server_insight
        with tempfile.TemporaryDirectory() as ops:
            rows = [{'timestamp': 1000, 'type': 'join', 'text': '[怀旧] 玩家 Litxue 加入服务器（1/20）',
                     'details': {'player': 'Litxue'}},
                    {'timestamp': 2000, 'type': 'join', 'text': '[怀旧] 玩家 MioPha 加入服务器（2/20）',
                     'details': {'player': 'MioPha'}},
                    {'timestamp': 2600, 'type': 'minecraft_event', 'text': '[怀旧] 玩家 MioPha 离开服务器', 'details': {}},
                    {'timestamp': 3000, 'type': 'join', 'text': '[怀旧] 玩家 MioPha 加入服务器（2/20）',
                     'details': {'player': 'MioPha'}},
                    {'timestamp': 3300, 'type': 'join', 'text': '[怀旧] 玩家 Enggoal 加入服务器（3/20）',
                     'details': {'player': 'Enggoal'}}]
            Path(ops, 'events.jsonl').write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n',
                                                encoding='utf-8')
            # Litxue's server crashed: no leave line, but not in the live list either.
            text = server_insight.events(ops, '24h', kind='join', now=3600, online=['Enggoal', 'MioPha', 'Ghost'])
            plain = server_insight.events(ops, '24h', kind='join', now=3600)
        self.assertIn('现在在线的玩家本次已在线（从长到短）：MioPha 10 分钟（', text)
        self.assertIn('、Enggoal 5 分钟（', text)
        self.assertIn('；Ghost 的进服时间不在这段记录里', text)
        self.assertNotIn('Litxue 4', text)
        self.assertNotIn('本次已在线', plain)


if __name__ == '__main__':
    unittest.main()
