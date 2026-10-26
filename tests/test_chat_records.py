"""Chat records: relay thought text, chat-record requests, forwarded records in context.

Offline: OneBot reads and model calls are mocks.
"""
import json
import tempfile
from pathlib import Path
from unittest import mock

import chat_recall
from social_lite import SocialLite
from tests.test_fusion_integration import FusionCase, bridge, s

LEAK = ('🫧🧠\n用户2004（祝が）把番茄钟跑起来了。\n按八千代的口吻回应：\n1. 看到他真跑起来了\n'
        '🫧合着我是你月底额度不够时的备用方案啊\n不过看你真跑起来了，月末来找我也行吧')


class RelayThoughtTests(FusionCase):
    def test_inlined_thought_is_removed(self):
        self.assertEqual('合着我是你月底额度不够时的备用方案啊\n不过看你真跑起来了，月末来找我也行吧',
                         s._strip_relay_thoughts(LEAK))
        self.assertEqual('', s._strip_relay_thoughts('🫧🧠\n只有想法，没有回答'))
        self.assertEqual('普通回复', s._strip_relay_thoughts('普通回复'))

    def test_gemini_normalization_never_returns_the_thought(self):
        raw = {'candidates': [{'content': {'parts': [{'text': LEAK}]}}], 'usageMetadata': {}}
        content = s._normalize_gemini_response(raw)['choices'][0]['message']['content']
        self.assertNotIn('🫧', content)
        self.assertNotIn('2004', content)
        self.assertTrue(content.startswith('合着我是'))

    def test_guard_blocks_a_marker_that_slips_through(self):
        self.assertTrue(s._leaked_tool_markup('🫧🧠 先想一想'))


class ChatRecordRequestTests(FusionCase):
    def test_digging_into_older_history_goes_to_the_reader(self):
        for text in ('早苗翻翻记录', '早苗爬楼看看', '早苗翻一下聊天记录', '早苗在聊天记录里找一下谁说要开服',
                     '早苗总结一下群聊', '早苗刚才群里聊了什么'):
            with self.subTest(text=text):
                self.assertTrue(chat_recall.is_recall(text))

    def test_recent_chat_and_forwards_stay_on_the_chat_path(self):
        # The social context already holds the last messages and expanded forwards.
        for text in ('早苗看看上面的聊天记录', '早苗你看下聊天记录', '早苗，上面他们在吵什么',
                     '早苗读一下这个聊天记录', '早苗你能看聊天记录吗'):
            with self.subTest(text=text):
                self.assertFalse(chat_recall.is_recall(text))

    def test_ordinary_chat_stays_ordinary(self):
        for text in ('早苗刚才说的南蛮鸡好吃吗', '早苗你前面那个塔怎么建', '早苗之前那个怪叫什么',
                     '早苗刚刚卡了一下', '我刚才去挖矿了', '早苗游戏记录怎么看', '早苗之前说好的呢',
                     '早苗上面那个图是什么', '早苗记录一下我的坐标', '早苗总结一下这个模组怎么玩',
                     '早苗刚才那个报错怎么解决', '早苗看看这个'):
            with self.subTest(text=text):
                self.assertFalse(chat_recall.is_recall(text))


READ_RESULT = json.dumps({'messages': [{'text': '[合并转发]\n引用转发可读消息2条；展开1份转发\n'
    + json.dumps({'说话人': '阿明', '内容': '今晚开荒', '时间': 't'}, ensure_ascii=False) + '\n'
    + json.dumps({'说话人': '小红', '内容': '带我一个', '时间': 't'}, ensure_ascii=False)}]},
    ensure_ascii=False)


class ForwardContextTests(FusionCase):
    def test_summary_keeps_speakers_and_drops_reader_headers(self):
        self.assertEqual('（2条）阿明：今晚开荒 / 小红：带我一个', bridge._forward_summary(READ_RESULT))
        self.assertEqual('', bridge._forward_summary(json.dumps({'error': '[引用内容不可读]'})))
        self.assertEqual('', bridge._forward_summary('not json'))
        unreadable = json.dumps({'messages': [{'text': '[合并转发内容不可读]\n范围'}]}, ensure_ascii=False)
        self.assertEqual('', bridge._forward_summary(unreadable))

    def test_forward_is_expanded_into_the_social_context_and_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            lite = SocialLite(Path(temp) / 's.json', Path(temp) / 'a.log')
            event = {'group_id': bridge.SOCIAL_LITE_GROUP, 'user_id': 2005, 'message_id': -43,
                     'sender': {'card': 'FixtureMember'}, 'message': [{'type': 'forward', 'data': {'id': 'x'}}]}
            with mock.patch.object(bridge, 'SOCIAL_LITE', lite), \
                    mock.patch.object(bridge._sanae_ai.qq_chat_reader, 'read_qq_message',
                                      return_value=READ_RESULT) as reader:
                bridge._ingest_forward(event)
            reader.assert_called_once_with(bridge._sanae_ai.LLBOT_API, bridge.SOCIAL_LITE_GROUP, -43)
            context = lite._context(bridge.SOCIAL_LITE_GROUP, [])
        self.assertIn('群友转发的聊天记录', context)
        self.assertIn('阿明：今晚开荒', context)
        archived = self.archive.call_args.args
        self.assertEqual('[聊天记录] （2条）阿明：今晚开荒 / 小红：带我一个', archived[3])

    def test_intake_hands_forwards_to_the_worker(self):
        event = self.event('[CQ:forward,id=abc]', uid='30001', mid='9')
        event['message'] = [{'type': 'forward', 'data': {'id': 'abc'}}]
        self.handler._process_event(event)
        targets = [c.args[0] for c in self.worker.call_args_list if c.args]
        self.assertIn(bridge._ingest_forward, targets)


class MuteInContextTests(FusionCase):
    def test_mute_notice_is_labelled_as_her_own_action(self):
        with tempfile.TemporaryDirectory() as temp:
            lite = SocialLite(Path(temp) / 's.json', Path(temp) / 'a.log')
            lite.record_outgoing(bridge.SOCIAL_LITE_GROUP, '（禁言 5分钟）先冷静一下', action='mute',
                                 reply_to_user_id='20001')
            context = lite._context(bridge.SOCIAL_LITE_GROUP, [])
        self.assertIn('助手自身玩笑禁言 禁言用户20001', context)
        self.assertIn('（禁言 5分钟）先冷静一下', context)

    def test_the_persona_admits_she_mutes(self):
        self.assertIn('群里的玩笑禁言是你执行的', s.SANAE_SYSTEM)


class HistoryForwardTests(FusionCase):
    """'翻记录 / 总结群聊' reads history; forwarded records in it must not be blank."""

    def rows(self):
        base = 1802707200
        return [
            {'message_id': 1, 'message_seq': 1, 'time': base + 1, 'group_id': bridge.SOCIAL_LITE_GROUP,
             'message_type': 'group', 'user_id': 30001, 'sender': {'nickname': '阿明'},
             'message': [{'type': 'text', 'data': {'text': '我转一下昨晚的安排'}}]},
            {'message_id': 2, 'message_seq': 2, 'time': base + 2, 'group_id': bridge.SOCIAL_LITE_GROUP,
             'message_type': 'group', 'user_id': 30001, 'sender': {'nickname': '阿明'},
             'message': [{'type': 'forward', 'data': {'id': 'fwd-1'}}]},
        ]

    def test_compact_forward(self):
        expanded = ('[合并转发]\n引用转发可读消息2条；展开1份转发\n'
                    + json.dumps({'说话人': '服主', '内容': '周六开荒'}, ensure_ascii=False) + '\n'
                    + json.dumps({'说话人': '管理A', '内容': '带床'}, ensure_ascii=False))
        self.assertEqual('（2条）服主：周六开荒 / 管理A：带床', chat_recall.compact_forward(expanded))
        self.assertEqual('', chat_recall.compact_forward('[合并转发内容不可读]\n范围'))

    def test_history_rows_carry_expanded_forwards_into_the_evidence(self):
        import qq_chat_reader
        forward = {'messages': [
            {'sender': {'nickname': '服主'}, 'time': 1802707000,
             'content': [{'type': 'text', 'data': {'text': '周六晚八点开荒'}}]},
            {'sender': {'nickname': '管理A'}, 'time': 1802707010,
             'content': [{'type': 'text', 'data': {'text': '记得带床'}}]}]}
        calls = []

        def read_api(api, action, payload, timeout=10):
            calls.append(action)
            if action == 'get_group_msg_history':
                return {'messages': self.rows()} if calls.count(action) == 1 else {'messages': []}
            if action == 'get_forward_msg':
                self.assertEqual('fwd-1', payload['id'])
                return forward
            raise AssertionError(action)

        with mock.patch.object(chat_recall, 'read_api', side_effect=read_api):
            rows, coverage = qq_chat_reader.fetch_group_rows('http://127.0.0.1:29999', bridge.SOCIAL_LITE_GROUP)
        self.assertEqual(1, coverage['forwards_expanded'])
        self.assertEqual(1, calls.count('get_forward_msg'))
        sample = chat_recall.evidence(rows, bridge.SOCIAL_LITE_GROUP, '99999', '早苗翻一下聊天记录',
                                      now=1802707300)
        self.assertEqual(2, sample['stats']['sample_messages'])
        self.assertIn('[转发的聊天记录] （2条）服主：周六晚八点开荒 / 管理A：记得带床', sample['context'])

    def test_forward_ids_not_seen_in_this_group_are_never_read(self):
        import qq_chat_reader
        reader = qq_chat_reader._Reader('http://127.0.0.1:29999', bridge.SOCIAL_LITE_GROUP)
        rows = self.rows()                    # IDs were never "discovered" by this reader
        with mock.patch.object(chat_recall, 'read_api') as request:
            self.assertEqual(0, qq_chat_reader._expand_history_forwards(reader, rows))
        request.assert_not_called()
        self.assertNotIn('_forward_text', rows[1])
