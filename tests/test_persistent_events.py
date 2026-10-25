import copy
import json
import tempfile
import unittest
from pathlib import Path

from conversation_memory import ConversationMemory


class PersistentEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "memory.json"
        self.now = 1_800_000_000
        self.serial = 0
        self.mem = ConversationMemory(self.path, (100, 200), (999,), lambda: self.now)

    def message(self, text, uid=1, gid=100, **extra):
        self.serial += 1
        result = {"post_type": "message", "message_type": "group", "group_id": gid,
                  "user_id": uid, "self_id": 999, "message_id": self.serial,
                  "raw_message": text, "time": self.now}
        result.update(extra)
        return result

    def batch(self, text, uid=1, gid=100, extra_messages=()):
        self.now += 600
        source = self.message(text, uid, gid)
        self.assertTrue(self.mem.observe(source))
        for other_uid, other_text in extra_messages:
            self.mem.observe(self.message(other_text, other_uid, gid))
        for _ in range(23 - len(extra_messages)):
            self.mem.observe(self.message("这次讨论到这里", uid, gid))
        batch = self.mem.prepare_batch(gid)
        self.assertIsNotNone(batch)
        return batch, str(source["message_id"])

    @staticmethod
    def response(source_id, text, topic="毕业论文", status="open", event_id="", uid=1):
        return {"summaries": [], "events": [{"event_id": event_id, "user_id": str(uid),
                "topic": topic, "detail": text, "status": status, "source_ids": [source_id]}]}

    def stored(self, gid=100):
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return data["groups"][str(gid)]["events"]

    def create(self, text="我正在写毕业论文", topic="毕业论文", uid=1, gid=100, status="open"):
        batch, source = self.batch(text, uid, gid)
        self.assertTrue(self.mem.complete_batch(batch, self.response(source, text, topic, status, uid=uid)))
        event = next(e for e in self.stored(gid) if e["user_id"] == str(uid) and e["topic"] == topic)
        return event, source

    def test_open_completed_and_restart_keep_literal_sources(self):
        event, origin = self.create()
        text = "我的毕业论文已经写完了"
        batch, source = self.batch(text)
        self.assertEqual(batch["existing_events"][0]["id"], event["id"])
        self.assertNotIn("evidence", batch["existing_events"][0])
        self.assertTrue(self.mem.complete_batch(batch, self.response(source, text, status="completed", event_id=event["id"])))
        saved = self.stored()[0]
        self.assertEqual(saved["source_ids"], [origin, source])
        self.assertEqual(saved["detail"], text)
        self.assertEqual(saved["status"], "completed")
        other = ConversationMemory(self.path, (100,), (999,), lambda: self.now)
        context = other.context(100, 1, "毕业论文后面还可以怎么改")
        self.assertIn("用户自述状态：已完成", context)
        self.assertIn(text, context)
        self.assertIn("不是追问任务", context)
        self.assertIn("已结束的事不再催进度", context)

    def test_cancel_and_explicit_reopen(self):
        event, _ = self.create("我打算参加春季比赛", "春季比赛")
        text = "我取消了春季比赛"
        batch, source = self.batch(text)
        self.assertTrue(self.mem.complete_batch(batch, self.response(source, text, "春季比赛", "cancelled", event["id"])))
        self.assertIn("已取消", self.mem.context(100, 1, "春季比赛"))
        text = "我重新准备参加春季比赛"
        batch, source = self.batch(text)
        self.assertTrue(self.mem.complete_batch(batch, self.response(source, text, "春季比赛", "open", event["id"])))
        self.assertEqual(self.stored()[0]["status"], "open")

    def test_finished_event_cannot_reopen_from_an_ordinary_progress_claim(self):
        event, _ = self.create("我完成了毕业论文", status="completed")
        text = "我正在写毕业论文"
        batch, source = self.batch(text)
        self.assertFalse(self.mem.complete_batch(batch, self.response(source, text, event_id=event["id"])))
        self.assertEqual(self.stored()[0]["status"], "completed")

    def test_object_origin_is_recalled_without_imaginary_relationship(self):
        text = "我收到了小林送的木雕挂饰"
        self.create(text, "木雕挂饰", status="completed")
        context = self.mem.context(100, 1, "这个木雕挂饰的来历呢")
        self.assertIn(text, context)
        self.assertNotIn("恋人", context)
        self.assertEqual(self.mem.context(100, 1, "早上好"), "")

    def test_same_group_different_user_and_same_user_different_group_are_isolated(self):
        self.create("我正在写毕业论文", uid=1)
        self.create("我正在准备生日蛋糕", "生日蛋糕", uid=2)
        self.create("我正在修红石工厂", "红石工厂", uid=1, gid=200)
        context = self.mem.context(100, 1, "毕业论文、生日蛋糕和红石工厂")
        self.assertIn("毕业论文", context)
        self.assertNotIn("生日蛋糕", context)
        self.assertNotIn("红石工厂", context)
        self.assertEqual(self.mem.context(100, 2, "毕业论文"), "")
        self.assertEqual(self.mem.context(200, 1, "毕业论文"), "")

    def test_no_event_on_greeting_generic_recall_or_changed_topic(self):
        self.create()
        for text in ("你好", "早上好", "还记得我吗", "上次聊到哪了", "红石中继器怎么制作"):
            with self.subTest(text=text):
                self.assertEqual(self.mem.context(100, 1, text), "")

    def test_only_one_relevant_event_fits_after_priority_facts(self):
        self.mem.observe(self.message("叫我小林"))
        self.mem.observe(self.message("以后回复我时简短一点"))
        # Consume these two observations through an ordinary summary batch first.
        for _ in range(22):
            self.mem.observe(self.message("今天聊到这里"))
        batch = self.mem.prepare_batch(100)
        self.assertTrue(self.mem.complete_batch(batch, {"summaries": [], "events": []}))
        self.create()
        self.create("我正在制作论文插图", "论文插图")
        context = self.mem.context(100, 1, "毕业论文的论文插图")
        self.assertEqual(context.count("相关持续事件·"), 1)
        self.assertIn("小林", context)
        self.assertIn("简短一点", context)
        self.assertLessEqual(len(context), self.mem.MAX_CONTEXT)

    def test_latest_status_suppresses_old_summary_for_same_topic(self):
        text = "我正在写毕业论文"
        batch, source = self.batch(text)
        response = self.response(source, text)
        response["summaries"] = [{"summary": "用户仍在赶毕业论文，尚未结束", "source_ids": [source]}]
        self.assertTrue(self.mem.complete_batch(batch, response))
        event = self.stored()[0]
        text = "我的毕业论文写完了"
        batch, source = self.batch(text)
        self.assertTrue(self.mem.complete_batch(batch, self.response(source, text, status="completed", event_id=event["id"])))
        context = self.mem.context(100, 1, "毕业论文")
        self.assertIn("已完成", context)
        self.assertNotIn("仍在赶", context)
        self.assertNotIn("我正在写毕业论文", context)
        # Even a vague summary must not leak its old source status.
        self.mem._groups["100"]["summaries"][0]["summary"] = "用户谈到了一件事情"
        context = self.mem.context(100, 1, "毕业论文")
        self.assertIn("已完成", context)
        self.assertNotIn("我正在写毕业论文", context)

    def test_sources_must_be_new_same_person_batch_messages(self):
        event, origin = self.create()
        text = "我的毕业论文写完了"
        batch, source = self.batch(text, uid=2)
        self.assertEqual(batch["existing_events"], [])
        forged = self.response(source, text, status="completed", event_id=event["id"], uid=1)
        self.assertFalse(self.mem.complete_batch(batch, forged))
        self.assertEqual(self.stored()[0]["status"], "open")
        self.now += 600
        retry = self.mem.prepare_batch(100)
        old_source = self.response(origin, "我正在写毕业论文", event_id=event["id"])
        self.assertFalse(self.mem.complete_batch(retry, old_source))

    def test_model_cannot_invent_text_topic_or_completion(self):
        cases = [
            ("我正在写毕业论文", "我的毕业论文写完了", "毕业论文", "completed"),
            ("我正在写毕业论文", "我正在写毕业论文", "婚礼筹备", "open"),
            ("我还没完成了毕业论文", "我还没完成了毕业论文", "毕业论文", "completed"),
            ("我不觉得毕业论文已经完成了", "我不觉得毕业论文已经完成了", "毕业论文", "completed"),
            ("我大概把毕业论文完成了", "我大概把毕业论文完成了", "毕业论文", "completed"),
            ("我明天准备完成了毕业论文", "我明天准备完成了毕业论文", "毕业论文", "completed"),
            ("我正在写毕业论文，数学作业写完了", "我正在写毕业论文，数学作业写完了", "毕业论文", "completed"),
            ("朋友说他完成了毕业论文", "朋友说他完成了毕业论文", "毕业论文", "completed"),
            ("我不准备写毕业论文", "我不准备写毕业论文", "毕业论文", "open"),
        ]
        for index, (source_text, detail, topic, status) in enumerate(cases):
            with self.subTest(source=source_text, detail=detail):
                self.path = Path(self.tmp.name) / f"rejection-{index}.json"
                self.mem = ConversationMemory(self.path, (100,), (999,), lambda: self.now)
                batch, source = self.batch(source_text)
                self.assertFalse(self.mem.complete_batch(batch, self.response(source, detail, topic, status)))
                self.assertEqual(self.mem.status()["groups"]["100"]["events"], 0)

    def test_plain_unfinished_statement_stays_open(self):
        self.create("我的毕业论文还没写完")
        self.assertIn("进行中", self.mem.context(100, 1, "毕业论文"))

    def test_forget_during_update_cannot_resurrect_event(self):
        event, _ = self.create()
        text = "我的毕业论文写完了"
        batch, source = self.batch(text)
        self.mem.command(self.message("!忘记 全部"))
        self.assertFalse(self.mem.complete_batch(batch, self.response(source, text, status="completed", event_id=event["id"])))
        self.assertEqual(self.stored(), [])
        self.assertEqual(self.mem.context(100, 1, "毕业论文"), "")

    def test_forget_one_person_does_not_clear_another_event(self):
        self.create(uid=1)
        self.create("我正在准备生日蛋糕", "生日蛋糕", uid=2)
        self.mem.command(self.message("!忘记 偏好", uid=1))
        self.assertEqual(self.mem.context(100, 1, "毕业论文"), "")
        self.assertIn("生日蛋糕", self.mem.context(100, 2, "生日蛋糕"))

    def test_retract_origin_during_update_cannot_restore_event(self):
        event, origin = self.create()
        text = "我的毕业论文写完了"
        batch, source = self.batch(text)
        self.assertTrue(self.mem.retract(100, origin))
        self.assertFalse(self.mem.complete_batch(batch, self.response(source, text, status="completed", event_id=event["id"])))
        self.assertEqual(self.stored(), [])

    def test_retract_latest_status_removes_event_instead_of_falling_back(self):
        event, _ = self.create()
        text = "我的毕业论文写完了"
        batch, source = self.batch(text)
        self.assertTrue(self.mem.complete_batch(batch, self.response(source, text, status="completed", event_id=event["id"])))
        self.assertTrue(self.mem.retract(100, source))
        self.assertEqual(self.mem.context(100, 1, "毕业论文"), "")
        self.assertEqual(self.stored(), [])

    def test_expiry_during_callback_cannot_revive_old_event(self):
        event, _ = self.create()
        text = "我的毕业论文写完了"
        batch, source = self.batch(text)
        self.now += self.mem.EVENT_TTL + 1
        self.assertEqual(self.mem.context(100, 1, "毕业论文"), "")
        self.assertFalse(self.mem.complete_batch(batch, self.response(source, text, status="completed", event_id=event["id"])))
        self.assertEqual(self.mem.status()["groups"]["100"]["events"], 0)

    def test_old_v1_keeps_facts_summaries_without_event_migration(self):
        self.mem.observe(self.message("叫我小林"))
        for _ in range(23):
            self.mem.observe(self.message("我正在写毕业论文"))
        batch = self.mem.prepare_batch(100)
        self.assertTrue(self.mem.complete_batch(batch, {"summaries": [
            {"summary": "用户谈到毕业论文", "source_ids": [batch["messages"][1]["source_id"]]}]}))
        old = json.loads(self.path.read_text(encoding="utf-8"))
        old["groups"]["100"].pop("events")
        self.path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        other = ConversationMemory(self.path, (100,), (999,), lambda: self.now)
        context = other.context(100, 1, "毕业论文")
        self.assertIn("小林", context)
        self.assertIn('原话="我正在写毕业论文"', context)
        self.assertNotIn("相关持续事件", context)
        self.assertEqual(other.status()["groups"]["100"]["events"], 0)

    def test_newer_batch_replaces_stale_callback_and_private_snapshot_wins(self):
        event, _ = self.create()
        text = "我的毕业论文写完了"
        first, source = self.batch(text)
        self.now += 600
        second = self.mem.prepare_batch(100)
        tampered = copy.deepcopy(second)
        tampered["existing_events"][0]["status"] = "cancelled"
        tampered["messages"][0]["text"] = "我取消了毕业论文"
        self.assertTrue(self.mem.complete_batch(tampered, self.response(source, text, status="completed", event_id=event["id"])))
        self.assertFalse(self.mem.complete_batch(first, self.response(source, text, status="completed", event_id=event["id"])))
        self.assertEqual(self.stored()[0]["status"], "completed")

    def test_new_observations_during_api_wait_survive_consuming_snapshot(self):
        text = "我正在写毕业论文"
        batch, source = self.batch(text)
        self.mem.observe(self.message("我正在准备生日蛋糕", uid=2))
        self.assertTrue(self.mem.complete_batch(batch, self.response(source, text)))
        self.assertEqual(self.mem.status()["groups"]["100"]["pending"], 1)
        self.assertEqual(len(self.stored()), 1)

    def test_per_user_capacity_and_batch_context_remain_small(self):
        for i in range(5):
            self.create(f"我正在准备研究项目{i}", f"研究项目{i}")
        events = self.stored()
        self.assertEqual(len(events), 4)
        self.assertNotIn("研究项目0", [e["topic"] for e in events])
        self.assertIn("研究项目4", [e["topic"] for e in events])
        batch, _ = self.batch("我继续准备研究项目4")
        self.assertLessEqual(len(batch["existing_events"]), self.mem.BATCH_EVENTS)
        self.assertNotIn("evidence", batch["existing_events"][0])
        self.assertNotIn("我继续准备研究项目4", batch["prompt"])

    def test_empty_extraction_consumes_batch_without_fabricating_events(self):
        batch, _ = self.batch("今天随便聊聊")
        self.assertTrue(self.mem.complete_batch(batch, {"summaries": [], "events": []}))
        status = self.mem.status()["groups"]["100"]
        self.assertEqual((status["pending"], status["summaries"], status["events"]), (0, 0, 0))

    def test_newer_unrelated_source_cannot_refresh_an_expired_statement(self):
        text = "我正在写毕业论文"
        old = self.message(text)
        self.mem.observe(old)
        self.now += self.mem.EVENT_TTL + 1
        new = self.message("这次讨论到这里")
        self.mem.observe(new)
        for _ in range(22):
            self.mem.observe(self.message("今晚先聊到这儿"))
        batch = self.mem.prepare_batch(100)
        response = self.response(str(old["message_id"]), text)
        response["events"][0]["source_ids"].append(str(new["message_id"]))
        self.assertFalse(self.mem.complete_batch(batch, response))
        self.assertEqual(self.mem.context(100, 1, "毕业论文"), "")

    def test_actual_extraction_replay_requires_a_contiguous_topic(self):
        # Recorded API output from the synthetic smoke scenario; no model call.
        text = "我的毕业论文还没写完，今晚准备修改摘要。"
        recorded = {"summaries": [{
            "summary": "一位成员提到毕业论文还没写完，今晚准备修改摘要；随后多次表示“这次讨论到这里”。",
            "source_ids": [str(i) for i in range(3, 27)]}], "events": [{
                "event_id": "", "user_id": "7", "topic": "毕业论文修改摘要",
                "detail": text, "status": "open", "source_ids": ["3"]}]}
        for topic, expected_events in (("毕业论文修改摘要", 0), ("毕业论文", 1)):
            with self.subTest(topic=topic):
                self.path = Path(self.tmp.name) / f"replay-{expected_events}.json"
                self.mem = ConversationMemory(self.path, (10,), (999,), lambda: self.now)
                self.serial = 2
                batch, source = self.batch(text, uid=7, gid=10)
                self.assertEqual(source, "3")
                response = copy.deepcopy(recorded)
                response["events"][0]["topic"] = topic
                self.assertTrue(self.mem.complete_batch(batch, response))
                status = self.mem.status()["groups"]["10"]
                self.assertEqual(status["summaries"], 1)
                self.assertEqual(status["events"], expected_events)
                context = self.mem.context(10, 7, "毕业论文的摘要总算改顺了。")
                self.assertEqual("相关持续事件·" in context, bool(expected_events))
                if expected_events:
                    self.assertIn(text, context)

    def test_explicit_submission_finishes_but_planned_or_negated_submission_does_not(self):
        cases = (("我的毕业论文已经写完并提交了。", True),
                 ("我的毕业论文准备提交了。", False),
                 ("我的毕业论文还没提交了。", False))
        for index, (text, accepted) in enumerate(cases):
            with self.subTest(text=text):
                self.path = Path(self.tmp.name) / f"submission-{index}.json"
                self.mem = ConversationMemory(self.path, (100,), (999,), lambda: self.now)
                event, _ = self.create()
                batch, source = self.batch(text)
                response = self.response(source, text, status="completed", event_id=event["id"])
                self.assertEqual(self.mem.complete_batch(batch, response), accepted)
                self.assertEqual(self.stored()[0]["status"], "completed" if accepted else "open")


if __name__ == "__main__":
    unittest.main()
