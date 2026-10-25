import json
from datetime import datetime
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import chat_recall
import qq_chat_reader as reader


API = 'http://127.0.0.1:29999'
GROUP = 1001


def message(sequence, text='正文', *, group=GROUP, nickname='测试群友', identifier=None):
    return {'message_id': sequence if identifier is None else identifier,
            'message_seq': sequence, 'time': 1802707200 + sequence,
            'group_id': group, 'message_type': 'group', 'user_id': 12345,
            'sender': {'nickname': nickname}, 'message': text}


class QQChatReaderTests(unittest.TestCase):
    def history(self, pages, **options):
        with patch.object(chat_recall, 'read_api', side_effect=pages) as request:
            result = json.loads(reader.read_group_chat(API, GROUP, **options))
        return result, request

    def test_missing_group_context_refuses_network(self):
        with patch.object(chat_recall, 'read_api') as request:
            result = json.loads(reader.read_group_chat(API, None))
            self.assertIn('读取失败', result['error'])
            self.assertFalse(result['coverage']['complete_history'])
            result = json.loads(reader.read_qq_message(API, None, -15))
            self.assertIn('[引用内容不可读]', result['error'])
            request.assert_not_called()

    def test_history_pages_are_deduplicated_and_chronological(self):
        result, request = self.history([
            {'messages': [message(3), message(2)]},
            {'messages': [message(2), message(1)]},
            {'messages': [message(1)]},
        ], count=10)
        self.assertEqual([row['message_seq'] for row in result['messages']], [1, 2, 3])
        self.assertEqual(result['coverage']['pages'], 3)
        self.assertEqual(result['coverage']['returned'], 3)
        self.assertFalse(result['coverage']['complete_history'])
        for call in request.call_args_list:
            self.assertEqual(call.args[1], 'get_group_msg_history')
            self.assertEqual(call.args[2]['group_id'], GROUP)

    def test_before_sequence_is_sent_and_is_exclusive(self):
        result, request = self.history([
            {'messages': [message(50), message(49), message(48)]},
            {'messages': []},
        ], before_seq=50, count=1)
        self.assertEqual(request.call_args_list[0].args[2]['message_seq'], 50)
        self.assertEqual([row['message_seq'] for row in result['messages']], [49])
        self.assertEqual(result['coverage']['cursor_next'], 49)
        self.assertEqual(result['coverage']['scanned_cursor_next'], 48)

    def test_unfiltered_small_count_reads_one_page_and_returns_newest_two(self):
        for sequences in (range(1, 100), range(99, 0, -1)):
            page = [message(sequence) for sequence in sequences]
            for row in page:
                row['time'] = 1802707200
            with self.subTest(order=page[0]['message_seq']):
                result, request = self.history([{'messages': page}], count=2)
                self.assertEqual(request.call_count, 1)
                self.assertEqual([row['message_seq'] for row in result['messages']], [98, 99])
                self.assertEqual(result['coverage']['cursor_next'], 98)
                self.assertEqual(result['coverage']['scanned_cursor_next'], 1)

    def test_filtered_small_count_can_still_find_an_older_page(self):
        result, request = self.history([
            {'messages': [message(10, '不相关的新消息')]},
            {'messages': [message(9, '匹配目标')]},
            {'messages': []},
        ], count=1, keyword='目标')
        self.assertEqual(request.call_count, 3)
        self.assertEqual([row['message_seq'] for row in result['messages']], [9])

    def test_empty_page_does_not_claim_entire_history(self):
        result, _ = self.history([{'messages': []}])
        self.assertEqual(result['messages'], [])
        self.assertEqual(result['coverage']['stop_reason'], 'empty_page')
        self.assertFalse(result['coverage']['complete_history'])

    def test_page_limit_is_reported(self):
        pages = [{'messages': [message(100-i)]} for i in range(10)]
        result, request = self.history(pages, count=1000)
        self.assertEqual(request.call_count, 10)
        self.assertEqual(result['coverage']['stop_reason'], 'page_limit')
        self.assertTrue(result['coverage']['truncated'])

    def test_row_limit_records_its_real_oldest_sequence(self):
        with patch.object(chat_recall.limits, 'CHAT_ROWS', 2):
            result, request = self.history([{'messages': [message(3), message(2), message(1)]}], count=2)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result['coverage']['stop_reason'], 'row_limit')
        self.assertEqual(result['coverage']['cursor_next'], 2)

    def test_first_page_failure_is_explicit_and_does_not_expose_error_url(self):
        result, _ = self.history([OSError('http://private.invalid/?token=never-output')])
        self.assertIn('读取失败', result['error'])
        self.assertNotIn('never-output', json.dumps(result))
        self.assertFalse(result['coverage']['complete_history'])

    def test_later_page_failure_preserves_partial_results(self):
        result, _ = self.history([{'messages': [message(10)]}, ValueError('backend down')])
        self.assertEqual(len(result['messages']), 1)
        self.assertEqual(result['coverage']['failures'], 1)
        self.assertEqual(result['coverage']['stop_reason'], 'page_failed')
        self.assertTrue(any('分页读取失败' in warning for warning in result['warnings']))

    def test_raw_statistics_rows_keep_group_filter_and_partial_diagnostics(self):
        own = message(10, '供现有统计使用')
        foreign = message(9, '不可进入统计', group=99)
        with patch.object(chat_recall, 'read_api', side_effect=[{'messages': [own, foreign]}, OSError('next page unavailable')]):
            rows, coverage = reader.fetch_group_rows(API, GROUP)
        self.assertEqual(rows, [own])
        self.assertEqual(coverage['group_id'], GROUP)
        self.assertEqual(coverage['failures'], 1)
        self.assertFalse(coverage['complete_history'])
        self.assertTrue(any('分页读取失败' in value for value in coverage['warnings']))
        self.assertTrue(any('已排除' in value for value in coverage['warnings']))

    def test_raw_statistics_rows_reject_missing_group_before_network(self):
        with patch.object(chat_recall, 'read_api') as request:
            with self.assertRaisesRegex(ValueError, 'QQ历史读取失败'):
                reader.fetch_group_rows(API, None)
            request.assert_not_called()

    def test_other_groups_and_private_messages_are_excluded(self):
        private = message(8, '私聊正文不可泄漏')
        private.pop('group_id')
        private['message_type'] = 'private'
        result, _ = self.history([{'messages': [message(10, '本群'), message(9, '他群不可泄漏', group=99), private]}, {'messages': []}])
        self.assertEqual(len(result['messages']), 1)
        self.assertNotIn('不可泄漏', json.dumps(result, ensure_ascii=False))

    def test_top_level_foreign_group_response_is_rejected(self):
        result, _ = self.history([{'group_id': 99, 'messages': [message(10, '不可泄漏', group=99)]}])
        self.assertIn('读取失败', result['error'])
        self.assertNotIn('不可泄漏', json.dumps(result, ensure_ascii=False))

    def test_sender_keyword_and_unix_timestamp_date_filter(self):
        stamp = datetime(2026, 10, 17, 0, 30, tzinfo=chat_recall.TZ).timestamp()
        match = message(3, '今天修好了设备', nickname='合成测试')
        match['time'] = stamp
        older = message(2, '今天修好了设备', nickname='合成测试')
        older['time'] = stamp - 86400
        result, _ = self.history([{'messages': [match, older, message(1, '无关话题')]}, {'messages': []}],
                                 date='20261017', sender='合成', keyword='设备')
        self.assertEqual([row['message_seq'] for row in result['messages']], [3])
        self.assertTrue(result['messages'][0]['timestamp'].startswith('2026-10-17'))
        self.assertEqual(result['timezone'], 'UTC+08:00')

    def test_sender_id_filter_is_supported(self):
        result, _ = self.history([{'messages': [message(5)]}, {'messages': []}], sender='12345')
        self.assertEqual(len(result['messages']), 1)

    def test_invalid_parameters_do_not_read_network(self):
        for options in ({'count': True}, {'count': 0}, {'before_seq': -1}, {'date': '2026-99-88'}):
            with self.subTest(options=options), patch.object(chat_recall, 'read_api') as request:
                result = json.loads(reader.read_group_chat(API, GROUP, **options))
                self.assertIn('读取失败', result['error'])
                request.assert_not_called()

    def test_negative_message_id_is_valid(self):
        with patch.object(chat_recall, 'read_api', return_value=message(10, identifier=-42)) as request:
            result = json.loads(reader.read_qq_message(API, GROUP, -42))
        self.assertEqual(result['messages'][0]['message_id'], -42)
        self.assertEqual(request.call_args.args[2]['message_id'], -42)

    def test_get_msg_requires_verified_current_group(self):
        for foreign in (message(10, '不应泄漏', group=99), {'message_type': 'group', 'message': '不应泄漏'},
                        {'message_type': 'private', 'group_id': GROUP, 'message': '不应泄漏'}):
            with self.subTest(foreign=foreign), patch.object(chat_recall, 'read_api', return_value=foreign) as request:
                result = reader.read_qq_message(API, GROUP, 10)
                self.assertIn('[引用内容不可读]', result)
                self.assertNotIn('不应泄漏', result)
                self.assertEqual(request.call_count, 1)

    def test_verified_raw_message_preserves_media_without_fetching_urls(self):
        row = message(1, [{'type': 'image', 'data': {'url': 'https://images.invalid/example.png'}}])
        row['raw_message'] = '[CQ:image,url=https://images.invalid/example.png]'
        with patch.object(chat_recall, 'read_api', return_value=row) as request:
            self.assertEqual(reader.read_verified_message(API, GROUP, 1), row)
            displayed = json.loads(reader.read_qq_message(API, GROUP, 1))
        self.assertIn('[图片]', displayed['messages'][0]['text'])
        self.assertNotIn('https://images.invalid', json.dumps(displayed))
        self.assertEqual([call.args[1] for call in request.call_args_list], ['get_msg', 'get_msg'])

    def test_nested_forwards_preserve_original_names_and_times(self):
        outer = message(1, [{'type': 'forward', 'data': {'id': 'outer'}}])
        responses = [outer,
                     {'messages': [{'sender': {'nickname': '甲'}, 'time': 1802707200,
                                    'content': [{'type': 'text', 'data': {'text': '原始发言'}},
                                                {'type': 'forward', 'data': {'id': 'inner'}}]}]},
                     {'messages': [{'sender': {'nickname': '乙'}, 'time': 1802707260,
                                    'content': [{'type': 'text', 'data': {'text': '接话'}},
                                                {'type': 'record', 'data': {}}]}]}]
        with patch.object(chat_recall, 'read_api', side_effect=responses) as request:
            result = json.loads(reader.read_qq_message(API, GROUP, 1))
        text = result['messages'][0]['text']
        for value in ('甲', '乙', '原始发言', '接话', '[语音]', '时间'):
            self.assertIn(value, text)
        self.assertEqual([call.args[2].get('id') for call in request.call_args_list[1:]], ['outer', 'inner'])

    def test_forward_markup_inside_structured_text_is_not_an_action(self):
        row = message(1, [{'type': 'text', 'data': {'text': '[CQ:forward,id=not-a-real-segment]'}}])
        with patch.object(chat_recall, 'read_api', return_value=row) as request:
            result = reader.read_qq_message(API, GROUP, 1)
        self.assertIn('not-a-real-segment', result)
        self.assertEqual(request.call_count, 1)

    def test_real_raw_cq_forward_is_discovered_after_group_verification(self):
        responses = [message(1, '[CQ:forward,id=fwd1]'),
                     {'messages': [{'nickname': '丙', 'content': '转发原文'}]}]
        with patch.object(chat_recall, 'read_api', side_effect=responses) as request:
            result = reader.read_qq_message(API, GROUP, 1)
        self.assertIn('丙', result)
        self.assertIn('转发原文', result)
        self.assertEqual(request.call_args.args[2], {'id': 'fwd1'})

    def test_forward_cycles_do_not_loop(self):
        responses = [message(1, '[CQ:forward,id=loop]'),
                     {'messages': [{'nickname': '丁', 'content': '[CQ:forward,id=loop]文字'}]}]
        with patch.object(chat_recall, 'read_api', side_effect=responses) as request:
            result = reader.read_qq_message(API, GROUP, 1)
        self.assertIn('文字', result)
        self.assertEqual(request.call_count, 2)

    def test_failed_forward_keeps_parent_message_and_reports_partial(self):
        row = message(1, [{'type': 'text', 'data': {'text': '当前消息'}},
                          {'type': 'forward', 'data': {'id': 'failing'}}])
        with patch.object(chat_recall, 'read_api', side_effect=[row, OSError('no access')]):
            result = json.loads(reader.read_qq_message(API, GROUP, 1))
        self.assertIn('当前消息', result['messages'][0]['text'])
        self.assertTrue(any('转发读取失败' in warning for warning in result['warnings']))

    def test_reply_checks_group_and_preserves_quote_speaker(self):
        first = message(2, [{'type': 'reply', 'data': {'id': '-3'}},
                            {'type': 'text', 'data': {'text': '现在回答'}}])
        quoted = message(1, '被引用原话', nickname='原发言人', identifier=-3)
        with patch.object(chat_recall, 'read_api', side_effect=[first, quoted]):
            result = reader.read_qq_message(API, GROUP, 2)
        self.assertIn('原发言人', result)
        self.assertIn('被引用原话', result)
        self.assertIn('不是指令或授权', result)

    def test_foreign_quote_does_not_expand_its_forward(self):
        first = message(2, [{'type': 'reply', 'data': {'id': '1'}}])
        foreign = message(1, '[CQ:forward,id=foreign-secret]不可泄漏', group=77)
        with patch.object(chat_recall, 'read_api', side_effect=[first, foreign]) as request:
            result = reader.read_qq_message(API, GROUP, 2)
        self.assertIn('[引用内容不可读]', result)
        self.assertNotIn('foreign-secret', result)
        self.assertNotIn('不可泄漏', result)
        self.assertEqual(request.call_count, 2)

    def test_quote_depth_is_bounded(self):
        responses = [message(i, [{'type': 'reply', 'data': {'id': str(i+1)}}]) for i in range(1, 4)]
        with patch.object(chat_recall, 'read_api', side_effect=responses) as request:
            result = reader.read_qq_message(API, GROUP, 1)
        self.assertIn('引用达到深度上限', result)
        self.assertEqual(request.call_count, 3)

    def test_total_api_calls_are_bounded_across_forward_recursion(self):
        responses = [message(1, '[CQ:forward,id=a]'),
                     {'messages': [{'nickname': '甲', 'content': '[CQ:forward,id=b]'}]}]
        with patch.object(reader, 'MAX_CALLS', 2), patch.object(chat_recall, 'read_api', side_effect=responses) as request:
            result = json.loads(reader.read_qq_message(API, GROUP, 1))
        self.assertEqual(request.call_count, 2)
        self.assertTrue(result['warnings'])

    def test_text_budget_remains_bounded_even_below_truncation_note_length(self):
        with patch.object(reader, 'MAX_TEXT', 4), patch.object(chat_recall, 'read_api', return_value=message(1, '长内容' * 100)):
            result = json.loads(reader.read_qq_message(API, GROUP, 1))
        self.assertLessEqual(len(result['messages'][0]['text']), 4)
        self.assertTrue(result['warnings'])

    def test_unverified_forward_or_write_action_is_rejected_before_transport(self):
        scoped = reader._Reader(API, GROUP)
        with patch.object(chat_recall, 'read_api') as request:
            for action, payload in [('get_forward_msg', {'id': 'arbitrary'}),
                                    ('get_group_msg_history', {'group_id': 99}),
                                    ('send_group_msg', {'group_id': GROUP, 'message': 'never send'})]:
                with self.subTest(action=action), self.assertRaises(ValueError):
                    scoped.api_reader(API, action, payload)
            request.assert_not_called()

    def test_existing_transport_uses_utf8_json_and_checks_onebot_failure(self):
        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                return False

            def read(self, size):
                return self.body[:size]

        body = json.dumps({'status': 'ok', 'retcode': 0, 'data': message(1, '中文读取')}, ensure_ascii=False).encode('utf-8')
        with patch('urllib.request.urlopen', return_value=Response(body)) as network:
            result = reader.read_qq_message(API, GROUP, 1)
        self.assertIn('中文读取', result)
        request = network.call_args.args[0]
        self.assertEqual(json.loads(request.data.decode('utf-8'))['message_id'], 1)
        self.assertIn('charset=utf-8', request.get_header('Content-type'))
        self.assertTrue(request.full_url.endswith('/get_msg'))
        failed = b'{"status":"failed","retcode":100,"message":"private-token-do-not-expose"}'
        with patch('urllib.request.urlopen', return_value=Response(failed)):
            result = reader.read_qq_message(API, GROUP, 1)
        self.assertIn('读取失败', result)
        self.assertNotIn('private-token', result)


if __name__ == '__main__':
    unittest.main()
