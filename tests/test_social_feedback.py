#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from social_feedback import FeedbackStore


class FeedbackTests(unittest.TestCase):
    def make(self):
        root = Path(tempfile.mkdtemp(prefix='feedback-', dir=str(Path(__file__).resolve().parents[1])))
        return FeedbackStore(root / 'feedback.json')

    def test_group_and_admin_gates(self):
        store = self.make()
        self.assertIsNone(store.command('普通消息', '1'))
        self.assertIn('\u4ec5\u7ba1\u7406\u5458', store.command('!反馈 统计', '1', privileged=False))

    def test_record_and_summary(self):
        store = self.make()
        with mock.patch.object(store, '_save'):
            self.assertIsNotNone(store.command('!反馈 赞 回复不错', '1'))
            self.assertIsNotNone(store.command('!反馈 踩 太长了', '2'))
        self.assertEqual(store.summary(), {'positive': 1, 'negative': 1, 'total': 2})


if __name__ == '__main__':
    unittest.main()
