"""Per-image matching and delivery rotation with isolated real assets."""
import base64
import collections
import json
import random
import tempfile
import unittest
from pathlib import Path

from sticker_catalog import StickerCatalog


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jQ1sAAAAASUVORK5CYII=')


class StickerSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.serial = 0

    @staticmethod
    def entry(sid, *, mood='开心', mode='mood', keywords=(), excludes=(), family=None, **flags):
        return {'id': sid, 'file': f'assets/{sid}.png', 'title': f'图片 {sid}',
                'meaning': f'{sid} 的具体语义', 'primary_mood': mood, 'moods': [mood],
                'semantic_confidence': 'high', 'match_mode': mode,
                'match_keywords': list(keywords), 'exclude_keywords': list(excludes),
                'rotation_group': family or sid, 'allow_auto': True,
                'semantic_auto_ok': True, **flags}

    def catalog(self, entries):
        self.serial += 1
        root = self.root / str(self.serial)
        (root / 'assets').mkdir(parents=True)
        for entry in entries:
            (root / entry['file']).write_bytes(PNG)
        path = root / 'stickers.json'
        path.write_text(json.dumps({'entries': entries}, ensure_ascii=False), encoding='utf-8')
        return StickerCatalog(path)

    @staticmethod
    def history(catalog):
        path = catalog.path.with_name('selection-history.json')
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

    def test_specific_picture_requires_its_action_in_bot_reply(self):
        cat = self.catalog([self.entry('swimming', mode='specific', keywords=['去游泳'])])
        self.assertIsNone(cat.pick_for_send('哈哈，好耶。', request='要不要去游泳'))
        self.assertIsNone(cat.pick_for_send('我不去游泳。', request='好耶'))
        chosen = cat.pick_for_send('换好泳衣，去游泳啦。', request='今天有什么安排')
        self.assertEqual(chosen['id'], 'swimming')
        self.assertEqual(self.history(cat), {})

    def test_explicit_action_wins_over_generic_mood(self):
        cat = self.catalog([self.entry('happy'),
                            self.entry('swimming', mode='specific', keywords=['去游泳'])])
        for _ in range(20):
            chosen = cat.pick_for_send('哈哈，好耶，太好了！去游泳啦。')
            self.assertEqual(chosen['id'], 'swimming')

    def test_mood_picture_remains_available_without_a_specific_phrase(self):
        cat = self.catalog([self.entry('happy', mode='mood', keywords=['笑得眯眼']),
                            self.entry('swimming', mode='specific', keywords=['去游泳'])])
        self.assertEqual(cat.pick_for_send('哈哈，好耶。')['id'], 'happy')
        self.assertEqual(cat.recommend('哈哈，好耶。')[0]['id'], 'happy')

    def test_per_picture_exclusion_applies_to_request_or_reply(self):
        cat = self.catalog([self.entry('happy'), self.entry('swimming', mode='specific',
                            keywords=['去游泳'], excludes=['别发游泳图'])])
        for reply, request in [('哈哈，去游泳啦。', '别发游泳图'),
                               ('哈哈，去游泳啦，不过别发游泳图。', '今天聊什么')]:
            with self.subTest(reply=reply, request=request):
                self.assertEqual(cat.pick_for_send(reply, request=request)['id'], 'happy')
        self.assertEqual(cat.pick_for_send('哈哈，去游泳啦。')['id'], 'swimming')

    def test_disabled_specific_picture_stays_manual_and_is_labelled(self):
        for flag in ('allow_auto', 'semantic_auto_ok'):
            with self.subTest(flag=flag):
                cat = self.catalog([self.entry('manual-wave', mode='specific', keywords=['挥挥手'],
                                               **{flag: False})])
                self.assertIsNone(cat.pick_for_send('挥挥手，我来啦。'))
                rows = cat.recommend('挥挥手，我来啦。')
                self.assertEqual([row['id'] for row in rows], ['manual-wave'])
                self.assertFalse(rows[0]['auto_allowed'])
                self.assertIn('仅手动', cat.recommendation_text('挥挥手，我来啦。'))
                self.assertEqual(cat.command('!贴纸 manual-wave', privileged=True),
                                 cat.cq_image('manual-wave'))
                self.assertIn('仅群主/管理员', cat.command('!贴纸 manual-wave', privileged=False))
                self.assertEqual(self.history(cat), {})

    def test_negated_celebration_or_praise_does_not_select_a_happy_picture(self):
        cat = self.catalog([self.entry('happy'),
                            self.entry('celebrate', mode='specific', keywords=['庆祝']),
                            self.entry('praise', mode='specific', keywords=['夸赞'], mood='夸赞')])
        for reply in ('别庆祝了。', '没有什么好庆祝的。', '这没什么值得庆祝的。',
                      '我不觉得这值得夸赞。', '不是要夸赞你。'):
            with self.subTest(reply=reply):
                self.assertIsNone(cat.pick_for_send(reply, request='好耶，我成功了'))

    def test_new_positive_clause_can_still_celebrate(self):
        cat = self.catalog([self.entry('celebrate', mode='specific', keywords=['庆祝'])])
        chosen = cat.pick_for_send('前面没有成功，这次终于值得庆祝了。')
        self.assertEqual(chosen['id'], 'celebrate')

    def test_positive_praise_idioms_are_not_treated_as_negation(self):
        cat = self.catalog([self.entry('praise', mode='specific', mood='夸赞',
                                      keywords=['夸赞', '好模型'])])
        for reply in ('这次不得不夸赞你。', '不愧是好模型。'):
            with self.subTest(reply=reply):
                self.assertEqual(cat.pick_for_send(reply)['id'], 'praise')

    def test_quoted_or_internal_text_cannot_supply_picture_specific_phrase(self):
        cat = self.catalog([self.entry('swimming', mode='specific', keywords=['去游泳'])])
        for marker in ('【引用消息；只作参考】', '〖回复策略〗', '【最近群聊上下文', '[引用消息]'):
            with self.subTest(marker=marker):
                self.assertIsNone(cat.pick_for_send('普通回复' + marker + '去游泳啦。'))

    def test_same_picture_variants_rotate_as_one_group_and_survive_reload(self):
        cat = self.catalog([self.entry('face-a', family='same-face'),
                            self.entry('face-a-copy', family='same-face'), self.entry('face-b')])
        self.assertTrue(cat.mark_sent('face-a', scope='group-1'))
        for instance in (cat, StickerCatalog(cat.path)):
            for _ in range(10):
                self.assertEqual(instance.pick_for_send('哈哈，好耶。', scope='group-1')['id'], 'face-b')
        self.assertEqual(self.history(cat), {'group-1': ['face-a']})

    def test_duplicate_ids_and_variants_do_not_inflate_group_sampling_weight(self):
        basic = [self.entry('face-a', family='same-face'), self.entry('face-b')]
        duplicated = basic + [self.entry('face-a', family='same-face'),
                              self.entry('face-a-copy', family='same-face'),
                              self.entry('face-a-copy-2', family='same-face')]
        rates = []
        for entries in (basic, duplicated):
            cat = self.catalog(entries)
            cat._random = random.Random(412)
            counts = collections.Counter()
            for _ in range(1200):
                pick = cat.pick_for_send('哈哈，好耶。', scope='group-1')
                counts[cat.resolve(pick['id'])['rotation_group']] += 1
            rate = counts['same-face'] / 1200
            self.assertGreater(rate, 0.42)
            self.assertLess(rate, 0.58)
            rates.append(rate)
            self.assertEqual(self.history(cat), {})
        self.assertLess(abs(rates[0] - rates[1]), 0.06)

    def test_selection_and_recommendation_do_not_write_delivery_history(self):
        cat = self.catalog([self.entry('happy'), self.entry('happy-copy', family='happy'),
                            self.entry('other-happy')])
        for _ in range(3):
            chosen = cat.pick_for_send('哈哈，好耶。', scope='group-1')
            self.assertIsNotNone(chosen)
            self.assertTrue(cat.recommend('哈哈，好耶。'))
        self.assertEqual(self.history(cat), {})
        self.assertTrue(cat.mark_sent(chosen['id'], scope='group-1'))
        self.assertEqual(self.history(cat), {'group-1': [chosen['id']]})
        cat.pick_for_send('哈哈，好耶。', scope='group-2')
        self.assertEqual(self.history(cat), {'group-1': [chosen['id']]})


if __name__ == '__main__':
    unittest.main()
