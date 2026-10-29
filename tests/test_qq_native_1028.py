"""Round 32 (owner 10-28): group honours, albums (with photo descriptions), who is muted, and
saving quoted pictures into an album (owner/admins). Offline, with a fake OneBot."""
import unittest
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests
from tests.test_qq_interact_1027 import BOT, GID, OWNER, ADMIN, SLE, LOLI, FakeActions
import qq_interact

s = runtime_tests.s
ALBUM = 'V63SaxQ83ELwWd3qHxbQ3bCBnX4D44rL'


def photo(width_urls):
    return {'photo_url': [{'spec': str(i), 'url': {'url': url, 'width': str(width), 'height': '1'}}
                          for i, (width, url) in enumerate(width_urls)]}


class NativeActions(FakeActions):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.shut = []
        self.messages = {555: {'group_id': GID, 'message': [
            {'type': 'text', 'data': {'text': '配方'}},
            {'type': 'image', 'data': {'url': 'https://multimedia.nt.qq.com.cn/download?a=1&amp;rkey=x'}},
            {'type': 'image', 'data': {'url': 'https://multimedia.nt.qq.com.cn/download?a=2'}}]},
            556: {'group_id': GID, 'message': [{'type': 'text', 'data': {'text': '没图'}}]}}

    def call(self, action, params):
        self.calls.append((action, params))
        if action == 'get_group_honor_info':
            return {'current_talkative': {'user_id': SLE, 'nickname': 'FixtureMember', 'day_count': '1'},
                    'talkative_list': [{'user_id': SLE, 'nickname': 'FixtureMember', 'description': '6天，最长蝉联2天'},
                                       {'user_id': LOLI, 'nickname': '自称群主的成员', 'description': '497天，最长蝉联47天'}],
                    'performer_list': [{'user_id': LOLI, 'nickname': '自称群主的成员', 'description': '连续发消息26天'}],
                    'legend_list': [{'user_id': '2013', 'nickname': '\u3000\u3000\u3000', 'description': '连续发消息49天'}],
                    'strong_newbie_list': [], 'emotion_list': []}
        if action == 'get_group_album_list':
            return [{'album_id': ALBUM, 'name': '往昔之旅余烬炼金配方', 'upload_number': '2', 'last_upload_time': '1755611082',
                     'creator': {'nick': '自称群主的成员', 'uin': LOLI}},
                    {'album_id': 'B', 'name': 'GTMH仙人系列', 'upload_number': '0', 'last_upload_time': '1751205382',
                     'creator': {'nick': '自称群主的成员', 'uin': LOLI}},
                    {'album_id': 'C', 'name': 'GTMH瓦尔基里', 'upload_number': '0', 'creator': {}}]
        if action == 'get_group_album_media_list':
            return {'media_list': [
                {'image': photo([(200, 'https://p/small1'), (400, 'https://p/mid1'), (2560, 'https://p/big1')]),
                 'uploader': LOLI, 'upload_time': '1754143534', 'desc': ''},
                {'image': photo([(400, 'https://p/mid2')]), 'uploader': LOLI, 'upload_time': '1755611080', 'desc': '新配方'}]}
        if action == 'get_group_shut_list':
            return self.shut
        if action == 'get_msg':
            return self.messages.get(params.get('message_id'))
        return None


class Base(unittest.TestCase):
    def make(self):
        self.now = [1793100000.0]
        self.qq = NativeActions()
        return qq_interact.QQInteract(self.qq, BOT, clock=lambda: self.now[0])


class HonorTests(Base):
    def test_overview_and_one_person(self):
        qq = self.make()
        text = qq.run('group_info', {'topic': 'honor'}, GID, SLE)
        self.assertIn('当前龙王：FixtureMember（连续 1 天）', text)
        self.assertIn('历史龙王，共 2 人，前 2：FixtureMember 6天，最长蝉联2天；自称群主的成员 497天，最长蝉联47天', text)
        self.assertIn('群聊炽焰（连续发言更久），共 1 人，前 1：QQ 2013 连续发消息49天', text)
        self.assertNotIn('冒尖小春笋', text)
        mine = qq.run('group_info', {'topic': 'honor', 'query': '我'}, GID, LOLI)
        self.assertIn('自称群主的成员：历史龙王：497天，最长蝉联47天；群聊之火（连续发言）：连续发消息26天', mine)
        sle = qq.run('group_info', {'topic': 'honor', 'query': 'FixtureMember'}, GID, OWNER)
        self.assertIn('现在就是龙王（连续 1 天）', sle)
        self.assertEqual(1, sum(1 for action, _ in self.qq.calls if action == 'get_group_honor_info'))


class AlbumTests(Base):
    def test_list_and_photos(self):
        qq = self.make()
        text = qq.run('group_info', {'topic': 'albums'}, GID, SLE)
        self.assertIn('共 3 个相册', text)
        self.assertIn('- 往昔之旅余烬炼金配方：2 张，自称群主的成员 建的，最后上传 2025-08-19', text)
        media = qq.run('group_info', {'topic': 'albums', 'query': '炼金'}, GID, SLE)
        self.assertIn('相册「往昔之旅余烬炼金配方」：2 张', media)
        self.assertLess(media.index('2025-08-19 自称群主的成员 上传，说明：新配方'), media.index('2025-08-02 自称群主的成员 上传'))
        self.assertIn('带上 look=true', media)
        # Replay 10-28: "自称群主的成员 上传" was retold as "群主上传的".
        self.assertIn('“自称群主的成员” 只是群名片，不是群主或管理员', media)
        self.assertIn('没有名字里带“不存在”的相册', qq.run('group_info', {'topic': 'albums', 'query': '不存在'}, GID, SLE))

    def test_looking_at_photos(self):
        qq = self.make()
        self.assertIn('现在看不了照片内容', qq.run('group_info', {'topic': 'albums', 'query': '炼金', 'look': True}, GID, SLE))
        seen = []
        qq.describe = lambda urls: seen.extend(urls) or ['祭坛上有金色雕像', '橙色火焰']
        text = qq.run('group_info', {'topic': 'albums', 'query': '炼金', 'look': True}, GID, SLE)
        self.assertEqual(['https://p/mid2', 'https://p/mid1'], seen)      # newest first, a size up to 1280 wide
        self.assertIn('第 1 张：祭坛上有金色雕像', text)
        qq.describe = mock.Mock(side_effect=TimeoutError())
        self.assertIn('照片没能看成：TimeoutError', qq.run('group_info', {'topic': 'albums', 'query': '炼金', 'look': True}, GID, SLE))


class MutedTests(Base):
    def test_members_learn_only_their_own(self):
        qq = self.make()
        self.qq.shut = [{'uin': SLE, 'cardName': 'FixtureMember', 'shutUpTime': int(self.now[0]) + 125},
                        {'uin': LOLI, 'cardName': '自称群主的成员', 'shutUpTime': int(self.now[0]) + 3 * 3600}]
        self.assertIn('你现在被禁言，还剩约 3 分钟', qq.run('group_info', {'topic': 'muted'}, GID, SLE))
        other = qq.run('group_info', {'topic': 'muted'}, GID, '2905')
        self.assertIn('你现在没有被禁言', other)
        self.assertNotIn('自称', other)
        full = qq.run('group_info', {'topic': 'muted'}, GID, ADMIN)
        self.assertIn('本群现在有 2 人被禁言：FixtureMember（QQ 2003）还剩约 3 分钟；自称群主的成员（QQ 2904）还剩约 3 小时 0 分钟', full)
        self.qq.shut = []
        self.assertEqual('本群现在没有人被禁言。', qq.run('group_info', {'topic': 'muted'}, GID, OWNER))


class SaveTests(Base):
    def test_owner_and_admins_save_the_quoted_pictures(self):
        qq = self.make()
        self.assertEqual('拒绝：只有群主和管理员能让我往群相册里存图。',
                         qq.run('save_to_album', {'album': '炼金'}, GID, SLE, turn={'reply_id': '555'}))
        result = qq.run('save_to_album', {'album': '炼金'}, GID, ADMIN, turn={'reply_id': '555'})
        self.assertEqual('已把 2 张图存进相册「往昔之旅余烬炼金配方」。', result)
        upload = [params for action, params in self.qq.calls if action == 'upload_group_album']
        self.assertEqual([{'group_id': GID, 'album_id': ALBUM, 'files': [
            'https://multimedia.nt.qq.com.cn/download?a=1&rkey=x', 'https://multimedia.nt.qq.com.cn/download?a=2']}], upload)

    def test_unclear_requests_write_nothing(self):
        qq = self.make()
        self.assertIn('引用那条带图的消息', qq.run('save_to_album', {'album': '炼金'}, GID, OWNER, turn={}))
        self.assertIn('没有图片', qq.run('save_to_album', {'album': '炼金'}, GID, OWNER, turn={'reply_id': '556'}))
        self.assertIn('“GTMH”对得上好几个相册', qq.run('save_to_album', {'album': 'GTMH'}, GID, OWNER, turn={'reply_id': '555'}))
        self.assertIn('新建相册我做不了', qq.run('save_to_album', {'album': '截图'}, GID, OWNER, turn={'reply_id': '555'}))
        self.assertFalse([1 for action, _ in self.qq.calls if action == 'upload_group_album'])


class WiringTests(unittest.TestCase):
    def test_routing(self):
        for text in ('现在龙王是谁', '我是不是群聊之火', '群相册里有什么', '我禁言还剩多久', '谁被禁言了', '还要多久解禁'):
            with self.subTest(text=text):
                self.assertIn('group_info', s._qq_action_request(text))
        self.assertNotIn('group_mute', s._qq_action_request('我禁言还剩多久'))
        self.assertIn('group_mute', s._qq_action_request('把他禁言十分钟'))
        self.assertEqual({'save_to_album'}, set(s._qq_action_request('把这张存进炼金相册', has_quote=True)))
        self.assertEqual(set(), set(s._qq_action_request('把这张存进炼金相册')))

    def test_tools(self):
        member = {tool['function']['name'] for tool in s.TOOLS_MEMBER}
        admin = {tool['function']['name'] for tool in s.TOOLS_ADMIN}
        self.assertNotIn('save_to_album', member)
        self.assertIn('save_to_album', admin)
        self.assertIn('save_to_album', qq_interact.ADMIN_TOOL_NAMES)
        topics = s.GROUP_INFO['function']['parameters']['properties']['topic']['enum']
        self.assertEqual(list(qq_interact.GROUP_INFO_TOPICS), topics)

    def test_describe_images(self):
        with mock.patch.object(s, '_vision_media_url', side_effect=lambda url: 'data:' + url), \
                mock.patch.object(s, '_observe_images', return_value=('祭坛\n火焰', {})) as observe:
            self.assertEqual(['祭坛', '火焰'], s.describe_images(['a', 'b']))
        self.assertTrue(observe.call_args.kwargs['brief'])
        self.assertEqual([], s.describe_images([]))


if __name__ == '__main__':
    unittest.main()
