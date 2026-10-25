"""New persona aliases retain the existing social and safety gates offline."""
import base64
import json
import tempfile
import unittest
from pathlib import Path

from chat_recall import clean
from conversation_turns import _message_parts
from sanae_identity import BOT_NAME, SANAE_QQ
from social_feedback import FeedbackStore
from social_lite import SocialLite
from sticker_catalog import StickerCatalog


ALIASES = ('八千代', '月见八千代', '月見ヤチヨ', 'ヤチヨ',
           'Yachiyo', 'yachiyo', 'YACHIYO8000', '早苗', '東風谷 早苗', '东风谷早苗')
GROUP = 1001
PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jQ1sAAAAASUVORK5CYII=')


def event(text):
    return {'group_id': GROUP, 'user_id': '2001', 'self_id': SANAE_QQ,
            'raw_message': text, 'sender': {'nickname': '测试员'}}


class YachiyoAliasTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_new_and_old_names_remain_direct_social_calls(self):
        social = SocialLite(self.root / 'social.json', self.root / 'activity.log',
                            ambient_reply_weight=0, spontaneous_probability=0)
        for alias in ALIASES:
            with self.subTest(alias=alias):
                current = event(alias + '，来接个梗')
                self.assertTrue(social.is_explicit_interaction(current))
                ok, reason = social.should_reply(current)
                self.assertTrue(ok)
                self.assertIn('direct', reason)
        self.assertFalse(social.is_explicit_interaction(event('刚才提到了八千代')))
        self.assertFalse(social.is_explicit_interaction(event('YachiyoBot，来接个梗')))
        self.assertFalse(social.is_explicit_interaction(event('[CQ:at,qq=12345] 来接个梗')))
        self.assertTrue(social.is_explicit_interaction(event(f'[CQ:at,qq={SANAE_QQ}] 来接个梗')))
        self.assertFalse(social.should_reply(event('!问 你好'))[0])

    def test_new_outgoing_display_keeps_existing_account(self):
        social = SocialLite(self.root / 'social.json', self.root / 'activity.log')
        social.record_outgoing(GROUP, '这句我接住了。')
        item = social._group(GROUP)['recent'][-1]
        self.assertEqual(item['sender'], BOT_NAME)
        self.assertEqual(item['userId'], SANAE_QQ)
        self.assertTrue(item['isSelf'])

    def test_turn_cleaning_and_command_exclusion_use_all_aliases(self):
        for alias in ALIASES:
            with self.subTest(alias=alias):
                self.assertEqual(_message_parts(event(alias + '，接个梗')), ('接个梗', ''))
                self.assertEqual(_message_parts(event('@' + alias + '，接个梗')), ('接个梗', ''))
                self.assertIsNone(_message_parts(event(alias + '，!问 你好')))
                self.assertIsNone(_message_parts(event(alias)))
        self.assertEqual(_message_parts(event('YachiyoBot，接个梗')), ('YachiyoBot，接个梗', ''))

    def test_style_requests_keep_scope_and_capture_groups(self):
        store = FeedbackStore(self.root / 'feedback.json', clock=lambda: 1_800_000_000, group_id=GROUP)
        for i, alias in enumerate(ALIASES, 1):
            with self.subTest(alias=alias):
                uid = str(i)
                row = store.observe_style_request(alias + '，今天别损我，认真一点', uid, group_id=GROUP)
                self.assertTrue(row['gentle'])
                self.assertTrue(row['serious'])
                self.assertGreater(row['expiresAt'] - row['time'], store.REQUEST_TTL)
                self.assertEqual(store.style_override(uid, group_id=GROUP), 'gentle_serious')
        self.assertIsNone(store.observe_style_request('他说八千代，今天别损我', '99', group_id=GROUP))
        self.assertIsNone(store.observe_style_request('八千代，认真一点', '99', group_id=123))
        self.assertIsNone(store.observe_style_request('YachiyoBot，认真一点', '99', group_id=GROUP))

    def test_recall_strips_new_and_old_aliases_only_at_start(self):
        for alias in ALIASES:
            with self.subTest(alias=alias):
                for prefix in (alias, '@' + alias):
                    self.assertEqual(clean(prefix + '，总结最近群聊'), '总结最近群聊')
        self.assertEqual(clean('!问 总结最近群聊'), '总结最近群聊')
        self.assertEqual(clean('关于八千代的讨论'), '关于八千代的讨论')
        self.assertEqual(clean('YachiyoBot，总结最近群聊'), 'YachiyoBot，总结最近群聊')

    def test_sticker_playfulness_accepts_aliases_without_accepting_real_questions(self):
        for alias in ALIASES:
            with self.subTest(alias=alias):
                self.assertTrue(StickerCatalog.allows_auto(alias + '，你为什么这么可爱？', '天生的。'))
                self.assertTrue(StickerCatalog.allows_auto(alias + '，能不能夸我一下？', '当然可以。'))
                self.assertFalse(StickerCatalog.allows_auto(alias + '，为什么一直掉线？', '我来看看。'))
                self.assertFalse(StickerCatalog.allows_auto(alias + '，认真回答，你为什么这么可爱？', '天生的。'))
        self.assertFalse(StickerCatalog.allows_auto('他说八千代，你为什么这么可爱？', '天生的。'))

    def test_praise_by_new_name_can_support_neutral_warm_reply(self):
        (self.root / 'assets').mkdir()
        (self.root / 'assets' / 'happy.png').write_bytes(PNG)
        path = self.root / 'stickers.json'
        path.write_text(json.dumps({'entries': [
            {'id': 'happy', 'file': 'assets/happy.png', 'primary_mood': '开心', 'moods': ['开心'],
             'allow_auto': True, 'semantic_auto_ok': True, 'semantic_confidence': 'high'}
        ]}, ensure_ascii=False), encoding='utf-8')
        catalog = StickerCatalog(path)
        for alias in ALIASES:
            with self.subTest(alias=alias):
                chosen = catalog.pick_for_send('这句话我收下啦。', request=alias + '真可爱')
                self.assertEqual(chosen['id'], 'happy')
        self.assertFalse((self.root / 'selection-history.json').exists())


if __name__ == '__main__':
    unittest.main()
