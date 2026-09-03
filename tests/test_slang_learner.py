#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from slang_learner import SlangLearner


class SlangLearnerTests(unittest.TestCase):
    def make(self):
        root = Path(tempfile.mkdtemp(prefix='slang-', dir=str(Path(__file__).resolve().parents[1])))
        return SlangLearner(root / 'slang.json', group_id=1001)

    def event(self, text, group_id=1001):
        return {'group_id': group_id, 'user_id': '10001', 'raw_message': text,
                'sender': {'nickname': '测试员'}}

    def test_only_approved_group_is_observed(self):
        learner = self.make()
        with mock.patch.object(learner, '_save'):
            learner.observe(self.event('抽象梗'))
            learner.observe(self.event('抽象梗'))
            learner.observe(self.event('别的群黑话', 1005))
        self.assertTrue(any(x['term'] == '抽象梗' for x in learner._entries))
        self.assertFalse(any(x['term'] == '别的群黑话' for x in learner._entries))

    def test_confirmed_terms_only_enter_context(self):
        learner = self.make()
        with mock.patch.object(learner, '_save'):
            learner.observe(self.event('yyds'))
            self.assertNotIn('yyds', learner.context())
            self.assertTrue(learner.confirm('yyds', '永远的神'))
        self.assertIn('yyds：永远的神', learner.context())
        self.assertIn('已确认并记住', learner.command('!黑话确认 yyds = 永远的神', True))

    def test_admin_gate_and_reject(self):
        learner = self.make()
        self.assertIn('管理员', learner.command('!黑话候选', False))
        with mock.patch.object(learner, '_save'):
            learner.observe(self.event('神秘梗'))
            self.assertTrue(learner.reject('神秘梗'))
        self.assertNotIn('神秘梗', learner.context())


if __name__ == '__main__':
    unittest.main()
