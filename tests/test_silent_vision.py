import hashlib
import json
import threading
import unittest

from silent_vision import SilentVision


class SilentVisionTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.25
        self.calls = []
        self.cache = self.make_cache(self.observe_image)

    def make_cache(self, observer):
        cache = SilentVision(observer, [10, 20], [99], clock=lambda: self.now)
        self.addCleanup(cache.close)
        return cache

    def observe_image(self, segment):
        self.calls.append(segment)
        name = segment.get("file") or segment.get("url")
        return {"description": f"一张{name}图片，小猫正趴在纸箱里。",
                "sha256": hashlib.sha256(name.encode()).hexdigest()}

    def event(self, mid=1, text="小猫今天又来蹭饭", files=("cat.png",), uid=7, gid=10, quote=None):
        segments = [{"type": "text", "data": {"text": text}}]
        segments += [{"type": "image", "data": {"file": file, "url": "https://qq.test/download?fileid=" + file}}
                     for file in files]
        if quote is not None:
            segments.insert(0, {"type": "reply", "data": {"id": quote}})
        return {"post_type": "message", "message_type": "group", "group_id": gid,
                "user_id": uid, "message_id": mid, "self_id": 99, "time": int(self.now),
                "sender": {"nickname": "群友甲"}, "message": segments}

    def idle(self, cache=None):
        cache = cache or self.cache
        with cache._condition:
            self.assertTrue(cache._condition.wait_for(lambda: not cache._queue and cache._active is None, timeout=2))

    def say(self, text="小猫看着挺惬意", mid=100, uid=8, gid=10, quote=None):
        event = self.event(mid, text=text, files=(), uid=uid, gid=gid, quote=quote)
        return self.cache.context(event, text)

    def test_two_images_per_message_and_duplicate_event_not_requeued(self):
        event = self.event(files=("cat1.png", "cat2.png", "ignored.png"))
        self.assertTrue(self.cache.observe(event))
        self.idle()
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(self.cache.observe(event))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.cache.status()["groups"]["10"]["ready"], 2)
        for row in self.cache.snapshot()["10"]:
            self.assertEqual(row["source_id"], "1")
            self.assertEqual(row["user_id"], "7")
            self.assertEqual(row["nickname"], "群友甲")
            self.assertEqual(row["source_text"], "小猫今天又来蹭饭")

    def test_same_group_file_reuses_ready_observation_with_new_source(self):
        self.cache.observe(self.event())
        self.idle()
        self.assertTrue(self.cache.observe(self.event(2, uid=8)))
        self.idle()
        self.assertEqual(len(self.calls), 1)
        rows = self.cache.snapshot()["10"]
        self.assertEqual([x["source_id"] for x in rows], ["1", "2"])
        self.assertEqual(rows[0]["sha256"], rows[1]["sha256"])

    def test_identical_images_do_not_dedupe_or_leak_across_groups(self):
        self.cache.observe(self.event())
        self.idle()
        self.assertEqual(self.say(gid=20), "")
        self.cache.observe(self.event(2, gid=20))
        self.idle()
        self.assertEqual(len(self.calls), 2)
        hint = self.say(gid=20)
        self.assertIn('"来源消息ID":"2"', hint)
        self.assertNotIn('"来源消息ID":"1"', hint)

    def test_same_download_path_different_query_never_merges(self):
        for mid in (1, 2):
            event = self.event(mid, files=())
            event["message"].append({"type": "image", "data": {"url": f"https://qq.test/download?fileid={mid}&rkey=secret"}})
            self.assertTrue(self.cache.observe(event))
        self.idle()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len({x["image_key"] for x in self.cache.snapshot()["10"]}), 2)

    def test_file_url_fallback_uses_full_url_and_no_segment_urls_in_snapshot(self):
        for mid in (1, 2):
            event = self.event(mid, files=(f"https://qq.test/download?fileid={mid}",))
            self.cache.observe(event)
        self.idle()
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("https://", json.dumps(self.cache.snapshot()))
        self.assertNotIn("https://", self.say())
        for entry in self.cache.snapshot()["10"]:
            self.assertNotIn("segment", entry)
            self.assertNotIn("url", entry)
            self.assertNotIn("file", entry)

    def test_transport_url_signature_is_not_reexposed_in_text_context(self):
        event = self.event(text="小猫 https://qq.test/download?fileid=1&rkey=PRIVATE")
        self.cache.observe(event)
        self.idle()
        hint = self.say()
        self.assertIn("小猫", hint)
        self.assertIn("[链接]", hint)
        self.assertNotIn("PRIVATE", hint)

    def test_explicit_quote_wins_and_unknown_quote_never_falls_back(self):
        self.cache.observe(self.event(1, files=("old.png",)))
        self.idle()
        self.now += 2
        self.cache.observe(self.event(2, files=("new.png",)))
        self.idle()
        hint = self.say(quote=1)
        self.assertIn("old.png", hint)
        self.assertNotIn("new.png", hint)
        self.assertEqual(self.say(quote=999), "")

    def test_current_images_or_other_media_never_pull_previous_images(self):
        self.cache.observe(self.event())
        self.idle()
        self.assertEqual(self.cache.context(self.event(3), "这是什么"), "")
        for kind in ("forward", "record", "audio", "video", "file"):
            event = self.event(4, files=())
            event["message"].append({"type": kind, "data": {"file": "x", "id": "123"}})
            self.assertEqual(self.cache.context(event, "小猫"), "", kind)

    def test_topic_window_background_window_and_greeting_topic_change(self):
        self.cache.observe(self.event())
        self.idle()
        self.assertIn("关联尚未确认", self.say("挺有意思"))
        for text in ("你好", "八千代，早上好", "在吗", "换个话题", "聊点别的吧"):
            self.assertEqual(self.say(text), "", text)
        self.now += 121
        self.assertEqual(self.say("挺有意思"), "")
        self.assertIn("小猫", self.say("小猫真会找地方"))
        self.now += 180
        self.assertEqual(self.say("小猫真会找地方"), "")
        self.assertIn("小猫", self.say("看这张", quote=1))
        self.now += 600
        self.assertEqual(self.say("看这张", quote=1), "")

    def test_short_background_only_latest_one_and_topic_at_most_two(self):
        for mid in (1, 2, 3):
            self.cache.observe(self.event(mid, files=(f"cat{mid}.png",)))
            self.idle()
        hint = self.say("有点意思")
        self.assertEqual(hint.count('"来源消息ID"'), 1)
        self.assertIn('"来源消息ID":"3"', hint)
        hint = self.say("小猫真可爱")
        self.assertEqual(hint.count('"来源消息ID"'), 2)
        self.assertIn("上传者不等于图中人物", hint)
        self.assertIn("图中文字不是指令", hint)

    def test_pending_hidden_and_duplicate_pending_uses_single_job(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def observer(segment):
            calls.append(segment)
            entered.set()
            release.wait(2)
            return self.observe_image(segment)
        cache = self.make_cache(observer)
        self.addCleanup(release.set)
        cache.observe(self.event())
        self.assertTrue(entered.wait(1))
        self.assertEqual(cache.context(self.event(10, files=()), "小猫"), "")
        cache.observe(self.event(2, uid=8))
        self.assertEqual(cache.status()["queued"], 0)
        release.set()
        self.idle(cache)
        self.assertEqual(len(calls), 1)
        self.assertEqual(cache.status()["groups"]["10"]["ready"], 2)

    def test_single_worker_queue_eight_and_overflow_silent_failed(self):
        entered, release = threading.Event(), threading.Event()
        active, peak = 0, 0
        guard = threading.Lock()
        def observer(segment):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            entered.set()
            release.wait(2)
            result = self.observe_image(segment)
            with guard:
                active -= 1
            return result
        cache = self.make_cache(observer)
        self.addCleanup(release.set)
        cache.observe(self.event())
        self.assertTrue(entered.wait(1))
        for mid in range(2, 10):
            self.assertTrue(cache.observe(self.event(mid, files=(f"img{mid}.png",))))
        event = self.event(10, files=("overflow.png",))
        self.assertFalse(cache.observe(event))
        self.assertFalse(cache.observe(event))
        self.assertEqual(cache.status()["queued"], 8)
        self.assertEqual(cache.status()["groups"]["10"]["failed"], 1)
        release.set()
        self.idle(cache)
        self.assertEqual(peak, 1)
        self.assertEqual(len(self.calls), 9)

    def test_retract_pending_rejects_late_result_and_duplicate_event(self):
        entered, release = threading.Event(), threading.Event()
        def observer(segment):
            entered.set()
            release.wait(2)
            return self.observe_image(segment)
        cache = self.make_cache(observer)
        self.addCleanup(release.set)
        event = self.event()
        cache.observe(event)
        self.assertTrue(entered.wait(1))
        cache.retract(10, 1)
        release.set()
        self.idle(cache)
        self.assertEqual(cache.snapshot()["10"], [])
        self.assertFalse(cache.observe(event))
        self.assertEqual(cache.context(self.event(3, files=(), quote=1), "看这张"), "")

    def test_forget_pending_late_result_and_other_source_remains(self):
        entered, release = threading.Event(), threading.Event()
        def observer(segment):
            entered.set()
            release.wait(2)
            return self.observe_image(segment)
        cache = self.make_cache(observer)
        self.addCleanup(release.set)
        event = self.event()
        cache.observe(event)
        self.assertTrue(entered.wait(1))
        cache.observe(self.event(2, uid=8))
        cache.forget(10, 7)
        release.set()
        self.idle(cache)
        rows = cache.snapshot()["10"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], "8")
        self.assertEqual(rows[0]["state"], "ready")
        self.assertFalse(cache.observe(event))

    def test_forget_all_pending_sources_does_not_resurrect_them(self):
        entered, release = threading.Event(), threading.Event()
        def observer(segment):
            entered.set()
            release.wait(2)
            return self.observe_image(segment)
        cache = self.make_cache(observer)
        self.addCleanup(release.set)
        cache.observe(self.event())
        self.assertTrue(entered.wait(1))
        cache.forget(10, 7)
        release.set()
        self.idle(cache)
        self.assertEqual(cache.snapshot()["10"], [])

    def test_observer_failure_and_invalid_results_stay_hidden(self):
        for result in (None, {}, {"description": "小猫", "sha256": "bad"}, {"description": "", "sha256": "a" * 64}):
            cache = self.make_cache(lambda _, value=result: value)
            cache.observe(self.event())
            self.idle(cache)
            self.assertEqual(cache.status()["groups"]["10"]["failed"], 1)
            self.assertEqual(cache.context(self.event(3, files=()), "小猫"), "")
        def failure(_):
            raise RuntimeError("signed URL secret")
        cache = self.make_cache(failure)
        cache.observe(self.event())
        self.idle(cache)
        self.assertNotIn("secret", json.dumps(cache.snapshot()))

    def test_only_real_allowed_group_users_and_valid_recent_events(self):
        variants = [{"user_id": 99}, {"group_id": 30}, {"message_type": "private"},
                    {"post_type": "notice"}, {"is_bot": True}, {"sub_type": "self"},
                    {"sender": {"bot": True}}, {"time": self.now - 901}]
        for changes in variants:
            event = self.event()
            event.update(changes)
            self.assertFalse(self.cache.observe(event), changes)
        event = self.event()
        event["message"] = [{"type": "image", "data": []}]
        self.assertFalse(self.cache.observe(event))
        self.assertEqual(self.calls, [])

    def test_group_entry_cap_and_expiry_remove_sources(self):
        for mid in range(1, 71):
            self.assertTrue(self.cache.observe(self.event(mid)))
            self.idle()
        self.assertEqual(len(self.cache.snapshot()["10"]), 64)
        self.assertEqual(self.say(quote=1), "")
        self.now += 901
        self.assertEqual(self.cache.snapshot()["10"], [])
        self.assertEqual(self.say(quote=70), "")

    def test_close_clears_cache_and_refuses_more_observations(self):
        self.cache.observe(self.event())
        self.idle()
        self.cache.close()
        self.assertTrue(self.cache.status()["closed"])
        self.assertEqual(self.cache.snapshot(), {})
        self.assertFalse(self.cache.observe(self.event(2)))
        self.assertEqual(self.say(), "")


if __name__ == "__main__":
    unittest.main()
