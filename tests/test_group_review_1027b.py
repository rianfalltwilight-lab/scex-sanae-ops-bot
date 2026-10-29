"""Round 28: the main group from 14:40 to 20:15 on 10-27. Offline.

- 16:49 "分析此图出自什么动漫" quoting a picture went to the chat-history summary, which cannot see images.
- 16:50 "群主今天干了些什么" was answered from chat memory with things the owner never said.
- 16:20 "收到，正在处理，请稍候…" in the middle of banter, before a two-second chat read.
- 18:01 a picture failed with an SSL EOF ("看图失败（连接中断）"); the next one a minute later worked.
- 19:11 a member's "执行服务器清理掉落物" was refused correctly, then sent back for a tool call it did not need.
"""
import copy
import unittest
import urllib.error
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests
from tests.test_server_round8 import text_reply, tool_call
import chat_recall

s = runtime_tests.s
SERVER = {'id': 'legacy', 'prefix': '[怀旧]', 'server_dir': '', 'remote_ssh_host': 'game-host'}


class QuestionTests(unittest.TestCase):
    def test_picture_questions(self):
        for text in ('分析此图出自什么动漫，为什么会变成这样', '这张图是什么', '图里这个角色是谁', '谁画的', '这是哪部番'):
            with self.subTest(text=text):
                self.assertTrue(chat_recall.is_image_question(text))
        for text in ('分析一下这段聊天', '总结一下他们在吵什么', '这个地图怎么走', '分析他的意图'):
            with self.subTest(text=text):
                self.assertFalse(chat_recall.is_image_question(text))

    def test_what_the_owner_did_reads_the_chat(self):
        for text in ('群主今天干了些什么', '管理今天说了什么', '服主最近在忙啥'):
            with self.subTest(text=text):
                self.assertTrue(chat_recall.is_activity_question(text))
                self.assertTrue(chat_recall.is_recall(text))
        self.assertFalse(chat_recall.is_recall('群主为什么不理人'))

    def test_the_sample_names_owner_and_admins(self):
        rows = [{'group_id': 1, 'user_id': 11, 'time': 1000, 'message_id': 1, 'raw_message': 'Apple生态这一块',
                 'sender': {'card': 'FixtureOwner', 'role': 'owner'}},
                {'group_id': 1, 'user_id': 12, 'time': 1001, 'message_id': 2, 'raw_message': '来点',
                 'sender': {'card': '路人', 'role': 'member'}},
                {'group_id': 1, 'user_id': 13, 'time': 1002, 'message_id': 3, 'raw_message': '更新了',
                 'sender': {'card': 'FixtureAdmin', 'role': 'admin'}}]
        context = chat_recall.evidence(rows, 1, 99, '总结一下大家聊了什么', self_id=0, now=2000)['context']
        self.assertIn('"说话人": "FixtureOwner（群主）"', context)
        self.assertIn('"说话人": "FixtureAdmin（管理员）"', context)
        self.assertIn('"说话人": "路人"', context)
        owner = chat_recall.evidence(rows, 1, 99, '群主今天干了些什么', self_id=0, now=2000)
        self.assertIn('只含群主的发言', owner['context'])
        self.assertIn('Apple生态这一块', owner['context'])
        self.assertNotIn('来点', owner['context'])
        self.assertNotIn('更新了', owner['context'])
        admins = chat_recall.evidence(rows, 1, 99, '管理今天说了什么', self_id=0, now=2000)['context']
        self.assertIn('更新了', admins)
        self.assertNotIn('来点', admins)
        nobody = chat_recall.evidence(rows[1:2], 1, 99, '群主今天干了些什么', self_id=0, now=2000)
        self.assertTrue(nobody['direct'].startswith('这次可见的群聊记录里没有群主的文本发言'))


class QuotedPictureTests(unittest.TestCase):
    def test_a_question_about_a_quoted_picture_goes_to_vision(self):
        captured = {}

        def fake_agent(text, user_id, privileged, group_id, interim_cb=None, image_url=None, **kwargs):
            captured.update(text=text, image_url=image_url)
            return '是某部番的截图', mock.Mock(footer=lambda: ''), False
        quoted = '[CQ:image,file=x.png,url=https://cdn.example/quoted.png]'
        raw = '[CQ:reply,id=123][CQ:at,qq=1002] 分析此图出自什么动漫，为什么会变成这样'
        with mock.patch.object(s, '_fetch_reply_raw', return_value=quoted), \
                mock.patch.object(s, '_recall_reply', side_effect=AssertionError('not a chat summary')), \
                mock.patch.object(s, 'run_agent', side_effect=fake_agent):
            out = s.sanae_reply(raw, 'u', privileged=True)
        self.assertEqual('是某部番的截图', out)
        self.assertEqual('https://cdn.example/quoted.png', captured['image_url'])

    def test_analysing_a_quoted_chat_still_summarises(self):
        quoted = '甲：今晚开服吗\n乙：不开'
        raw = '[CQ:reply,id=123][CQ:at,qq=1002] 分析一下这段'
        with mock.patch.object(s, '_fetch_reply_raw', return_value=quoted), \
                mock.patch.object(s, 'run_agent', return_value=('总结', mock.Mock(footer=lambda: ''), False)) as agent:
            s.sanae_reply(raw, 'u', privileged=True)
        self.assertIsNone(agent.call_args.kwargs.get('image_url'))


class VisionRetryTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(s, '_retry_sleep')
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (('_vision_media_url', 'data:image/png;base64,AA=='),
                            ('_download_visual_data_url', 'data:image/png;base64,AA==')):
            patcher = mock.patch.object(s, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_dropped_connection_is_retried_after_a_pause(self):
        eof = urllib.error.URLError('[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol')
        ok = {'choices': [{'message': {'content': '粉发角色'}}]}
        with mock.patch.object(s, '_post', side_effect=[eof, eof, ok]) as post:
            answer, _ = s._vision_reply('这是谁', 'https://cdn.example/x.png')
        self.assertEqual('粉发角色', answer)
        self.assertEqual(3, post.call_count)
        self.assertEqual([mock.call(1.5), mock.call(3)], self.sleep.call_args_list)

    def test_a_rejected_request_is_not_retried_with_a_pause(self):
        bad = urllib.error.HTTPError('https://x', 400, 'bad', {}, None)
        with mock.patch.object(s, '_post', side_effect=[bad, bad]) as post, self.assertRaises(urllib.error.HTTPError):
            s._vision_reply('这是谁', 'https://cdn.example/x.png')
        self.assertEqual(2, post.call_count)        # the existing re-download attempt only
        self.sleep.assert_not_called()


class VisionPayloadTests(unittest.TestCase):
    def test_the_deepseek_vision_answer_does_not_spend_its_budget_thinking(self):
        with mock.patch.multiple(s, VISION_PROVIDER='deepseek'), \
                mock.patch.object(s, '_vision_media_url', return_value='data:image/png;base64,AA=='), \
                mock.patch.object(s, '_post', return_value={'choices': [{'message': {'content': '某部番'}}]}) as post:
            s._vision_reply('出自什么动漫', 'https://cdn.example/x.png')
        self.assertEqual({'type': 'disabled'}, post.call_args.args[2]['thinking'])
        with mock.patch.multiple(s, VISION_PROVIDER='zhipu'), \
                mock.patch.object(s, '_vision_media_url', return_value='data:image/png;base64,AA=='), \
                mock.patch.object(s, '_post', return_value={'choices': [{'message': {'content': '某部番'}}]}) as post:
            s._vision_reply('出自什么动漫', 'https://cdn.example/x.png')
        self.assertNotIn('thinking', post.call_args.args[2])


class LoopTests(unittest.TestCase):
    setUp = runtime_tests.YachiyoPayloadTests.setUp

    def run_turn(self, text, responses, privileged=False, tool_result='结果'):
        sent, replies, interim = [], iter(responses), []

        def model(payload, **kwargs):
            sent.append(copy.deepcopy(payload))
            return next(replies)
        with mock.patch.object(s, '_text_post', side_effect=model), \
                mock.patch.object(s, 'execute_tool', return_value=tool_result) as tool:
            answer, _usage, _used = s.run_agent(text, 'member', privileged, 'fixture-group', interim_cb=interim.append,
                                                selected_server=SERVER, explicit_server=True)
        return answer, sent, tool, interim

    def test_no_progress_notice_for_a_quick_chat_read(self):
        _answer, _sent, _tool, interim = self.run_turn(
            '刚才他们在聊什么', [tool_call('read_recent_chat', {}), text_reply('在聊战地')])
        self.assertEqual([], interim)
        _answer, _sent, _tool, interim = self.run_turn(
            '服务器现在卡不卡', [tool_call('server_world', {'topic': 'lag'}), text_reply('有点卡')])
        self.assertEqual(['收到，正在处理，请稍候…'], interim)

    def test_a_members_change_request_may_be_refused_without_a_tool(self):
        text = '早苗，执行服务器清理掉落物'
        self.assertTrue(s._has_server_query_intent(text))
        self.assertTrue(s._has_operation_request(text))
        answer, sent, tool, _interim = self.run_turn(text, [text_reply('清理掉落物得找管理员来做。')])
        self.assertEqual('清理掉落物得找管理员来做。', answer)
        self.assertEqual(1, len(sent))
        tool.assert_not_called()


if __name__ == '__main__':
    unittest.main()
