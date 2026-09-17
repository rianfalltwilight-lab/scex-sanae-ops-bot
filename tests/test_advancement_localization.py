#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from advancement_localization import AdvancementLocalizer,_translation_component,_render_component


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
        goal = localizer.format_event("[nast]", "Bar", "goal", "Limitless Potential")
        self.assertIn("达成目标【潜能无界】", goal)
        self.assertIn("获取方式：抵达最终能源等级", goal)
        self.assertTrue(cache.is_file())
        second = AdvancementLocalizer(
            self.root, cache, manual_titles={"Limitless Potential": "潜能无界"})
        self.assertEqual(("潜能无界", "抵达最终能源等级"),
                         second.localize("Limitless Potential"))
        challenge = second.format_event("[nast]", "Bar", "challenge", "Unknown Title")
        self.assertEqual("[nast] 玩家 Bar 完成挑战【Unknown Title】（暂缺中文译名）\n"
                         "获取方式：请在游戏内进度界面查看该项具体条件",
                         challenge)

    def test_builtin_vanilla_fallback_without_client_language_assets(self):
        localizer = AdvancementLocalizer(self.root, self.root / "state" / "official.json")
        self.assertEqual(("用钻石包裹我", ""),
                         localizer.localize("Cover Me with Diamonds"))
        self.assertEqual(("鹦鹉和蝙蝠", ""),
                         localizer.localize("The Parrots and the Bats"))

    def test_english_only_mod_preserves_title_and_description(self):
        en = {
            "advancements.only_en.title": "No Chinese Pack",
            "advancements.only_en.description": "Do the mod thing",
        }
        path = self.root / "mods" / "english-only.jar"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("assets/only_en/lang/en_us.json", json.dumps(en))
        localizer = AdvancementLocalizer(self.root, self.root / "state" / "english-only.json")
        self.assertEqual(
            "[nast] 玩家 Bar 达成进度【No Chinese Pack】（暂缺中文译名）\n"
            "进度说明（原文）：Do the mod thing",
            localizer.format_event("[nast]", "Bar", "progress", "No Chinese Pack"))

    def test_loose_translation_refreshes_preexisting_english_cache(self):
        en={"advancement.fixture.new.title":"A New Goal",
            "advancement.fixture.new.description":"Reach the new goal"}
        with zipfile.ZipFile(self.root/'mods/english.jar','w') as archive:
            archive.writestr('assets/fixture/lang/en_us.json',json.dumps(en))
        localizer=AdvancementLocalizer(self.root,self.root/'state/cache.json')
        self.assertIn('暂缺中文译名',localizer.format_event('[怀旧]','Foo','goal','A New Goal'))
        pack=self.root/'resourcepacks/translation/assets/fixture/lang/zh_cn.json'
        pack.parent.mkdir(parents=True)
        pack.write_text(json.dumps({"advancement.fixture.new.title":"新的目标",
                                   "advancement.fixture.new.description":"达到新的目标"}),encoding='utf-8')
        localizer._last_check=0
        self.assertEqual(('新的目标','达到新的目标'),localizer.localize('A New Goal'))
        second=AdvancementLocalizer(self.root,self.root/'state/cache.json')
        self.assertEqual(('新的目标','达到新的目标'),second.localize('A New Goal'))

    def test_known_chinese_title_keeps_english_description_as_original(self):
        self.make_jar()
        localizer=AdvancementLocalizer(self.root,self.root/'state/cache.json',manual_titles={'Only a title':'只有标题'})
        localizer.refresh()
        localizer._descriptions['Only a title']='Original condition'
        self.assertEqual('[怀旧] 玩家 Foo 达成进度【只有标题】\n进度说明（原文）：Original condition',
                         localizer.format_event('[怀旧]','Foo','progress','Only a title'))

    def test_empty_title_does_not_claim_unregistered_mod(self):
        localizer=AdvancementLocalizer(self.root,self.root/'state/cache.json')
        self.assertIn('【未命名进度】',localizer.format_event('[怀旧]','Foo','progress',''))

    def test_component_arguments_numbered_repeated_nested_and_percent(self):
        component=_translation_component([{'translate':'test','with':[{'translate':'item.test'},'B'],
                                          'extra':[{'text':'!'}]},'尾'])
        self.assertEqual('B / 宝剑 / 宝剑 / 100%!尾',
            _render_component(component,{'test':'%2$s / %1$s / %s / 100%%','item.test':'宝剑'}))

    def _write_records(self, records, en, zh):
        with zipfile.ZipFile(self.root/'mods/records.jar','w') as archive:
            archive.writestr('assets/test/lang/en_us.json',json.dumps(en))
            archive.writestr('assets/test/lang/zh_cn.json',json.dumps(zh))
            for name,title,desc in records:
                archive.writestr('data/test/advancement/'+name+'.json',json.dumps({'display':{
                    'title':title,'description':desc}}))
        return AdvancementLocalizer(self.root,self.root/'state/cache.json')

    def test_translated_alias_does_not_replace_original_numeric_title(self):
        localizer=self._write_records([
            ('cooking',{'translate':'adv.test.cooking.title'},{'translate':'adv.test.cooking.desc'}),
            ('maid',{'translate':'adv.test.maid.title'},{'translate':'adv.test.maid.desc'})],
            {'adv.test.cooking.title':'996','adv.test.cooking.desc':'Drive the mill',
             'adv.test.maid.title':'Keep Track of Time','adv.test.maid.desc':'Switch schedule'},
            {'adv.test.cooking.title':'996','adv.test.cooking.desc':'驱动磨盘',
             'adv.test.maid.title':'996','adv.test.maid.desc':'切换日程表'})
        self.assertEqual(('996','驱动磨盘'),localizer.localize('996'))
        self.assertEqual(('996','切换日程表'),localizer.localize('Keep Track of Time'))
        self.assertNotIn('暂缺中文译名',localizer.format_event('[怀旧]','Foo','progress','996'))

    def test_identical_english_titles_keep_possible_item_conditions(self):
        records=[(x,{'translate':'adv.test.'+x+'.title'},
                  {'translate':'adv.test.unique.desc','with':[{'translate':'item.test.'+x}]}) for x in ['a','b']]
        en={'adv.test.a.title':'Ancient Technology','adv.test.b.title':'Ancient Technology',
            'adv.test.unique.desc':'Obtain %s','item.test.a':'Sword','item.test.b':'Spear'}
        zh={'adv.test.a.title':'古代科技','adv.test.b.title':'古代科技',
            'adv.test.unique.desc':'获得%s','item.test.a':'宝剑','item.test.b':'长矛'}
        localizer=self._write_records(records,en,zh)
        title,desc=localizer.localize('Ancient Technology')
        self.assertEqual('古代科技',title)
        self.assertIn('同名进度',desc);self.assertIn('获得宝剑',desc);self.assertIn('获得长矛',desc)
        self.assertEqual(('古代科技','获得宝剑'),localizer.localize('adv.test.a.title'))

    def test_loose_advancement_definition_renders_parameter_and_invalidates_cache(self):
        localizer=self._write_records([],{'item.test.sword':'Sword'},{'item.test.sword':'宝剑'})
        localizer.refresh()
        base=self.root/'world/datapacks/custom/data/test/advancement'
        base.mkdir(parents=True)
        (base/'loose.json').write_text(json.dumps({'display':{'title':{'text':'松散进度'},
            'description':[{'text':'获得'},{'translate':'item.test.sword'}]}}),encoding='utf-8')
        localizer._last_check=0
        self.assertEqual(('松散进度','获得宝剑'),localizer.localize('松散进度'))

    def test_old_cache_schema_cannot_keep_lost_arguments(self):
        self.make_jar()
        path=self.root/'state/cache.json'
        localizer=AdvancementLocalizer(self.root,path)
        localizer.refresh()
        old=json.loads(path.read_text(encoding='utf-8'))
        old['schema']=1;old.pop('records')
        path.write_text(json.dumps(old),encoding='utf-8')
        second=AdvancementLocalizer(self.root,path)
        self.assertEqual(('违背自然','建造禁忌机器'),second.localize('Unnatural'))
        self.assertEqual(2,json.loads(path.read_text(encoding='utf-8'))['schema'])

    def test_dedicated_server_vanilla_fallback_has_method(self):
        localizer = AdvancementLocalizer(self.root, self.root / "state" / "vanilla.json")
        title, description = localizer.localize("Sweet Dreams")
        self.assertEqual("甜蜜的梦", title)
        self.assertEqual("在床上睡一觉以改变你的重生点", description)

    def test_bad_advancement_json_does_not_discard_valid_language_in_same_jar(self):
        path = self.make_jar()
        with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("data/test/advancement/broken.json", "{not json")
        localizer = AdvancementLocalizer(self.root, self.root / "state" / "resilient.json")
        self.assertEqual(("违背自然", "建造禁忌机器"), localizer.localize("Unnatural"))


if __name__ == "__main__":
    unittest.main()
