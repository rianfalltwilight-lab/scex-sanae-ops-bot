"""Closing state must retain user/source scope when shared excerpts are cleared."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from social_lite import SocialLite


class ClosingForgetScopeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.social = SocialLite(root / "state.json", root / "activity.log", self_id="99")
        clock = mock.patch("social_lite.time.time", return_value=1000)
        clock.start()
        self.addCleanup(clock.stop)
        network = mock.patch("social_lite.urllib.request.urlopen", side_effect=AssertionError("offline test"))
        network.start()
        self.addCleanup(network.stop)
        for uid, gid, mid in (("1", "10", "100"), ("2", "10", "200"), ("1", "20", "300")):
            self.social.record_incoming({"group_id": gid, "user_id": uid,
                "message_id": mid, "raw_message": "我先睡了"})

    def closed(self, group="10"):
        return set(self.social._group(group)["closing"])

    def test_explicit_forget_removes_only_sender_closing_but_clears_shared_excerpts(self):
        self.social.forget_recent("10", user_id="1")
        self.assertEqual(self.closed(), {"2"})
        self.assertEqual(self.closed("20"), {"1"})
        self.assertEqual(self.social._group("10")["recent"], [])

    def test_unrelated_recall_then_shared_cache_clear_does_not_reopen_anyone(self):
        self.social.retract("10", "999")
        self.social.forget_recent("10", user_id="1", clear_closing=False)
        self.assertEqual(self.closed(), {"1", "2"})
        self.assertEqual(self.social._group("10")["recent"], [])

    def test_recall_own_farewell_removes_only_exact_source(self):
        self.social.retract("10", "100")
        self.social.forget_recent("10", user_id="1", clear_closing=False)
        self.assertEqual(self.closed(), {"2"})
        self.assertEqual(self.closed("20"), {"1"})

    def test_admin_recall_does_not_clear_the_admins_own_closing(self):
        # Actor identity is irrelevant: only the recalled message owns this state.
        self.social.retract("10", "100")
        self.social.forget_recent("10", user_id="2", clear_closing=False)
        self.assertEqual(self.closed(), {"2"})

    def test_old_default_group_forget_still_clears_group_closing(self):
        self.social.forget_recent("10")
        self.assertEqual(self.closed(), set())
        self.assertEqual(self.closed("20"), {"1"})


if __name__ == "__main__":
    unittest.main()
