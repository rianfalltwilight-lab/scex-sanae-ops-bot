#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path

from sticker_catalog import StickerCatalog


class StickerRotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        manifest = {
            'entries': [
                {'id': f'reject-{index}', 'primary_mood': '拒绝',
                 'moods': ['拒绝', '无语'], 'semantic_confidence': 'high',
                 'file': f'assets/reject-{index}.jpg'}
                for index in range(4)
            ]
        }
        self.path = self.root / 'stickers.json'
        self.path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        self.catalog = StickerCatalog(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_same_mood_rotates_without_immediate_repeats(self):
        picks = [self.catalog.pick_for_send('不行，拒绝', scope='group-1')['id']
                 for _ in range(4)]
        self.assertEqual(4, len(set(picks)))
        fifth = self.catalog.pick_for_send('不行，拒绝', scope='group-1')['id']
        self.assertNotEqual(picks[-1], fifth)

    def test_unknown_semantics_do_not_force_a_sticker(self):
        self.assertIsNone(self.catalog.pick_for_send('普通消息', scope='group-1'))


if __name__ == '__main__':
    unittest.main()
