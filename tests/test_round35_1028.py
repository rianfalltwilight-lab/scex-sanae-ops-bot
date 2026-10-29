"""Round 35 (10-28 evening): recipe_lookup mode=machine. On the server's index 搅拌器, 注液器, 鼓风机,
工作盆, 魔力池 and 机械手 found no recipe type at all, and 熔炉 found only thermal:furnace (the Redstone
Furnace's). Offline: a small index shaped like the real one."""
import json
import tempfile
import unittest
from pathlib import Path

import lookup_tools

NAMES = {
    'create:whisk': {'zh': '搅拌器', 'en': 'Whisk'},
    'create:mechanical_mixer': {'zh': '动力搅拌器', 'en': 'Mechanical Mixer'},
    'createcasing:brass_mixer': {'zh': '黄铜动力搅拌器'},
    'create:mechanical_press': {'zh': '动力冲压机', 'en': 'Mechanical Press'},
    'create:basin': {'zh': '工作盆', 'en': 'Basin'},
    'create:encased_fan': {'zh': '鼓风机', 'en': 'Encased Fan'},
    'createcasing:brass_encased_fan': {'zh': '黄铜鼓风机'},
    'create:spout': {'zh': '注液器', 'en': 'Spout'},
    'create:millstone': {'zh': '石磨', 'en': 'Millstone'},
    'kaleidoscope_cookery:millstone': {'zh': '石磨', 'en': 'Millstone'},
    'botania:mana_pool': {'zh': '魔力池', 'en': 'Mana Pool'},
    'enderio:sag_mill': {'zh': '半自磨机', 'en': 'SAG Mill'},
    'enderio_evolution:basic_sag_mill': {'zh': '基础磨粉机'},
    'ae2:charger': {'zh': '充能器', 'en': 'Charger'},
    'thermal:machine_furnace': {'zh': '红石熔炼炉', 'en': 'Redstone Furnace'},
    'minecraft:furnace': {'zh': '熔炉', 'en': 'Furnace'},
    'minecraft:blast_furnace': {'zh': '高炉', 'en': 'Blast Furnace'},
    'mod:wheat': {'zh': '小麦'}, 'mod:flour': {'zh': '面粉'}, 'mod:bread': {'zh': '面包'},
}


def row(kind, result, source='mod.jar', **extra):
    return dict({'type': kind, 'result': result, 'count': 1, 'ingredients': ['mod:wheat'], 'source': source}, **extra)


INDEX = {'generated_at': 1793180000, 'names': NAMES, 'recipes': [
    row('create:mixing', 'mod:flour', heat='heated'), row('create:mixing', 'mod:bread'),
    row('create:compacting', 'mod:bread'), row('create:pressing', 'mod:flour'),
    row('create:splashing', 'mod:flour'), row('create:haunting', 'mod:bread'),
    row('create:filling', 'mod:bread'), row('create:milling', 'mod:flour'), row('create:milling', 'mod:bread'),
    row('kaleidoscope_cookery:millstone', 'mod:flour', 'kitchen.jar'),
    row('botania:mana_infusion', 'mod:bread'), row('enderio:sag_milling', 'mod:flour'),
    row('ae2:charger', 'mod:bread'), row('createaddition:charging', 'mod:bread'),
    row('thermal:furnace', 'mod:bread'), row('minecraft:smelting', 'mod:bread'), row('blasting', 'mod:bread'),
]}


class MachineModeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.index = Path(temp.name) / 'recipe-index.json'
        self.index.write_text(json.dumps(INDEX, ensure_ascii=False), encoding='utf-8')

    def machine(self, query):
        return lookup_tools.recipe_lookup(self.index, query, 'machine', names=NAMES)

    def head(self, query):
        return self.machine(query).splitlines()[0]

    def test_create_machines_find_their_types(self):
        # 搅拌器 is the whisk; the mixer's longer name counts when the exact one runs nothing.
        self.assertIn('create:mixing', self.head('搅拌器'))
        self.assertIn('配方类型：create:mixing', self.head('黄铜动力搅拌器'))
        self.assertIn('create:filling', self.head('注液器'))
        fan = self.head('黄铜鼓风机')
        self.assertIn('create:splashing', fan)
        self.assertIn('create:haunting', fan)
        basin = self.head('工作盆')
        self.assertIn('create:mixing', basin)
        self.assertIn('create:compacting', basin)
        self.assertIn('需要加热', self.machine('工作盆'))

    def test_old_names_and_labels(self):
        # 辊压机 is the old name of 动力冲压机.
        press = self.head('辊压机')
        self.assertIn('create:pressing（机械动力冲压（动力冲压机））1 条', press)
        self.assertIn('create:compacting', press)
        self.assertIn('create:mixing', self.head('搅拌机'))

    def test_same_name_in_two_mods_shows_both(self):
        text = self.machine('石磨')
        self.assertIn('共 3 条', text.splitlines()[0])
        rows = text.splitlines()[1:4]
        self.assertIn('机械动力石磨', rows[0])
        self.assertIn('石磨（kaleidoscope_cookery:millstone）', rows[1])
        self.assertIn('机械动力石磨', rows[2])

    def test_other_mods(self):
        self.assertIn('botania:mana_infusion（魔力池）', self.head('魔力池'))
        self.assertIn('enderio:sag_milling（半自磨机）', self.head('半自磨机'))
        self.assertIn('enderio:sag_milling（基础磨粉机）', self.head('基础磨粉机'))
        self.assertIn('enderio:sag_milling', self.head('SAG Mill'))
        self.assertIn('thermal:furnace（红石熔炼炉）', self.head('红石熔炼炉'))

    def test_one_shared_word_from_another_mod_is_not_enough(self):
        charger = self.head('充能器')
        self.assertIn('ae2:charger', charger)
        self.assertNotIn('createaddition', charger)
        furnace = self.machine('熔炉')
        self.assertIn('配方类型：minecraft:smelting（熔炉）1 条', furnace)
        self.assertNotIn('thermal:furnace', furnace)
        self.assertIn('原版自己的配方', furnace)
        # Datapacks may write "blasting" without the namespace.
        self.assertIn('minecraft:blasting（高炉）1 条', self.head('高炉'))

    def test_nothing_matched(self):
        self.assertIn('没对上', self.head('乐事'))


if __name__ == '__main__':
    unittest.main()
