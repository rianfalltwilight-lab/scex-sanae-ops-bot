#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import datetime as dt
import importlib
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAINT = os.path.join(HERE, "maintenance")
if MAINT not in sys.path:
    sys.path.insert(0, MAINT)

r = importlib.import_module("backup_retention")


def d(days_ago: int, hour: int = 12, minute: int = 0) -> dt.datetime:
    base = dt.datetime(2026, 8, 17, 15, 0, 0)
    return base - dt.timedelta(days=days_ago, hours=base.hour - hour, minutes=base.minute - minute)


def name(days_ago: int, hour: int = 12, minute: int = 0) -> str:
    ts = d(days_ago, hour, minute)
    return ts.strftime("world_%Y-%m-%d_%H-%M-%S.zip")


class ParseNameTests(unittest.TestCase):
    def test_valid(self):
        ts = r.parse_name("world_2026-08-17_12-30-45.zip")
        self.assertEqual(ts, dt.datetime(2026, 8, 17, 12, 30, 45))

    def test_bad_names_return_none(self):
        for bad in ("world_2026-13-01_00-00-00.zip", "world_2026-08-17.zip",
                    "backup.zip", "world_2026-08-17_12-30-45", "", "world_2026-08-17_25-00-00.zip"):
            self.assertIsNone(r.parse_name(bad))

    def test_basename_strips_path(self):
        self.assertEqual(r.basename("/some/dir/world_2026-08-17_12-30-45.zip"),
                         "world_2026-08-17_12-30-45.zip")


class PlanAnchorsTests(unittest.TestCase):
    NOW = dt.datetime(2026, 8, 17, 15, 0, 0)

    def test_newest_n_anchor(self):
        # 10 天前的 5 份，只有最新 3 份保留
        names = [name(10, hour=h) for h in (1, 2, 3, 4, 5)]
        result = r.plan(names, newest_n=3, daily_d=0, weekly_w=0, now=self.NOW)
        self.assertEqual(sorted(result["keep"]), sorted([name(10, h) for h in (3, 4, 5)]))
        self.assertEqual(sorted(result["delete"]), sorted([name(10, h) for h in (1, 2)]))

    def test_daily_last_per_day(self):
        # 昨天 3 份、前天 2 份，每天只保留最后一份
        names = [name(1, hour=1), name(1, hour=2), name(1, hour=3),
                 name(2, hour=1), name(2, hour=2)]
        result = r.plan(names, newest_n=0, daily_d=3, weekly_w=0, now=self.NOW)
        self.assertIn(name(1, hour=3), result["keep"])
        self.assertIn(name(2, hour=2), result["keep"])
        self.assertNotIn(name(1, hour=1), result["keep"])
        self.assertNotIn(name(2, hour=1), result["keep"])

    def test_weekly_last_per_week(self):
        # 上一周（ISO 周）多份，只保留最后一份
        week_ago = [name(7, hour=1), name(7, hour=2)]
        two_weeks = [name(14, hour=1), name(14, hour=2)]
        result = r.plan(week_ago + two_weeks, newest_n=0, daily_d=0, weekly_w=3, now=self.NOW)
        self.assertIn(name(7, hour=2), result["keep"])
        self.assertNotIn(name(7, hour=1), result["keep"])
        self.assertIn(name(14, hour=2), result["keep"])
        self.assertNotIn(name(14, hour=1), result["keep"])

    def test_old_backups_fall_to_delete(self):
        names = [name(90), name(91)]
        result = r.plan(names, newest_n=1, daily_d=7, weekly_w=4, now=self.NOW)
        # 只有最新一份因 newest 保留，另一份删除
        self.assertIn(name(90), result["keep"])
        self.assertIn(name(91), result["delete"])


class PlanRobustnessTests(unittest.TestCase):
    NOW = dt.datetime(2026, 8, 17, 15, 0, 0)

    def test_bad_names_go_to_warnings_not_delete(self):
        result = r.plan(["garbage.zip", name(1)], newest_n=1, daily_d=0, weekly_w=0, now=self.NOW)
        self.assertIn("garbage.zip", result["invalid_names"])
        self.assertTrue(any("无法解析" in w for w in result["warnings"]))
        self.assertNotIn("garbage.zip", result["delete"])
        self.assertIn("garbage.zip", result["invalid_names"])

    def test_protected_always_kept_even_if_bad_name(self):
        result = r.plan([name(1), "manual.zip"], newest_n=1, daily_d=0, weekly_w=0,
                        protected={"manual.zip"}, now=self.NOW)
        self.assertIn("manual.zip", result["keep"])
        self.assertTrue(any("protected" in rr for rr in result["decisions"]["manual.zip"]["reasons"]))

    def test_protected_does_not_count_against_newest(self):
        # 保护最新一份，则 newest=1 仍应保留「第二新」的另一份
        names = [name(10, hour=4), name(10, hour=5)]
        protected = {name(10, hour=5)}
        result = r.plan(names, newest_n=1, daily_d=0, weekly_w=0,
                        protected=protected, now=self.NOW)
        self.assertIn(name(10, hour=4), result["keep"])
        self.assertIn(name(10, hour=5), result["keep"])

    def test_empty_input(self):
        result = r.plan([], now=self.NOW)
        self.assertEqual(result["keep"], [])
        self.assertEqual(result["delete"], [])


class NoMutationTests(unittest.TestCase):
    """验证工具不具备删除/落地能力。"""

    NOW = dt.datetime(2026, 8, 17, 15, 0, 0)

    def test_no_apply_or_delete_function(self):
        src = r.__file__
        with open(src, "r", encoding="utf-8") as f:
            text = f.read()
        for forbidden in ("os.remove", "os.unlink", "shutil.rmtree", "Path.unlink",
                          "def apply", "def delete", "--apply"):
            self.assertNotIn(forbidden, text, f"不应出现删除/落地痕迹：{forbidden}")

    def test_plan_has_no_side_effects(self):
        names = [name(1), name(2), name(3)]
        snapshot = list(names)
        r.plan(names, now=self.NOW)
        self.assertEqual(names, snapshot)

    def test_space_warning_only_warns(self):
        result = {"keep": [], "delete": [], "decisions": {}, "warnings": []}
        r.space_warning(result, threshold_gib=10.0, free_gib=5.0)
        self.assertTrue(any("低于阈值" in w for w in result["warnings"]))
        # 高于阈值不追加
        result2 = {"warnings": []}
        r.space_warning(result2, threshold_gib=10.0, free_gib=50.0)
        self.assertEqual(result2["warnings"], [])


if __name__ == "__main__":
    unittest.main()
