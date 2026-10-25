"""Synthetic event lifecycle checks; no model calls or production conversation data."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conversation_memory import ConversationMemory


class EventFollowupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "memory.json"
        self.now, self.serial = 1_800_000_000, 0
        self.mem = ConversationMemory(self.path, (100, 200), (999,), lambda: self.now)
        network = patch("socket.create_connection", side_effect=AssertionError("offline test"))
        network.start()
        self.addCleanup(network.stop)

    def event(self, text, uid=1, gid=100, **extra):
        self.serial += 1
        value = {"post_type": "message", "message_type": "group", "group_id": gid,
                 "user_id": uid, "self_id": 999, "message_id": self.serial,
                 "time": self.now, "raw_message": text}
        value.update(extra)
        return value

    def stored(self, gid=100):
        return json.loads(self.path.read_text(encoding="utf-8"))["groups"][str(gid)]["events"]

    @staticmethod
    def extraction(event, topic):
        return {"summaries": [], "events": [{"event_id": "", "user_id": str(event["user_id"]),
            "topic": topic, "detail": event["raw_message"], "status": "open",
            "source_ids": [str(event["message_id"])]}]}

    def plan(self, topic="毕业论文", uid=1, gid=100):
        # Existing model extraction is replayed from an exact synthetic response.
        event = self.event("我正在准备" + topic, uid, gid)
        self.mem.observe(event)
        batch = self.mem.prepare_batch(gid)
        self.assertIsNotNone(batch)
        self.assertTrue(self.mem.complete_batch(batch, self.extraction(event, topic)))
        return event

    def test_one_explicit_plan_can_use_existing_extractor_before_twenty_four(self):
        origin = self.plan()
        self.assertEqual(len(self.stored()), 1)
        self.assertEqual(self.stored()[0]["source_ids"], [str(origin["message_id"])])
        self.assertEqual(self.mem.status()["groups"]["100"]["facts"], 0)

    def test_small_extraction_keeps_original_cooldown(self):
        self.plan()
        self.now += 20
        self.mem.observe(self.event("我准备参加登山活动"))
        self.assertIsNone(self.mem.prepare_batch(100))
        self.now += 580
        self.assertIsNotNone(self.mem.prepare_batch(100))

    def test_chitchat_preferences_and_quoted_plans_do_not_start_small_batches(self):
        for text in ("你好", "我喜欢咖啡", "朋友说我准备参加比赛", "我正在准备考试吗", "我正在写什么呢？"):
            self.mem.observe(self.event(text))
        self.assertIsNone(self.mem.prepare_batch(100))

    def test_recent_unique_completion_has_literal_evidence_and_immediate_context(self):
        origin = self.plan()
        self.now += 60
        update = self.event("终于弄完了")
        self.assertTrue(self.mem.observe(update))
        saved = self.stored()[0]
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["detail"], "终于弄完了")
        self.assertEqual(saved["source_ids"], [str(origin["message_id"]), str(update["message_id"])])
        self.assertIn("毕业论文", self.mem.context(100, 1, "终于弄完了", update["message_id"]))
        self.assertIn("已完成", self.mem.context(100, 1, "终于弄完了", update["message_id"]))
        self.assertEqual(self.mem.context(100, 1, "终于弄完了", "99999"), "")
        self.assertEqual(self.mem.context(100, 1, "终于弄完了"), "")
        self.assertEqual(self.mem.status()["groups"]["100"]["facts"], 0)
        restored = ConversationMemory(self.path, (100, 200), (999,), lambda: self.now)
        self.assertIn("已完成", restored.context(100, 1, "毕业论文"))

    def test_implicit_cancel_is_same_user_and_group_only(self):
        self.plan("登山活动", uid=1, gid=100)
        self.plan("登山活动", uid=1, gid=200)
        self.mem.observe(self.event("那个不去了", uid=2))
        self.assertEqual(self.stored()[0]["status"], "open")
        self.mem.observe(self.event("那个不去了"))
        self.assertEqual(self.stored()[0]["status"], "cancelled")
        self.assertEqual(self.stored(200)[0]["status"], "open")

    def test_two_recent_events_do_not_guess_omitted_subject(self):
        self.plan()
        self.now += 600
        self.plan("登山活动")
        self.mem.observe(self.event("那个不去了"))
        self.assertEqual([e["status"] for e in self.stored()], ["open", "open"])

    def test_named_update_does_not_require_a_small_model_batch(self):
        self.plan()
        update = self.event("我的毕业论文终于写完了")
        self.mem.observe(update)
        self.assertEqual(self.stored()[0]["status"], "completed")
        self.assertIsNone(self.mem.prepare_batch(100))

    def test_explicit_reply_disambiguates_and_can_reference_older_own_event(self):
        origin = self.plan()
        self.now += 1800
        self.plan("登山活动")
        update = self.event("unused", message=[
            {"type": "reply", "data": {"id": str(origin["message_id"]) }},
            {"type": "text", "data": {"text": "终于弄完了"}}])
        self.assertTrue(self.mem.observe(update))
        states = {e["topic"]: e["status"] for e in self.stored()}
        self.assertEqual(states, {"毕业论文": "completed", "登山活动": "open"})
        self.assertIn("已完成", self.mem.context(100, 1, "终于弄完了", update["message_id"]))

    def test_raw_cq_reply_and_metadata_are_supported_without_importing_quoted_content(self):
        origin = self.plan()
        event = self.event(f"[CQ:reply,id={origin['message_id']}]那个不去了")
        self.assertTrue(self.mem.observe(event))
        self.assertEqual(self.stored()[0]["status"], "cancelled")
        self.assertIn("已取消", self.mem.context(100, 1, event["raw_message"], event["message_id"]))
        self.now += 600
        second = self.plan("生日蛋糕")
        event = self.event("做完了", reply={"id": second["message_id"], "text": "我喜欢咖啡"})
        self.assertTrue(self.mem.observe(event))
        self.assertEqual(self.mem.status()["groups"]["100"]["facts"], 0)
        self.assertNotIn("咖啡", self.path.read_text(encoding="utf-8"))

    def test_foreign_unknown_conflicting_or_non_status_reply_never_falls_back(self):
        origin = self.plan(uid=2)
        self.now += 600
        own = self.plan()
        for reply, text in ((origin["message_id"], "弄完了"), (9999, "弄完了"),
                            (own["message_id"], "我喜欢咖啡")):
            event = self.event(f"[CQ:reply,id={reply}]{text}")
            self.assertFalse(self.mem.observe(event))
        self.assertFalse(self.mem.observe(self.event("弄完了", reply={"id": own["message_id"]},
            message=[{"type": "reply", "data": {"id": str(origin["message_id"])}},
                     {"type": "text", "data": {"text": "弄完了"}}])))
        self.assertTrue(all(e["status"] == "open" for e in self.stored()))

    def test_expired_recent_window_requires_explicit_reference(self):
        self.plan()
        self.now += 601
        self.mem.observe(self.event("终于弄完了"))
        self.assertEqual(self.stored()[0]["status"], "open")

    def test_ambiguous_new_short_message_does_not_recall_old_same_wording(self):
        self.plan()
        previous = self.event("终于弄完了")
        self.mem.observe(previous)
        current = self.event("终于弄完了")
        self.mem.observe(current)
        self.assertEqual(self.mem.context(100, 1, "终于弄完了", current["message_id"]), "")

    def test_negated_future_question_and_others_status_do_not_finish(self):
        self.plan()
        for text in ("还没弄完了", "明天就弄完了", "终于弄完了吗", "他终于弄完了", "朋友说弄完了",
                     "如果弄完了", "我的毕业论文还没写完了", "我明天准备完成了毕业论文"):
            self.mem.observe(self.event(text))
            self.assertEqual(self.stored()[0]["status"], "open", text)
        self.mem.observe(self.event("我的毕业论文弄完了没"))
        self.assertEqual(self.stored()[0]["status"], "open")

    def test_completion_as_a_condition_does_not_finish(self):
        self.plan()
        for text in ("我的毕业论文写完了再睡", "我的毕业论文完成了才能休息",
                     "我的毕业论文写完了，就可以休息了", "我的毕业论文写完了才睡"):
            with self.subTest(text=text):
                self.mem.observe(self.event(text))
                self.assertEqual(self.stored()[0]["status"], "open")
        # A later realization is a completed action, not a future condition.
        self.mem.observe(self.event("我的毕业论文写完了，才发现忘了参考文献"))
        self.assertEqual(self.stored()[0]["status"], "completed")

    def test_model_cannot_trim_completion_condition_out_of_the_source(self):
        origin = self.plan()
        self.now += 600
        conditional = self.event("我的毕业论文写完了再睡")
        self.mem.observe(conditional)
        for _ in range(23):
            self.mem.observe(self.event("今天天气不错"))
        batch = self.mem.prepare_batch(100)
        response = self.extraction(conditional, "毕业论文")
        response["events"][0].update(event_id=self.stored()[0]["id"],
            status="completed", detail="我的毕业论文写完了")
        self.assertFalse(self.mem.complete_batch(batch, response))
        self.assertEqual(self.stored()[0]["status"], "open")
        self.assertEqual(self.stored()[0]["source_ids"], [str(origin["message_id"])])

    def test_explicit_correction_reopens_with_evidence_and_can_be_retracted(self):
        origin = self.plan()
        completion = self.event("终于弄完了")
        self.mem.observe(completion)
        correction = self.event("我的毕业论文其实还没写完")
        self.mem.observe(correction)
        saved = self.stored()[0]
        self.assertEqual(saved["status"], "open")
        self.assertEqual(saved["detail"], correction["raw_message"])
        self.assertEqual(saved["source_ids"], [str(e["message_id"]) for e in (origin, completion, correction)])
        self.assertIn("进行中", self.mem.context(100, 1, "毕业论文"))
        self.mem.retract(100, correction["message_id"])
        self.assertEqual(self.stored(), [])

    def test_plain_or_foreign_unfinished_claim_does_not_reopen(self):
        self.plan()
        self.mem.observe(self.event("终于弄完了"))
        self.mem.observe(self.event("我的毕业论文还没写完"))
        self.mem.observe(self.event("我的毕业论文还没写完，我的晚饭其实还没吃"))
        self.mem.observe(self.event("我的毕业论文其实还没写完", uid=2))
        self.mem.observe(self.event("我的毕业论文其实还没写完", gid=200))
        self.assertEqual(self.stored()[0]["status"], "completed")
        self.mem.observe(self.event("我刚才说错了，我的毕业论文尚未完成"))
        self.assertEqual(self.stored()[0]["status"], "open")

    def test_old_model_result_cannot_undo_explicit_correction(self):
        self.plan()
        self.now += 600
        completion = self.event("我的毕业论文写完了")
        self.mem.observe(completion)
        for _ in range(23):
            self.mem.observe(self.event("今天天气不错"))
        batch = self.mem.prepare_batch(100)
        eid = self.stored()[0]["id"]
        correction = self.event("我的毕业论文其实还没写完")
        self.mem.observe(correction)
        response = self.extraction(completion, "毕业论文")
        response["events"][0].update(event_id=eid, status="completed")
        self.assertFalse(self.mem.complete_batch(batch, response))
        self.assertEqual(self.stored()[0]["status"], "open")
        self.assertEqual(self.stored()[0]["source_ids"][-1], str(correction["message_id"]))

    def test_old_or_duplicate_delivery_cannot_change_status(self):
        self.plan()
        self.now += 30
        old = self.event("终于弄完了", time=self.now - 60)
        self.mem.observe(old)
        self.assertEqual(self.stored()[0]["status"], "open")
        update = self.event("那个不去了")
        self.mem.observe(update)
        self.assertFalse(self.mem.observe(update))
        self.assertEqual(self.stored()[0]["status"], "cancelled")

    def test_retract_origin_or_update_removes_the_whole_event(self):
        origin = self.plan()
        update = self.event("终于弄完了")
        self.mem.observe(update)
        self.mem.retract(100, update["message_id"])
        self.assertEqual(self.stored(), [])
        self.assertFalse(self.mem.observe(update))
        self.now += 600
        origin = self.plan()
        self.mem.observe(self.event("终于弄完了"))
        self.mem.retract(100, origin["message_id"])
        self.assertEqual(self.stored(), [])

    def test_forget_blocks_old_small_batch_and_replied_source(self):
        event = self.event("我正在准备毕业论文")
        self.mem.observe(event)
        batch = self.mem.prepare_batch(100)
        self.mem.command(self.event("!忘记 全部"))
        self.assertFalse(self.mem.complete_batch(batch, self.extraction(event, "毕业论文")))
        self.assertFalse(self.mem.observe(self.event(f"[CQ:reply,id={event['message_id']}]终于弄完了")))
        self.assertEqual(self.stored(), [])

    def test_inflight_old_progress_cannot_overwrite_immediate_completion(self):
        self.plan()
        self.now += 600
        progress = self.event("我的毕业论文还在修改")
        self.mem.observe(progress)
        for _ in range(23):
            self.mem.observe(self.event("今天天气不错"))
        batch = self.mem.prepare_batch(100)
        eid = self.stored()[0]["id"]
        update = self.event("终于弄完了")
        self.mem.observe(update)
        response = self.extraction(progress, "毕业论文")
        response["events"][0]["event_id"] = eid
        self.assertFalse(self.mem.complete_batch(batch, response))
        self.assertEqual(self.stored()[0]["status"], "completed")

    def test_memory_command_shows_three_own_event_statuses_without_facts(self):
        self.plan("他人活动", uid=2)
        for topic in ("活动一", "活动二", "活动三", "活动四"):
            self.now += 600
            self.plan(topic)
        message = self.mem.command(self.event("!记忆"))
        self.assertNotIn("没有你的", message)
        self.assertNotIn("他人活动", message)
        self.assertEqual(message.count("事件·"), 3)
        self.assertIn("进行中", message)
        self.assertIn("消息", message)
        self.assertNotIn("事件·", self.mem.command(self.event("!记忆", gid=200)))


if __name__ == "__main__":
    unittest.main()
