#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import base64
import json
import tempfile
import unittest
from pathlib import Path

from sticker_catalog import StickerCatalog


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jQ1sAAAAASUVORK5CYII=')


class StickerCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'assets').mkdir()
        entries = []
        for prefix, mood, count in [('reject', '拒绝', 4), ('happy', '开心', 2),
                                    ('coax', '撒娇', 1), ('sad', '委屈', 1), ('praise', '夸赞', 1)]:
            for index in range(count):
                sid = f'{prefix}-{index}'
                file = f'assets/{sid}.png'
                (self.root / file).write_bytes(PNG)
                entries.append({'id': sid, 'primary_mood': mood, 'moods': [mood],
                                'semantic_confidence': 'high', 'file': file})
        self.path = self.root / 'stickers.json'
        self.path.write_text(json.dumps({'entries': entries}, ensure_ascii=False), encoding='utf-8')
        self.catalog = StickerCatalog(self.path)
        self.history_path = self.root / 'selection-history.json'

    def test_selection_without_delivery_changes_neither_memory_nor_disk(self):
        for _ in range(3):
            chosen = self.catalog.pick_for_send('才不要。', scope='group-1')
            self.assertIsNotNone(chosen)
            self.assertTrue(self.catalog.cq_image(chosen['id']).startswith('[CQ:image,file=file:///'))
        self.assertEqual(self.catalog._recent_picks, {})
        self.assertFalse(self.history_path.exists())
        self.assertEqual(StickerCatalog(self.path)._recent_picks, {})

    def test_mark_sent_persists_only_acknowledged_selection_in_its_scope(self):
        chosen = self.catalog.pick_for_send('才不要。', scope='group-1')
        self.assertTrue(self.catalog.mark_sent(chosen['id'], scope='group-1'))
        saved = self.history_path.read_bytes()
        self.catalog.pick_for_send('才不要。', scope='group-2')
        self.assertEqual(self.history_path.read_bytes(), saved)
        self.assertEqual(StickerCatalog(self.path)._recent_picks, {'group-1': [chosen['id']]})
        self.assertFalse(self.catalog.mark_sent('missing', scope='group-1'))
        self.assertEqual(self.history_path.read_bytes(), saved)

    def test_same_mood_rotates_after_acknowledged_sends(self):
        picks = []
        for _ in range(4):
            chosen = self.catalog.pick_for_send('才不要。', scope='group-1')
            picks.append(chosen['id'])
            self.catalog.mark_sent(chosen['id'], scope='group-1')
        self.assertEqual(4, len(set(picks)))
        fifth = self.catalog.pick_for_send('才不要。', scope='group-1')['id']
        self.assertNotEqual(picks[-1], fifth)

    def test_singleton_can_be_used_again_after_bridge_cooldown(self):
        first = self.catalog.pick_for_send('嘿嘿。', scope='group-1')
        self.catalog.mark_sent(first['id'], scope='group-1')
        self.assertEqual(self.catalog.pick_for_send('嘿嘿。', scope='group-1')['id'], first['id'])

    def test_playful_questions_allow_short_replies_including_real_at(self):
        for request in ('你为什么这么可爱', '早苗，你为什么这么可爱？',
                        '能不能夸我一下', '早苗可不可以夸夸我？',
                        '[CQ:at,qq=1002] 你为什么这么可爱？'):
            with self.subTest(request=request):
                self.assertTrue(self.catalog.allows_auto(request, '天生的。'))

    def test_real_questions_and_mixed_technical_requests_stay_blocked(self):
        for request in ('怎么合成这件装备', '为什么一直掉线', '能不能帮我修一下',
                        '认真回答，你为什么这么可爱', '你为什么这么可爱，顺便解释配置错误',
                        '能不能夸我一下，顺便解释配置错误', '是否需要重启',
                        '[CQ:at,qq=1002] !问 你好', '详细讲一下教程'):
            with self.subTest(request=request):
                self.assertFalse(self.catalog.allows_auto(request, '哈哈，我看看。'))

    def test_reply_length_and_explanatory_format_stay_blocked(self):
        self.assertTrue(self.catalog.allows_auto('今天真开心', '哈' * 180))
        for reply in ('哈' * 181, '哈哈〖' + '这是完整模型正文。' * 30,
                      '```python\nprint(1)\n```', '1. 第一步\n2. 第二步',
                      '- 先这样\n- 再这样', '建议先检查配置', ''):
            with self.subTest(reply=reply[:20]):
                self.assertFalse(self.catalog.allows_auto('今天真开心', reply))

    def test_current_playful_request_can_support_neutral_warm_answer(self):
        pairs = [('你为什么这么可爱', '天生的，收下这份可爱。', '开心'),
                 ('能不能夸我一下', '这次做得很漂亮。', '夸赞'),
                 ('好耶，成功了', '这份努力有回报啦。', '开心')]
        for request, reply, mood in pairs:
            with self.subTest(request=request):
                chosen = self.catalog.pick_for_send(reply, request=request)
                self.assertEqual(chosen['mood'], mood)

    def test_answer_mood_wins_and_request_mood_is_not_copied(self):
        chosen = self.catalog.pick_for_send('才不要。', request='能不能夸我一下')
        self.assertEqual(chosen['mood'], '拒绝')
        self.assertIsNone(self.catalog.pick_for_send('现在别这样。', request='哈哈，成功了'))
        self.assertIsNone(self.catalog.pick_for_send('没有什么好庆祝的。', request='哈哈，成功了'))
        self.assertIsNone(self.catalog.pick_for_send('我听到了。', request='你不太聪明'))
        self.assertIsNone(self.catalog.pick_for_send('我听到了。', request='真是气死我了'))
        self.assertIsNone(self.catalog.pick_for_send('普通消息'))

    def test_distress_never_selects_a_happy_or_mocking_image(self):
        for request in ('我今天很难过', '我不开心，想哭'):
            for reply in ('我陪你待一会儿。', '哈哈，先歇一下。'):
                self.assertFalse(self.catalog.allows_auto(request, reply))
                self.assertIsNone(self.catalog.pick_for_send(reply, request=request))

    def test_injected_history_and_internal_hints_do_not_supply_moods(self):
        for marker in ('【最近群聊上下文', '【引用消息；只作参考】', '〖回复策略〗', '[引用消息]'):
            with self.subTest(marker=marker):
                self.assertEqual(self.catalog.recommend('普通消息' + marker + '哈哈，好耶'), [])
                self.assertIsNone(self.catalog.pick_for_send('我听到了。', request='在吗' + marker + '哈哈'))
                self.assertIsNone(self.catalog.pick_for_send('普通消息' + marker + '嘿嘿'))
                self.assertTrue(self.catalog.allows_auto('你为什么这么可爱' + marker + '解释配置', '天生的。'))
                chosen = self.catalog.pick_for_send('这份努力有回报啦。', request='好耶' + marker + '我很难过')
                self.assertEqual(chosen['mood'], '开心')

    def test_missing_assets_cannot_be_selected_or_recorded_by_selection(self):
        for path in (self.root / 'assets').glob('*.png'):
            path.unlink()
        self.assertIsNone(self.catalog.pick_for_send('哈哈，好耶'))
        self.assertEqual(self.catalog._recent_picks, {})
        self.assertFalse(self.history_path.exists())

    def test_manual_permissions_and_recommendation_do_not_record_a_send(self):
        self.assertIn('仅群主/管理员', self.catalog.command('!贴纸 happy-0', privileged=False))
        self.assertEqual(self.catalog.command('!贴纸 happy-0', privileged=True), self.catalog.cq_image('happy-0'))
        self.assertIn('仅推荐', self.catalog.command('!贴纸推荐 好耶', privileged=True))
        self.assertEqual(self.catalog._recent_picks, {})
        self.assertFalse(self.history_path.exists())


if __name__ == '__main__':
    unittest.main()
