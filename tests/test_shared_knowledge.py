import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)) )
sys.path.insert(0, HERE)
from shared_knowledge import SharedKnowledge


class SharedKnowledgeTests(unittest.TestCase):
    def test_persist_search_and_delete(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'knowledge.jsonl')
            store = SharedKnowledge(path)
            ok, _ = store.remember('物品名称', '这是一个通用结论')
            self.assertTrue(ok)
            self.assertEqual(len(store.search('物品名称')), 1)
            restored = SharedKnowledge(path)
            self.assertEqual(restored.status()['entries'], 1)
            self.assertEqual(restored.delete('物品名称'), 1)
            self.assertEqual(restored.search('物品名称'), [])

    def test_sensitive_content_is_rejected(self):
        store = SharedKnowledge('/tmp/not-used-knowledge.jsonl')
        ok, _ = store.remember('config/server.properties', 'secret')
        self.assertFalse(ok)

    def test_media_fingerprint_match(self):
        with tempfile.TemporaryDirectory() as td:
            store = SharedKnowledge(os.path.join(td, 'k.jsonl'))
            ok, _ = store.remember('歌曲', '正确歌名', ['a' * 64])
            self.assertTrue(ok)
            self.assertEqual(store.search('', ['a' * 64])[0]['conclusion'], '正确歌名')


if __name__ == '__main__':
    unittest.main()
