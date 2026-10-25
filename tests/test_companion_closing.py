"""Offline state transitions for the per-person conversation closing hint."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from social_lite import SocialLite


class CompanionClosingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="companion-closing-", dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.social = SocialLite(root / "state.json", root / "activity.log", self_id="99",
                                 ambient_reply_weight=0.0)
        self.clock = mock.patch("social_lite.time.time", return_value=1000.0)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        network = mock.patch("social_lite.urllib.request.urlopen", side_effect=AssertionError("offline test"))
        network.start()
        self.addCleanup(network.stop)

    def event(self, text="我先睡了，晚安", uid="1", gid="10", mid="100"):
        return {"group_id": gid, "user_id": uid, "message_id": mid, "raw_message": text}

    def test_explicit_farewells_are_recognized(self):
        for text in ("睡了", "先忙", "我先走了", "我去睡觉了，晚安", "八千代，先忙一下", "[CQ:at,qq=99] 我下线了"):
            with self.subTest(text=text):
                self.assertTrue(self.social._is_closing_text(text))

    def test_questions_quotes_negations_and_continuations_do_not_close(self):
        for text in ("睡了吗", "睡了？", "别睡了", "他说睡了", "他说：睡了", "“睡了”",
                     "睡了但还想聊", "先忙，不过还有个问题", "睡了\n等等我还想说一句", "我昨天睡了",
                     "你先忙", "八千代睡了", "晚安吧？"):
            with self.subTest(text=text):
                event = self.event(text)
                self.social.observe_closing(event)
                self.assertEqual("", self.social.closing_hint(event))

    def test_current_farewell_hint_is_short_without_followup(self):
        event = self.event()
        self.social.observe_closing(event)
        hint = self.social.closing_hint(event)
        self.assertIn("一句自然短答", hint)
        self.assertIn("别追加问题", hint)
        self.assertIn("八千代", hint)
        self.assertNotIn("我先睡了", hint)

    def test_other_user_keeps_talking_without_reopening_departed_person(self):
        self.social.observe_closing(self.event())
        other = self.event("今天有什么新进展？", uid="2", mid="101")
        self.social.observe_closing(other)
        hint = self.social.closing_hint(other)
        self.assertIn("暂已收尾的用户：1", hint)
        self.assertIn("其他人的交谈正常继续", hint)
        self.assertNotIn("当前说话人已明确收尾", hint)
        self.assertIn("1", self.social._group("10")["closing"])

    def test_same_person_new_message_reopens_only_own_group(self):
        self.social.observe_closing(self.event())
        self.social.observe_closing(self.event(gid="11", mid="201"))
        self.social.observe_closing(self.event("等等，还是再聊两句", mid="102"))
        self.assertNotIn("1", self.social._group("10")["closing"])
        self.assertIn("1", self.social._group("11")["closing"])

    def test_media_only_new_message_reopens(self):
        self.social.observe_closing(self.event())
        event = self.event("", mid="102")
        event["message"] = [{"type": "image", "data": {"file": "offline.png"}}]
        self.social.record_incoming(event)
        self.assertEqual({}, self.social._group("10")["closing"])

    def test_repeated_observation_does_not_extend_ttl(self):
        event = self.event()
        self.social.observe_closing(event)
        expires = self.social._group("10")["closing"]["1"]["expiresAt"]
        self.now.return_value = 1100.0
        self.social.record_incoming(event)
        self.assertEqual(expires, self.social._group("10")["closing"]["1"]["expiresAt"])

    def test_state_survives_restart_but_expires_after_six_hours(self):
        event = self.event()
        self.social.observe_closing(event)
        restarted = SocialLite(self.social.state_path, self.social.activity_path, self_id="99")
        self.assertIn("当前说话人已明确收尾", restarted.closing_hint(event))
        self.now.return_value = 1000.0 + restarted.CLOSING_TTL_SECONDS
        self.assertEqual("", restarted.closing_hint(event))
        self.assertEqual({}, restarted._group("10")["closing"])

    def test_retraction_removes_only_its_source(self):
        self.social.observe_closing(self.event())
        self.social.observe_closing(self.event(uid="2", mid="200"))
        self.social.observe_closing(self.event(gid="11", mid="300"))
        self.social.retract("10", "100")
        self.assertEqual({"2"}, set(self.social._group("10")["closing"]))
        self.assertEqual({"1"}, set(self.social._group("11")["closing"]))

    def test_old_retraction_does_not_remove_later_closing(self):
        self.social.observe_closing(self.event())
        self.social.observe_closing(self.event("还有一句", mid="101"))
        self.social.observe_closing(self.event("这次不聊了", mid="102"))
        # The recognizer deliberately ignores elaborations; use an explicit farewell.
        self.social.observe_closing(self.event("我先走了", mid="103"))
        self.social.retract("10", "100")
        self.assertEqual("message:103", self.social._group("10")["closing"]["1"]["sourceKey"])

    def test_forget_clears_closing_and_self_messages_do_not_reopen(self):
        self.social.observe_closing(self.event())
        self.social.record_outgoing("10", "行，明天再说。")
        self.social.observe_closing(self.event("晚安", uid="99", mid="101"))
        self.assertEqual({"1"}, set(self.social._group("10")["closing"]))
        self.social.forget_recent("10")
        self.assertEqual({}, self.social._group("10")["closing"])

    def test_recent_partner_can_receive_one_closing_reply_despite_cooldown(self):
        self.social.record_incoming(self.event("八千代，今天挺累的", mid="98"))
        self.social.record_outgoing("10", "今天这班可真够加码的。", reply_to_user_id="1")
        event = self.event()
        self.social.record_incoming(event)
        allowed, reason = self.social.should_reply(event)
        self.assertTrue(allowed)
        self.assertIn("closing", reason)
        self.assertEqual(0.0, self.social.ambient_reply_weight)
        self.assertEqual(0.0, self.social.spontaneous_probability)

    def test_other_person_farewell_does_not_inherit_recent_exchange(self):
        self.social.record_incoming(self.event("八千代，你在吗", uid="2", mid="98"))
        self.social.record_outgoing("10", "在呢。", reply_to_user_id="2")
        self.now.return_value = 1020.0
        event = self.event()
        self.social.record_incoming(event)
        self.assertFalse(self.social.should_reply(event)[0])

    def test_old_exchange_is_not_a_closing_summons(self):
        self.social.record_incoming(self.event("八千代，你在吗", mid="98"))
        self.social.record_outgoing("10", "在呢。", reply_to_user_id="1")
        self.now.return_value = 1091.0
        event = self.event()
        self.social.record_incoming(event)
        self.assertFalse(self.social.should_reply(event)[0])

    def test_farewell_state_is_bounded_without_background_work(self):
        for value in range(self.social.MAX_CLOSING_USERS + 3):
            self.now.return_value = 1000.0 + value
            self.social.observe_closing(self.event(uid=str(value + 100), mid=str(value)))
        self.assertEqual(self.social.MAX_CLOSING_USERS, len(self.social._group("10")["closing"]))
        self.assertEqual({}, self.social._timers)
        self.assertEqual({}, self.social._pending)

    def test_async_reply_does_not_give_bystander_the_original_speakers_turn(self):
        self.social.record_incoming(self.event("八千代，你在吗", uid="1", mid="97"))
        self.social.record_incoming(self.event("我只是路过", uid="2", mid="98"))
        self.social.record_outgoing("10", "在，怎么了。", reply_to_user_id="1")
        self.assertTrue(self.social.should_reply(self.event(uid="1", mid="101"))[0])
        self.assertFalse(self.social.should_reply(self.event(uid="2", mid="102"))[0])

    def test_old_reply_record_without_recipient_does_not_guess_a_partner(self):
        self.social.record_incoming(self.event("八千代，你在吗", mid="98"))
        self.social.record_outgoing("10", "在呢。")
        self.assertFalse(self.social.should_reply(self.event())[0])


if __name__ == "__main__":
    unittest.main()
