"""Round 13: block entities, ServerCore, host memory/uptime, quest and Thaumcraft progress.

Command outputs are copied from the live server on 10-26. Remote scripts run locally on
written fixture files; nothing reaches RCON, SSH or QQ.
"""
import gzip
import json
import os
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tests.test_remote_collectors import run_local
from tests import test_remote_legacy_monitor as monitor_tests
from tests import test_world_status as world_tests
from tests import test_yachiyo_runtime as runtime_tests
import command_catalog as cc
import player_progress as pp
import remote_server_reader as reader
import sanae_local_ops
import world_status as ws

s = runtime_tests.s

BLOCKS = """Block Entities by Type
1. minecraft:sculk_sensor 4917
2. minecraft:sculk_catalyst 1252
3. dysoncubeproject:multiblock_structure 884
4. create:fluid_tank 556
5. minecraft:sculk_shrieker 490
6. lootr:lootr_chest 431
7. thaumcraft:aura_node 322
8. lootr:decorated_pot 302
<< Page 1 of 53 >>"""
BLOCKS_2 = """Block Entities by Type
9. enderio:conduit 288
10. ae2lt:overloaded_controller 245
16. <lithium_sleeping> 186
<< Page 2 of 53 >>"""
BLOCKS_BY_PLAYER = """Block Entities by Player
1. MioPha 3657
2. Enggoal 1297
3. Ales_winter 1072
<< Page 1 of 1 >>"""
ENTITIES_BY_PLAYER = """Entities by Player
1. twilighttowns 1027
2. L_Morningstar 473
<< Page 1 of 1 >>"""
SERVERCORE = """ServerCore
» Version: 1.5.19+1.21.1
» Mobcap percentage: 100%
» Chunk-tick distance: 10
» Simulation distance: 4
» View distance: 10"""
HOST = {'process': {'rss_kb': 7054432, 'cpu': 193.2, 'etime': '49:46', 'xmx': '10240M'},
        'heap': '55778:\n garbage-first heap   total 10485760K, used 5843351K [0x0000007000800000, 0x0000007280800000)\n'
                '  region size 8192K, 64 young (524288K), 6 survivors (49152K)\n'
                ' Metaspace       used 586900K, committed 593088K, reserved 1572864K\n',
        'uptime': '55778:\n2985.731 s\n', 'cores': 12, 'memory': 17179869184,
        'disk': [245054767104, 115301101568, 129753665536], 'collected_at': 1792997848.0}
NAMES = {'minecraft:sculk_sensor': {'zh': '幽匿感测体'}, 'enderio:conduit': {'zh': '<MISSING>导管'}}


class ParserTests(unittest.TestCase):
    def test_statistics_servercore_heap_and_elapsed_time(self):
        self.assertEqual([('minecraft:sculk_sensor', 4917), ('minecraft:sculk_catalyst', 1252)],
                         ws.parse_statistics(BLOCKS)[:2])
        self.assertEqual(8, len(ws.parse_statistics(BLOCKS)))
        self.assertEqual(('<lithium_sleeping>', 186), ws.parse_statistics(BLOCKS_2)[-1])
        self.assertEqual([('MioPha', 3657), ('Enggoal', 1297), ('Ales_winter', 1072)],
                         ws.parse_statistics(BLOCKS_BY_PLAYER))
        self.assertIsNone(ws.parse_statistics('Unknown or incomplete command'))
        self.assertEqual({'simulation': 4, 'view': 10, 'chunk_tick': 10, 'mobcap': 100}, ws.parse_servercore(SERVERCORE))
        self.assertIsNone(ws.parse_servercore(''))
        self.assertEqual({'total': 10485760, 'used': 5843351, 'metaspace': 586900}, ws.parse_heap(HOST['heap']))
        self.assertIsNone(ws.parse_heap('error TimeoutExpired'))
        self.assertEqual(2986, ws._elapsed_seconds('49:46'))
        self.assertEqual(93784, ws._elapsed_seconds('1-02:03:04'))
        self.assertIsNone(ws._elapsed_seconds('soon'))


class ReportTests(unittest.TestCase):
    def test_machines_report(self):
        text = ws.format_machines('[怀旧]', BLOCKS, BLOCKS_2, BLOCKS_BY_PLAYER, ENTITIES_BY_PLAYER, NAMES)
        self.assertIn('最多的种类：幽匿感测体（minecraft:sculk_sensor） 4917、minecraft:sculk_catalyst 1252', text)
        self.assertIn('休眠中的方块实体（Lithium 已暂停，几乎不耗算力） 186', text)
        self.assertIn('、导管（enderio:conduit） 288', text)
        self.assertIn('按在线玩家（统计各玩家周围已加载的区块）：MioPha 3657、Enggoal 1297、Ales_winter 1072', text)
        self.assertIn('实体按在线玩家：twilighttowns 1027、L_Morningstar 473', text)
        self.assertIn('暂时取不到', ws.format_machines('[怀旧]', ''))

    def test_host_report_leaves_disk_out(self):
        text = ws.format_host('[怀旧]', HOST)
        self.assertIn('- 已连续运行 49 分钟（上次启动于 10-26 14:07）', text)
        self.assertIn('- JVM 堆内存：已用 5.6 GB / 当前分配 10.0 GB（55%），上限 -Xmx10240M；类元数据 573 MB', text)
        self.assertIn('- 进程实际占用内存 6.7 GB（主机共 16 GB）', text)
        self.assertIn('- CPU：193%（按单核计，主机 12 核，约占整机 16%）', text)
        self.assertIn('单次读数高不代表内存不够', text)
        self.assertNotIn('磁盘', text)
        self.assertNotIn('245054767104', text)
        # No jcmd answer: ps etime still gives the uptime.
        bare = ws.format_host('[怀旧]', {'process': dict(HOST['process'], etime='2-03:04:05'), 'collected_at': 0})
        self.assertIn('已连续运行 2 天 3 小时', bare)
        self.assertNotIn('JVM 堆内存', bare)
        self.assertIn('没有找到服务端进程', ws.format_host('[怀旧]', {'disk': [1, 1, 0]}))

    def test_lag_report_and_alert_add_block_entities_servercore_and_heap(self):
        lag = ws.format_lag('[怀旧]', world_tests.TPS, world_tests.PERF, world_tests.ENTITIES, BLOCKS, SERVERCORE,
                            HOST, NAMES)
        self.assertIn('正在运算的方块实体最多的：幽匿感测体（minecraft:sculk_sensor） 4917、minecraft:sculk_catalyst 1252', lag)
        self.assertIn('ServerCore 当前设置：模拟距离 4、视距 10、区块刻距离 10、刷怪上限 100%', lag)
        self.assertIn('JVM 堆内存已用 5.6 GB / 10.0 GB（55%，瞬时值，含还没回收的垃圾）', lag)
        # The round-12 call shape still works.
        self.assertIn('整体 TPS 17.4', ws.format_lag('[怀旧]', world_tests.TPS, world_tests.PERF, world_tests.ENTITIES))
        extra = ws.lag_alert_lines(world_tests.ENTITIES, world_tests.PERF, '', '', BLOCKS, SERVERCORE, HOST)
        self.assertIn('运算中的方块实体：minecraft:sculk_sensor 4917、minecraft:sculk_catalyst 1252、'
                      'dysoncubeproject:multiblock_structure 884、create:fluid_tank 556', extra)
        self.assertIn('ServerCore 当前设置：模拟距离 4、视距 10、区块刻距离 10、刷怪上限 100%', extra)
        self.assertIn('JVM 堆 5.6 GB / 10.0 GB（55%）', extra)

    def test_chunk_estimate_uses_the_live_servercore_simulation_distance(self):
        with tempfile.TemporaryDirectory() as temp:
            props = Path(temp) / 'server.properties'
            props.write_text('simulation-distance=10\n', encoding='utf-8')
            query = lambda command: SERVERCORE if command == 'servercore status' else world_tests.fake_query(command)
            data = ws.collect_chunks(query, temp, props)
            self.assertEqual(4, data['simulation'])
            self.assertIn('ServerCore 当前设置：模拟距离 4', ws.format_chunks('[怀旧]', data))
            self.assertEqual(10, ws.collect_chunks(world_tests.fake_query, temp, props)['simulation'])


def payload(kind, value):
    """NBT writer for the fixture: byte, int, string, list, compound."""
    if kind == 1:
        return struct.pack('>b', value)
    if kind == 3:
        return struct.pack('>i', value)
    if kind == 8:
        raw = value.encode()
        return struct.pack('>H', len(raw)) + raw
    if kind == 9:
        item, rows = value
        return bytes([item]) + struct.pack('>i', len(rows)) + b''.join(payload(item, r) for r in rows)
    if kind == 10:
        return b''.join(bytes([k]) + payload(8, n) + payload(k, v) for n, (k, v) in value.items()) + b'\x00'
    raise ValueError(kind)


def player_file(body):
    return gzip.compress(b'\x0a' + payload(8, '') + payload(10, body))


class ResearchReaderTests(unittest.TestCase):
    def test_research_aspects_and_warp_leave_the_host_and_nothing_else(self):
        first = 'aaaaaaaa-0000-0000-0000-000000000001'
        second = 'bbbbbbbb-0000-0000-0000-000000000002'
        research = (10, {'BASICTHAUMATURGY': (1, 1), 'ALUMENTUM': (1, 1), '@NITOR': (1, 1), 'FORGOTTEN': (1, 0)})
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'world' / 'playerdata'
            folder.mkdir(parents=True)
            (Path(temp) / 'usercache.json').write_text(json.dumps([{'uuid': first, 'name': 'Alice'}]), encoding='utf-8')
            (folder / f'{first}.dat').write_bytes(player_file({
                'Inventory': (9, (10, [{'id': (8, 'minecraft:diamond'), 'Count': (1, 3)}])),
                'Pos': (9, (3, [100, 64, -32])),
                'NeoForgeData': (10, {'ThaumcraftResearch': research,
                                      'ThaumcraftDiscoveredAspects': (10, {'aer': (1, 1), 'ignis': (1, 1), 'aqua': (1, 0)}),
                                      'ThaumcraftWarp': (3, 5), 'ThaumcraftWarpSticky': (3, 2),
                                      'ThaumcraftWarpTemporary': (3, 1), 'Note': (8, 'secret base')})}))
            # Older saves keep the data only in the attachment.
            (folder / f'{second}.dat').write_bytes(player_file({
                'neoforge:attachments': (10, {'thaumcraft:player_progress_data': (10, {
                    'ThaumcraftResearch': (10, {'BASICTHAUMATURGY': (1, 1)})})})}))
            (folder / 'cccccccc-0000-0000-0000-000000000003.dat').write_bytes(player_file({'Health': (3, 20)}))
            (folder / 'dddddddd-0000-0000-0000-000000000004.dat').write_bytes(b'broken')
            server = {'remote_ssh_host': 'remote-test', 'remote_os': 'posix', 'remote_server_dir': temp,
                      'remote_python': 'python3'}
            result = reader.thaumcraft_progress(server, runner=run_local)
        self.assertEqual({first, second}, set(result['players']))
        row = result['players'][first]
        self.assertEqual(('Alice', ['ALUMENTUM', 'BASICTHAUMATURGY'], 1, 2, [5, 2, 1]),
                         (row['name'], row['research'], row['clues'], row['aspects'], row['warp']))
        self.assertEqual(('bbbbbbbb', ['BASICTHAUMATURGY']),
                         (result['players'][second]['name'], result['players'][second]['research']))
        self.assertEqual(1, result['errors'])
        dumped = json.dumps(result)
        for private in ('diamond', 'secret', 'Inventory', 'Pos'):
            self.assertNotIn(private, dumped)


def write_jar(path, files):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in files.items():
            archive.writestr(name, json.dumps(data, ensure_ascii=False))


class ResearchProgressTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        mods = self.root / 'mods'
        mods.mkdir()
        write_jar(mods / 'thaumcraft-1.jar', {
            'data/thaumcraft/research/basics/BASICTHAUMATURGY.json': {'key': 'BASICTHAUMATURGY'},
            'assets/thaumcraft/lang/zh_cn.json': {'tc.research_name.BASICTHAUMATURGY': '神秘学基础',
                                                  'tc.research_name.FOCUS': '法器（本体译名）'},
            'assets/thaumcraft/lang/en_us.json': {'tc.research_name.BASICTHAUMATURGY': 'Basics'}})
        write_jar(mods / 'addon-1.jar', {
            'assets/addon/lang/zh_cn.json': {'tc.research_name.FOCUS': '法器', 'tc.research_name.BASICTHAUMATURGY': '别名'},
            'assets/addon/lang/en_us.json': {'tc.research_name.ALUMENTUM': 'Alumentum'}})
        (mods / 'broken.jar').write_bytes(b'not a zip')
        (self.root / 'mod-inventory.json').write_text(json.dumps({'mods': [
            {'file': 'thaumcraft-1.jar', 'name': 'Thaumcraft'}, {'file': 'addon-1.jar', 'name': 'Addon'}]}), encoding='utf-8')
        self.index = pp.build_research_index(mods, self.root / 'mod-inventory.json',
                                             'BASICTHAUMATURGY\nALUMENTUM\nFOCUS\nSECRETS\nWARP\nNITOR\n')
        (self.root / 'thaumcraft-research.json').write_text(json.dumps(self.index), encoding='utf-8')
        players = {
            'u1': {'name': 'Alice', 'research': ['BASICTHAUMATURGY', 'ALUMENTUM', 'REMOVED'], 'clues': 2,
                   'aspects': 40, 'warp': [3, 4, 5], 'saved_at': 1792997000},
            'u2': {'name': 'u2', 'research': ['BASICTHAUMATURGY', 'ALUMENTUM', 'FOCUS', 'SECRETS', 'WARP', 'NITOR'],
                   'clues': 0, 'aspects': 66, 'warp': [0, 0, 0]},
            'u3': {'name': 'Alice', 'research': ['BASICTHAUMATURGY'], 'clues': 0, 'aspects': 66, 'warp': [9, 0, 0]}}
        (self.root / 'thaumcraft-progress.json').write_text(json.dumps({'generated_at': 1792997848, 'players': players}),
                                                            encoding='utf-8')

    def test_index_names_and_owning_mods(self):
        self.assertEqual(['BASICTHAUMATURGY', 'ALUMENTUM', 'FOCUS', 'SECRETS', 'WARP', 'NITOR'], self.index['keys'])
        # Base research JSON wins the owner; the owner's own translation wins the name.
        self.assertEqual('Thaumcraft', self.index['mods']['BASICTHAUMATURGY'])
        self.assertEqual('神秘学基础', self.index['names']['BASICTHAUMATURGY'])
        self.assertEqual(('Addon', '法器'), (self.index['mods']['FOCUS'], self.index['names']['FOCUS']))
        self.assertEqual(('Addon', 'Alumentum'), (self.index['mods']['ALUMENTUM'], self.index['names']['ALUMENTUM']))
        self.assertEqual('Thaumcraft', self.index['mods']['SECRETS'])
        self.assertNotIn('SECRETS', self.index['names'])
        # A failed listing keeps the previous keys.
        again = pp.build_research_index(self.root / 'mods', self.root / 'mod-inventory.json',
                                        'Unknown or incomplete command, see below for error', self.index)
        self.assertEqual(self.index['keys'], again['keys'])
        self.assertEqual([], pp.parse_research_list('Unknown or incomplete command, see below for error'))

    def test_player_report_ranking_and_ties(self):
        with mock.patch.object(pp, 'quest_teams', return_value={'players': {'u2': 'Bob'}}):
            text = pp.research_progress(self.root, 'alice', mirror_root=self.root)
            ranking = pp.research_progress(self.root, mirror_root=self.root)
            aspects = pp.research_progress(self.root, top='aspects', limit=1, mirror_root=self.root)
            warp = pp.research_progress(self.root, top='warp', mirror_root=self.root)
        # Two UUIDs named Alice: the one with more research answers; removed research is not counted.
        self.assertIn('- 已完成 2/6 项研究（33%）', text)
        self.assertIn('- 按模组：Thaumcraft 1/4、Addon 1/2', text)
        self.assertIn('- 线索 2 条（还没完成的研究的提示），已发现要素 40 种', text)
        self.assertIn('- 扭曲：永久 3、粘性 4、临时 5（合计 12）', text)
        self.assertIn('- 还差的 4 项：法器、NITOR、SECRETS、WARP', text)
        self.assertIn('1. Bob：6（100%）', ranking)            # name filled from FTB Teams
        self.assertIn('完成全部研究的有 1 人。', ranking)
        self.assertIn('并列第一（66）的共有 2 人', aspects)
        self.assertIn('1. Alice：12（永久 3、粘性 4、临时 5）', warp)
        self.assertIn('没有找到 nobody 的神秘时代研究记录', pp.research_progress(self.root, 'nobody'))
        with self.assertRaises(ValueError):
            pp.research_progress(self.root, top='gold')
        self.assertIn('还没有从服务器同步过来', pp.research_progress(self.root / 'missing'))


QUEST_CHAPTER = """{
    id: "C1"
    filename: "start"
    quests: [
        { id: "Q1" tasks: [{ id: "T1" type: "checkmark" }] }
        { id: "Q2" tasks: [{ id: "T2" type: "checkmark" }] }
        { id: "Q3" tasks: [{ id: "T3" type: "checkmark" }] }
    ]
}"""
QUEST_CHAPTER_2 = """{ id: "C2" filename: "magic" quests: [ { id: "Q4" tasks: [{ id: "T4" type: "checkmark" }] } ] }"""
QUEST_LANG = """{
    chapter.C1.title: "起始"
    chapter.C2.title: "魔法"
    quest.Q1.title: "第一步"
    quest.Q2.title: "第二步"
    quest.Q3.title: "第三步"
    quest.Q4.title: "入门魔法"
}"""
ALICE = '11111111-1111-1111-1111-111111111111'
BOB = '22222222-2222-2222-2222-222222222222'
CAROL = '33333333-3333-3333-3333-333333333333'
CAROL_OLD = '44444444-4444-4444-4444-444444444444'
PARTY = '99999999-9999-9999-9999-999999999999'


class QuestProgressTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.mirror = Path(temp.name)
        book = self.mirror / 'config' / 'ftbquests' / 'quests'
        (book / 'chapters').mkdir(parents=True)
        (book / 'lang').mkdir()
        (book / 'chapters' / 'start.snbt').write_text(QUEST_CHAPTER, encoding='utf-8')
        (book / 'chapters' / 'magic.snbt').write_text(QUEST_CHAPTER_2, encoding='utf-8')
        (book / 'lang' / 'zh_cn.snbt').write_text(QUEST_LANG, encoding='utf-8')
        world = self.mirror / 'world'
        for folder in ('ftbquests', 'ftbteams/party', 'ftbteams/player', 'ftbteams/deleted'):
            (world / folder).mkdir(parents=True)
        for uuid, name in ((ALICE, 'Alice'), (BOB, 'Bob'), (CAROL, 'Carol'), (CAROL_OLD, 'Carol')):
            (world / 'ftbteams' / 'player' / f'{uuid}.snbt').write_text(
                f'{{ id: "{uuid}" type: "player" player_name: "{name}" ranks: {{ }} }}', encoding='utf-8')
        (world / 'ftbteams' / 'party' / f'{PARTY}.snbt').write_text(
            f'{{ id: "{PARTY}" type: "party" owner: "{ALICE}" ranks: {{ "{ALICE}": "owner" "{BOB}": "member" '
            f'"{CAROL}": "invited" }} properties: {{ "ftbteams:display_name": "Alice\'s Party" }} }}', encoding='utf-8')

        def progress(team, completed):
            body = ' '.join(f'"{k}": {v}L' for k, v in completed.items())
            (world / 'ftbquests' / f'{team}.snbt').write_text(
                f'{{ version: 1 uuid: "{team.replace("-", "")}" completed: {{ {body} }} }}', encoding='utf-8')
        progress(PARTY, {'Q1': 1792900000000, 'Q2': 1792990000000, 'C1': 1792990000000})
        progress(ALICE, {'Q1': 1790000000000, 'Q2': 1790000000000, 'Q3': 1790000000000, 'Q4': 1790000000000})
        progress(CAROL, {'Q4': 1792000000000})
        progress(CAROL_OLD, {})
        progress('dead0000-0000-0000-0000-000000000000', {'Q1': 1, 'Q2': 1, 'Q3': 1})

    def test_ranking_counts_teams_once(self):
        text = pp.quest_progress(self.mirror, self.mirror)
        self.assertIn('任务书共 4 个任务，2 支队伍或个人有进度', text)
        self.assertIn("1. 队伍「Alice's Party」（Alice、Bob）：2 个（50%）", text)
        self.assertIn('2. Carol：1 个（25%）', text)
        # Alice's own player team is stale while she is in the party; a deleted team is gone.
        self.assertNotIn('4 个（100%）', text)
        self.assertNotIn('3 个', text)

    def test_member_report_follows_the_party(self):
        text = pp.quest_progress(self.mirror, self.mirror, 'bob')
        self.assertIn("玩家 Bob 的任务进度（队伍「Alice's Party」（Alice、Bob），进度全队共享）", text)
        self.assertIn('- 已完成 2/4 个任务（50%）', text)
        self.assertIn('- 各章节：起始 2/3', text)
        self.assertIn('- 还没开始的章节：魔法', text)
        self.assertIn('- 最近完成：「第二步」（起始，10-26 12:46）、「第一步」（起始，10-25 11:46）', text)
        self.assertIn('- 进行中、离完成最近的章节：起始（还差 1 个）', text)
        # Two UUIDs named Carol: the one with progress answers. An invite is not membership.
        carol = pp.quest_progress(self.mirror, self.mirror, 'Carol')
        self.assertIn('玩家 Carol 的任务进度：', carol)
        self.assertIn('- 已完成 1/4 个任务（25%）', carol)
        self.assertIn('没有找到名叫 Zed 的玩家', pp.quest_progress(self.mirror, self.mirror, 'Zed'))
        self.assertIn('有多个玩家名匹配：Bob、Carol。', pp.quest_progress(self.mirror, self.mirror, 'o'))


class WiringTests(unittest.TestCase):
    def test_new_commands_are_read_class(self):
        for command in ('statistics', 'statistics block-entities byType', 'statistics block-entities byType 2',
                        'statistics entities byPlayer', 'statistics entities 3', 'servercore status',
                        'thaumcraft research list'):
            with self.subTest(command=command):
                self.assertEqual('read', cc.classify(command))
        for command in ('thaumcraft research Alice ALL', 'thaumcraft research list; stop', 'servercore reload',
                        'statistics entities byType; stop', 'servercore settings mobcaps 50'):
            with self.subTest(command=command):
                self.assertEqual('confirm', cc.classify(command))

    def test_tools_are_offered_and_routed(self):
        for tool in (s.QUEST_PROGRESS, s.THAUMCRAFT_PROGRESS):
            self.assertIn(tool, s.TOOLS_MEMBER)
            self.assertIn(tool, s.TOOLS_ADMIN)
        self.assertTrue({'quest_progress', 'thaumcraft_progress'} <= s.READ_QUERY_TOOL_NAMES)
        self.assertEqual(['chunks', 'entities', 'lag', 'machines', 'host'],
                         s.SERVER_WORLD['function']['parameters']['properties']['topic']['enum'])
        server = {'id': 'legacy', 'prefix': '[怀旧]', 'server_dir': 'mirror', 'remote_ssh_host': 'remote-test'}
        answers = {'statistics block-entities byType': BLOCKS, 'statistics block-entities byType 2': BLOCKS_2,
                   'statistics block-entities byPlayer': BLOCKS_BY_PLAYER,
                   'statistics entities byPlayer': ENTITIES_BY_PLAYER, 'servercore status': SERVERCORE,
                   'neoforge tps': world_tests.TPS, 'mio_icif perf': world_tests.PERF,
                   'neoforge entity list': world_tests.ENTITIES}
        sanae_local_ops._HOST_CACHE.clear()
        with mock.patch.object(sanae_local_ops.server_registry, 'resolve_server', return_value=server), \
                mock.patch.object(sanae_local_ops.server_registry, 'query_server',
                                  side_effect=lambda srv, command: answers[command]), \
                mock.patch.object(sanae_local_ops.remote_server_reader, 'host_status', return_value=HOST) as host, \
                mock.patch.object(sanae_local_ops.player_progress, 'quest_progress', return_value='Q') as quests, \
                mock.patch.object(sanae_local_ops.player_progress, 'research_progress', return_value='R') as research:
            run = lambda name, args: sanae_local_ops.execute_insight(name, args, {'id': 'legacy'}, False)
            machines = run('server_world', {'topic': 'machines'})
            status = run('server_world', {'topic': 'host'})
            lag = run('server_world', {'topic': 'lag'})
            self.assertEqual('[怀旧] Q', run('quest_progress', {'player': 'Bob'}))
            self.assertEqual('[怀旧] R', run('thaumcraft_progress', {'top': 'warp'}))
        self.assertIn('按在线玩家', machines)
        self.assertIn('JVM 堆内存：已用 5.6 GB', status)
        self.assertIn('JVM 堆内存已用 5.6 GB', lag)
        self.assertIn('正在运算的方块实体最多的', lag)
        host.assert_called_once_with(server)              # lag reused the cached host numbers
        self.assertEqual('mirror', quests.call_args.args[0])
        self.assertEqual('Bob', quests.call_args.args[2])
        self.assertEqual(('', 'warp'), research.call_args.args[1:3])
        local = dict(server, remote_ssh_host='')
        with mock.patch.object(sanae_local_ops.server_registry, 'resolve_server', return_value=local):
            self.assertIn('不在远端主机上', sanae_local_ops.execute_insight('server_world', {'topic': 'host'},
                                                                          {'id': 'legacy'}, False))
        sanae_local_ops._HOST_CACHE.clear()
        with mock.patch.object(sanae_local_ops.server_registry, 'resolve_server', return_value=server), \
                mock.patch.object(sanae_local_ops.remote_server_reader, 'host_status', side_effect=OSError('ssh')):
            self.assertIn('主机暂时连不上（OSError）', sanae_local_ops.execute_insight(
                'server_world', {'topic': 'host'}, {'id': 'legacy'}, False))

    def test_new_questions_need_tools_and_chat_stays_chat(self):
        for text in ('早苗，现在哪些机器最多', '方块实体有多少', '服务器内存用了多少', '服务端cpu占用高吗',
                     '开服多久了', '服务器上次重启是什么时候', '什么时候重启的服', '我任务做到哪了', '我的任务进度',
                     '谁任务做得最多', '任务完成了多少', '神秘研究谁最多', '我解锁了多少研究', '研究进度怎么样',
                     'Alice 扭曲多少', '谁扭曲最高', '要素发现了多少'):
            with self.subTest(text=text):
                self.assertTrue(s._has_server_query_intent(text))
        for text in ('我电脑内存不够了', '你的作业做到哪了', '这个研究很有意思', '早苗你有什么爱好',
                     '扭曲是什么意思'):
            with self.subTest(text=text):
                self.assertFalse(s._has_server_query_intent(text))

    def test_forced_prompt_names_the_new_tools(self):
        source = Path(s.__file__).read_text(encoding='utf-8')
        self.assertIn('服务端内存、CPU、开服多久、上次重启用 topic=host', source)
        self.assertIn('神秘时代研究、要素、扭曲用 thaumcraft_progress', s.SANAE_OPERATIONAL_POLICY)


class MonitorRound13Tests(unittest.TestCase):
    setUp = monitor_tests.RemoteMonitorTests.setUp

    def test_mirror_extras_writes_research_snapshot_and_daily_index(self):
        monitor = self.monitor
        monitor._mirror_extras = self.real_mirror_extras
        self.assertIn('world/ftbquests', monitor.MIRROR_TREES)
        self.assertIn('world/ftbteams', monitor.MIRROR_TREES)
        progress = {'generated_at': 1, 'players': {'u1': {'name': 'Alice', 'research': ['A']}}, 'errors': 0}
        (Path(self.temp.name) / 'mod-inventory.json').write_text(json.dumps({'signature': 'v1', 'mods': []}),
                                                                 encoding='utf-8')
        listing = mock.Mock(return_value='ALPHA\nBETA\nGAMMA\nDELTA\nEPSILON\n')
        with mock.patch.object(monitor.remote_mods_mirror, 'sync_tree', return_value={'fetched': 0, 'deleted': 0, 'missing': 0}), \
                mock.patch.object(monitor.remote_server_reader, 'player_stats', return_value={'players': []}), \
                mock.patch.object(monitor.remote_server_reader, 'forced_chunks',
                                  return_value={'dims': {}, 'errors': []}), \
                mock.patch.object(monitor.remote_server_reader, 'thaumcraft_progress', return_value=progress), \
                mock.patch.object(monitor.command_catalog, 'refresh_server', return_value={'count': 1}), \
                mock.patch.object(monitor, 'query_server', listing):
            monitor._mirror_extras()
            monitor._mirror_extras()                                   # the index is daily
        root = Path(self.temp.name)
        self.assertEqual(progress, json.loads((root / 'thaumcraft-progress.json').read_text(encoding='utf-8')))
        index = json.loads((root / 'thaumcraft-research.json').read_text(encoding='utf-8'))
        self.assertEqual(['ALPHA', 'BETA', 'GAMMA', 'DELTA', 'EPSILON'], index['keys'])
        listing.assert_called_once_with(self.server, 'thaumcraft research list')
        # A changed mod set rebuilds it at once.
        (root / 'mod-inventory.json').write_text(json.dumps({'signature': 'v2', 'mods': []}), encoding='utf-8')
        with mock.patch.object(monitor, 'query_server', listing), \
                mock.patch.object(monitor.remote_server_reader, 'thaumcraft_progress', side_effect=OSError('ssh')):
            monitor._refresh_research_index([])
        self.assertEqual(2, listing.call_count)

    def test_lag_alert_adds_block_entities_servercore_and_heap(self):
        monitor = self.monitor
        monitor.LAG_ALERT_GROUPS = (2105,)
        answers = {'neoforge tps': world_tests.TPS, 'list': 'There are 1 of a max of 20 players online: Alice',
                   'neoforge entity list': world_tests.ENTITIES, 'mio_icif perf': world_tests.PERF,
                   'neoforge entity list "minecraft:item"': world_tests.ITEMS,
                   'neoforge entity list "minecraft:experience_orb"': world_tests.ORBS,
                   'statistics block-entities byType': BLOCKS, 'servercore status': SERVERCORE}
        with mock.patch.object(monitor, 'query_server', side_effect=lambda server, command: answers[command]), \
                mock.patch.object(monitor.remote_server_reader, 'host_status', return_value=HOST):
            texts = [monitor._lag_watch({'timestamp': 5000 + 60 * i, 'tps': 7.5, 'mspt': 130, 'tick_parser': 2})
                     for i in range(5)]
        self.assertIn('运算中的方块实体：minecraft:sculk_sensor 4917', texts[4])
        self.assertIn('ServerCore 当前设置：模拟距离 4', texts[4])
        self.assertIn('JVM 堆 5.6 GB / 10.0 GB（55%）', texts[4])
        self.assertIn('经验球最集中：区块 (-938, 800) 350 个', texts[4])


if __name__ == '__main__':
    unittest.main()
