import json
import tempfile
import unittest
from pathlib import Path

from conversation_memory import ConversationMemory


class MemoryPriorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "memory.json"
        self.now = 1_800_000_000
        self.serial = 0
        self.mem = ConversationMemory(self.path, (100, 200), (999,), lambda: self.now)

    def event(self, text, uid=1, gid=100):
        self.serial += 1
        return {"post_type": "message", "message_type": "group", "group_id": gid,
                "user_id": uid, "self_id": 999, "message_id": self.serial,
                "time": self.now, "raw_message": text}

    def observe(self, text, **scope):
        event = self.event(text, **scope)
        self.assertTrue(self.mem.observe(event))
        return event

    def remember_basics(self, **scope):
        return (self.observe("叫我小林", **scope),
                self.observe("以后回复我时先说结论", **scope))

    def stored_facts(self, uid="1", gid="100"):
        return json.loads(self.path.read_text(encoding="utf-8"))["groups"][gid]["users"][uid]["facts"]

    def test_overflow_keeps_address_and_style_within_twelve_total(self):
        self.remember_basics()
        for i in range(20):
            self.observe(f"我喜欢咖啡品种{i}")
        facts = self.stored_facts()
        self.assertEqual(len(facts), 12)
        self.assertEqual(facts["称呼"]["value"], "小林")
        self.assertEqual(facts["回复习惯"]["value"], "先说结论")
        self.assertEqual({key for key in facts if key.startswith("偏好:")},
                         {f"偏好:咖啡品种{i}" for i in range(10, 20)})

    def test_no_address_or_style_still_uses_all_twelve_slots(self):
        for i in range(13):
            self.observe(f"我喜欢品种{i}")
        facts = self.stored_facts()
        self.assertEqual(len(facts), 12)
        self.assertNotIn("偏好:品种0", facts)
        self.assertIn("偏好:品种12", facts)

    def test_adding_basics_to_full_store_evicts_only_oldest_preferences(self):
        for i in range(12):
            self.observe(f"我喜欢品种{i}")
        self.remember_basics()
        facts = self.stored_facts()
        self.assertEqual(len(facts), 12)
        self.assertNotIn("偏好:品种0", facts)
        self.assertNotIn("偏好:品种1", facts)
        self.assertIn("偏好:品种2", facts)
        self.assertIn("称呼", facts)
        self.assertIn("回复习惯", facts)

    def test_core_facts_reach_context_ahead_of_newer_matching_preferences(self):
        self.remember_basics()
        for i in range(8):
            self.now += 1
            self.observe(f"我喜欢咖啡品种{i}")
        for query in ("咖啡品种", "还记得我的偏好吗", "刚下班回家"):
            context = self.mem.context(100, 1, query)
            self.assertIn("用户自述·称呼：小林", context)
            self.assertIn("用户自述·回复习惯：先说结论", context)
            self.assertLessEqual(len(context), self.mem.MAX_CONTEXT)
            fact_lines = [line for line in context.splitlines() if line.startswith("用户自述")]
            self.assertTrue(all("·偏好" not in line for line in fact_lines[:2]))
            self.assertLessEqual(len(fact_lines), 4)

    def test_preference_relevance_outranks_newness_then_time_breaks_ties(self):
        self.remember_basics()
        self.observe("我喜欢coffee hiking beach")
        for text in ("我喜欢coffee", "我喜欢hiking", "我喜欢beach"):
            self.now += 1
            self.observe(text)
        context = self.mem.context(100, 1, "coffee hiking beach")
        self.assertIn("偏好（coffee hiking beach）", context)
        self.assertIn("偏好（beach）", context)
        self.assertNotIn("偏好（coffee）", context)
        self.assertNotIn("偏好（hiking）", context)
        self.assertLess(context.index("偏好（coffee hiking beach）"), context.index("偏好（beach）"))

    def test_same_second_relevance_ties_use_latest_observation(self):
        self.remember_basics()
        for name in ("coffee", "hiking", "beach"):
            self.observe(f"我喜欢{name}")
        context = self.mem.context(100, 1, "coffee hiking beach")
        self.assertIn("偏好（beach）", context)
        self.assertIn("偏好（hiking）", context)
        self.assertNotIn("偏好（coffee）", context)
        self.assertLess(context.index("偏好（beach）"), context.index("偏好（hiking）"))

    def test_core_facts_and_sources_survive_restart_after_overflow(self):
        nick, style = self.remember_basics()
        for i in range(16):
            self.observe(f"我喜欢品种{i}")
        restarted = ConversationMemory(self.path, (100, 200), (999,), lambda: self.now)
        context = restarted.context(100, 1, "还记得我吗")
        self.assertIn('原话="叫我小林"', context)
        self.assertIn('原话="以后回复我时先说结论"', context)
        self.assertIn(f"证据消息={nick['message_id']}，", context)
        self.assertIn(f"证据消息={style['message_id']}，", context)
        self.assertEqual(restarted.status()["groups"]["100"]["facts"], 12)

    def test_update_and_retract_do_not_restore_older_core_fact(self):
        old_nick, old_style = self.remember_basics()
        new_nick = self.observe("以后叫我小陈")
        new_style = self.observe("以后回复我时详细一点")
        self.mem.retract(100, old_nick["message_id"])
        self.mem.retract(100, old_style["message_id"])
        context = self.mem.context(100, 1, "还记得我吗")
        self.assertIn("小陈", context)
        self.assertIn("详细一点", context)
        self.assertNotIn("小林", context)
        self.assertNotIn("先说结论", context)
        self.mem.retract(100, new_nick["message_id"])
        self.mem.retract(100, new_style["message_id"])
        self.assertEqual(self.mem.context(100, 1, "还记得我吗"), "")
        self.assertFalse(self.mem.observe(new_nick))
        self.assertFalse(self.mem.observe(new_style))

    def test_forget_core_facts_remains_user_and_group_scoped(self):
        self.remember_basics()
        self.observe("叫我小陈", uid=2)
        self.observe("叫我阿林", gid=200)
        self.observe("我喜欢咖啡")
        self.mem.command(self.event("!忘记 称呼"))
        context = self.mem.context(100, 1, "还记得我吗")
        self.assertNotIn("小林", context)
        self.assertIn("先说结论", context)
        self.mem.command(self.event("!忘记 回复习惯"))
        context = self.mem.context(100, 1, "咖啡")
        self.assertNotIn("先说结论", context)
        self.assertIn("喜欢咖啡", context)
        self.assertNotIn("小陈", context)
        self.assertNotIn("阿林", context)
        self.mem.command(self.event("!忘记 全部"))
        self.assertEqual(self.mem.context(100, 1, "还记得我吗"), "")
        self.assertIn("小陈", self.mem.context(100, 2, "你好"))
        self.assertIn("阿林", self.mem.context(200, 1, "你好"))


if __name__ == "__main__":
    unittest.main()
