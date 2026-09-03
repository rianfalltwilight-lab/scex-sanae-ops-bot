#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from advancement_localization import AdvancementLocalizer


class AdvancementLocalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "mods").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def make_jar(self):
        en = {
            "advancements.test.unnatural.title": "Unnatural",
            "advancements.test.unnatural.description": "Build the forbidden machine",
            "adv.test.goal.title": "Limitless Potential",
            "adv.test.goal.desc": "Reach the final energy tier",
        }
        zh = {
            "advancements.test.unnatural.title": "违背自然",
            "advancements.test.unnatural.description": "建造禁忌机器",
            "adv.test.goal.title": "无限潜能",
            "adv.test.goal.desc": "抵达最终能源等级",
        }
        path = self.root / "mods" / "fixture.jar"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("assets/test/lang/en_us.json", json.dumps(en))
            archive.writestr("assets/test/lang/zh_cn.json", json.dumps(zh, ensure_ascii=False))
            archive.writestr("data/test/advancement/unnatural.json", json.dumps({"display": {
                "title": {"translate": "advancements.test.unnatural.title"},
                "description": {"translate": "advancements.test.unnatural.description"},
            }}))
        return path

    def test_scans_title_and_acquisition_description(self):
        self.make_jar()
        localizer = AdvancementLocalizer(self.root, self.root / "state" / "cache.json")
        title, description = localizer.localize("Unnatural")
        self.assertEqual("违背自然", title)
        self.assertEqual("建造禁忌机器", description)
        rendered = localizer.format_event("[怀旧]", "Foo", "progress", "Unnatural")
        self.assertEqual("[怀旧] 玩家 Foo 达成进度【违背自然】\n获取方式：建造禁忌机器", rendered)

    def test_goal_challenge_manual_override_and_cache(self):
        self.make_jar()
        cache = self.root / "state" / "cache.json"
        localizer = AdvancementLocalizer(
            self.root, cache, manual_titles={"Limitless Potential": "潜能无界"})
        goal = localizer.format_event("[副服]", "Bar", "goal", "Limitless Potential")
        self.assertIn("达成目标【潜能无界】", goal)
        self.assertIn("获取方式：抵达最终能源等级", goal)
        self.assertTrue(cache.is_file())
        second = AdvancementLocalizer(
            self.root, cache, manual_titles={"Limitless Potential": "潜能无界"})
        self.assertEqual(("潜能无界", "抵达最终能源等级"),
                         second.localize("Limitless Potential"))
        challenge = second.format_event("[副服]", "Bar", "challenge", "Unknown Title")
        self.assertEqual("[副服] 玩家 Bar 完成挑战【未收录的模组进度】\n"
                         "获取方式：请在游戏内进度界面查看该项具体条件",
                         challenge)

    def test_vanilla_title_lookup_ignores_case_and_has_official_description(self):
        localizer = AdvancementLocalizer(self.root, self.root / "state" / "official.json")
        self.assertEqual(("钻石护体", "钻石盔甲能救人"),
                         localizer.localize("Cover Me with Diamonds"))
        self.assertEqual(("我从哪儿来？", "繁殖一对动物"),
                         localizer.localize("The Parrots and the Bats"))

    def test_english_only_mod_description_uses_chinese_fallback(self):
        en = {
            "advancements.only_en.title": "No Chinese Pack",
            "advancements.only_en.description": "Do the mod thing",
        }
        path = self.root / "mods" / "english-only.jar"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("assets/only_en/lang/en_us.json", json.dumps(en))
        localizer = AdvancementLocalizer(self.root, self.root / "state" / "english-only.json")
        self.assertEqual(
            "[副服] 玩家 Bar 达成进度【未收录的模组进度】\n"
            "获取方式：服务器资源仅提供外文条件，请在游戏内进度界面查看",
            localizer.format_event("[副服]", "Bar", "progress", "No Chinese Pack"))

    def test_dedicated_server_vanilla_fallback_has_method(self):
        localizer = AdvancementLocalizer(self.root, self.root / "state" / "vanilla.json")
        title, description = localizer.localize("Sweet Dreams")
        self.assertEqual("甜蜜的梦", title)
        self.assertEqual("在床上睡觉以改变你的重生点", description)

    def test_bad_advancement_json_does_not_discard_valid_language_in_same_jar(self):
        path = self.make_jar()
        with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("data/test/advancement/broken.json", "{not json")
        localizer = AdvancementLocalizer(self.root, self.root / "state" / "resilient.json")
        self.assertEqual(("违背自然", "建造禁忌机器"), localizer.localize("Unnatural"))


if __name__ == "__main__":
    unittest.main()
