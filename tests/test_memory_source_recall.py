import copy
import json
import tempfile
import unittest
from pathlib import Path

from conversation_memory import ConversationMemory


class SourceRecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "memory.json"
        self.now = 1_800_000_000
        self.mem = ConversationMemory(self.path, (100,), (999,), lambda: self.now)
        self.serial = 0

    def store(self, rows, summary):
        ids = []
        for uid, text in rows:
            self.serial += 1
            self.assertTrue(self.mem.observe({
                "post_type": "message", "message_type": "group", "group_id": 100,
                "user_id": uid, "self_id": 999, "message_id": self.serial,
                "time": self.now, "raw_message": text}))
            ids.append(str(self.serial))
        for _ in range(24 - len(rows)):
            self.serial += 1
            self.mem.observe({"post_type": "message", "message_type": "group",
                "group_id": 100, "user_id": 8, "self_id": 999,
                "message_id": self.serial, "time": self.now, "raw_message": "今天路过"})
        batch = self.mem.prepare_batch(100)
        self.assertIsNotNone(batch)
        self.assertTrue(self.mem.complete_batch(batch, {"summaries": [
            {"summary": summary, "source_ids": ids}]}))
        self.now += 600
        return ids

    def test_mixed_authors_cannot_transfer_topic_or_summary_claim(self):
        summary = "有人改善识图，并询问奇点是否有动力API"
        self.store([(1, "刚才改善早苗识图能力"),
                    (2, "说起来奇点有和动力有关的API不")], summary)
        before = self.path.read_bytes()
        self.assertEqual(self.mem.context(100, 1, "奇点API"), "")
        first = self.mem.context(100, 1, "之前说过什么")
        second = self.mem.context(100, 2, "奇点API")
        self.assertIn("改善早苗识图能力", first)
        self.assertNotIn("动力", first)
        self.assertNotIn(summary, first + second)
        self.assertIn("用户 2", second)
        self.assertIn("奇点有和动力有关的API不", second)
        self.assertNotIn("改善早苗识图能力", second)
        self.assertEqual(self.path.read_bytes(), before)

    def test_uncertainty_and_sleep_are_separate_people(self):
        self.store([(1, "不知道"), (2, "眠了，明天折磨模型去了")],
                   "有人表示不清楚，并说眠了、明天折磨模型")
        context = self.mem.context(100, 1, "还记得我吗")
        self.assertIn('原话="不知道"', context)
        self.assertNotIn("眠了", context)
        self.assertNotIn("模型", context)

    def test_relevant_source_after_first_three_is_retrieved(self):
        self.store([(2, "下午晴天"), (2, "晚上吃饭"), (2, "天气凉快"),
                    (1, "磁悬浮项目还没开始")], "群友聊了一些事情")
        context = self.mem.context(100, 1, "磁悬浮项目")
        self.assertIn('原话="磁悬浮项目还没开始"', context)
        self.assertNotIn("用户 2", context)

    def test_late_negation_kept_and_budget_skips_whole_source(self):
        text = "咖啡品鉴：" + "这里讨论的是一个虚构人物。" * 10 + "这不是我的经历，我不喝咖啡。"
        self.assertGreater(len(text), 120)
        self.store([(1, text)], "用户喜欢咖啡")
        context = self.mem.context(100, 1, "咖啡品鉴")
        self.assertIn("原话=" + json.dumps(text, ensure_ascii=False), context)
        self.assertNotIn("节选", context)
        self.mem.MAX_CONTEXT = len(context) - 1
        self.assertEqual(self.mem.context(100, 1, "咖啡品鉴"), "")

    def test_duplicate_sources_count_once_and_use_message_time(self):
        self.store([(1, "明天读量子物理")], "量子物理讨论")
        group = self.mem._groups["100"]
        duplicate = copy.deepcopy(group["summaries"][0])
        duplicate["time"] += 86400
        group["summaries"].append(duplicate)
        evidence = duplicate["evidence"][0]
        context = self.mem.context(100, 1, "量子物理")
        self.assertEqual(context.count('原话="明天读量子物理"'), 1)
        self.assertIn(self.mem._stamp(evidence["time"]), context)
        self.assertNotIn(self.mem._stamp(duplicate["time"]), context)

    def test_generic_recall_is_recent_and_unrelated_query_empty(self):
        self.store([(1, "明天读量子物理")], "开始读书")
        self.store([(1, "晚上去看流星雨")], "准备出门")
        context = self.mem.context(100, 1, "还记得我吗")
        self.assertLess(context.index("流星雨"), context.index("量子物理"))
        self.assertEqual(self.mem.context(100, 1, "如何养猫"), "")


if __name__ == "__main__":
    unittest.main()
