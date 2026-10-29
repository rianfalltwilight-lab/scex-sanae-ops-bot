"""World status: chunk loading, entities and lag sources from read-only commands.

Command outputs below are copied from the live server on 10-26; nothing here
reaches RCON, SSH or QQ. The chunks.dat reader runs locally on a written file.
"""
import base64
import gzip
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_remote_collectors import run_local
from tests import test_remote_legacy_monitor as monitor_tests
from tests import test_yachiyo_runtime as runtime_tests
import command_catalog as cc
import remote_server_reader as reader
import sanae_local_ops
import server_watch as sw
import world_status as ws

s = runtime_tests.s
FORCELOAD = ('25 force loaded chunks were found in Overworld at: [804, -518], [805, -518], [806, -518], '
             '[807, -518], [2160, 1634]')
LOADERS = """SI chunk loaders: 3 loaded, 3 working, 35 chunks held
Rules: switch ON, per loader 25, per player - chunks / - loaders, offline - min, 1.0 EU per chunk, random ticks ON
minecraft:overworld -14764 87 25285 - L_Morningstar - 25/25 chunks - active
minecraft:overworld 12832 17 -8225 - (no owner) - 3/3 chunks - active
minecraft:the_nether 134 128 33469 - Alice - 6/7 chunks - active"""
ENTITIES = """Total: 3210
  895: minecraft:item
  820: minecraft:experience_orb
  179: botania:spark
  102: minecraft:zombie"""
ORBS = """Entity: minecraft:experience_orb Total: 805
  350: -938, 800
  228: -938, 802
  165: -938, 801"""
ITEMS = """Entity: minecraft:item Total: 976
  124: 2149, 1625
  54: -922, 1581"""
PERF = """SI performance: server tick 57.47 ms (17.4 TPS)
minecraft:overworld: energy network 9.87 ms median, 16.76 ms p95, 50.48 ms max (256 ticks); route plans built 3160750
minecraft:the_end: energy network 0.03 ms median, 0.05 ms p95, 0.07 ms max (256 ticks); route plans built 0"""
TPS = """Overworld: 17.400 TPS (57.000 ms/tick)
The Nether: 20.000 TPS (3.800 ms/tick)
thaumcraft:outer_lands: 20.000 TPS (0.039 ms/tick)
Overall: 17.400 TPS (61.000 ms/tick)"""
DIMENSIONS = """Currently registered dimensions by type:
minecraft:overworld: Overworld
ae2:spatial_storage: Spatial Storage
minecraft:the_nether: The Nether
twilightforest:twilight_forest_type: Twilight Forest"""


def fake_query(command):
    return {
        'forceload query': FORCELOAD,
        'mio_icif chunkloaders': LOADERS,
        'neoforge dimensions': DIMENSIONS,
        'list': 'There are 2 of a max of 20 players online: Alice, Bob',
        'data get entity Alice Dimension': 'Alice has the following entity data: "minecraft:overworld"',
        'data get entity Alice Pos': 'Alice has the following entity data: [100.5d, 64.0d, -32.2d]',
        'data get entity Bob Dimension': 'Bob has the following entity data: "minecraft:the_nether"',
        'data get entity Bob Pos': 'Bob has the following entity data: [0.0d, 64.0d, 0.0d]',
    }.get(command, 'No force loaded chunks were found in Somewhere' if 'forceload query' in command else '')


class ParserTests(unittest.TestCase):
    def test_command_outputs(self):
        self.assertEqual([[804, -518], [805, -518], [806, -518], [807, -518], [2160, 1634]], ws.parse_forceload(FORCELOAD))
        self.assertEqual([], ws.parse_forceload('No force loaded chunks were found in The Nether'))
        self.assertIsNone(ws.parse_forceload('Unknown or incomplete command'))
        loaders = ws.parse_chunkloaders(LOADERS)
        self.assertEqual((3, 3, 35, 3), (loaders['loaded'], loaders['working'], loaders['held'], len(loaders['loaders'])))
        self.assertEqual({'dim': 'minecraft:the_nether', 'x': 134, 'y': 128, 'z': 33469, 'owner': 'Alice',
                          'held': 6, 'wanted': 7, 'state': 'active'}, loaders['loaders'][2])
        self.assertEqual('', loaders['loaders'][1]['owner'])
        self.assertEqual((3210, [(895, 'minecraft:item'), (820, 'minecraft:experience_orb'), (179, 'botania:spark'),
                                 (102, 'minecraft:zombie')]), ws.parse_entity_list(ENTITIES))
        self.assertEqual((805, [(350, '-938, 800'), (228, '-938, 802'), (165, '-938, 801')]), ws.parse_entity_list(ORBS))
        self.assertEqual((None, []), ws.parse_entity_list('Expected whitespace to end one argument'))
        perf = ws.parse_si_perf(PERF)
        self.assertEqual((57.47, 17.4, 9.87), (perf['tick_ms'], perf['tps'], perf['dims']['minecraft:overworld']['median']))
        self.assertIsNone(ws.parse_si_perf(''))
        self.assertEqual(['minecraft:overworld', 'minecraft:the_nether', 'minecraft:the_end', 'ae2:spatial_storage'],
                         ws.dimension_ids(DIMENSIONS))

    def test_simulation_distance_and_player_chunks(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'server.properties'
            path.write_text('view-distance=10\nsimulation-distance=6\n', encoding='utf-8')
            self.assertEqual(6, ws.simulation_distance(path))
            self.assertEqual(10, ws.simulation_distance(Path(temp) / 'missing'))
        self.assertEqual([('Alice', 'minecraft:overworld', 6, -3), ('Bob', 'minecraft:the_nether', 0, 0)],
                         ws.player_positions(fake_query, fake_query('list')))


class ReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'server.properties').write_text('simulation-distance=2\n', encoding='utf-8')
        (self.root / 'forced-chunks.json').write_text(json.dumps({'saved_at': 1792993889.6, 'dims': {
            'minecraft:overworld': {'vanilla': [[804, -518]], 'tickets': {
                'ae2:default': {'chunks': [[0, 0], [1, 0]], 'owners': [[14, 72, 276401]], 'entities': 0},
                'mio_icif:paid_chunk_loader': {'chunks': [[804, -518], [5, 5]], 'owners': [[1, 2, 3]], 'entities': 0}}}}}),
            encoding='utf-8')

    def test_chunk_report_counts_and_hides_coordinates_from_members(self):
        data = ws.collect_chunks(fake_query, self.root, self.root / 'server.properties')
        member = ws.format_chunks('[怀旧]', data)
        admin = ws.format_chunks('[怀旧]', data, detailed=True)
        # Overworld forced: forceload 5 + AE2 2 + SI 2 (one overlaps) = 8 distinct; Alice's 5x5 square adds 25.
        self.assertIn('- 主世界：强加载 8 个区块，其中 /forceload 5 个，AE2 空间锚 2 个，SI 区块加载器 2 个；'
                      '玩家 1 人，估计在运算的区块约 33 个（玩家周围按模拟距离 2 估算）', member)
        # The nether has no snapshot ticket for SI loaders, so the live "held" total counts.
        self.assertIn('- 下界：强加载 6 个区块；玩家 1 人', member)
        self.assertIn('其他 2 个维度（末地、AE2 空间存储）没有强加载，也没有玩家', member)
        self.assertIn('按放置人：L_Morningstar 1 个/25 区块、无主 1 个/3 区块、Alice 1 个/6 区块', member)
        self.assertIn('存档于 10-26 13:51', member)
        self.assertIn('（坐标只对管理员显示。）', member)
        self.assertNotIn('-14764', member)
        self.assertNotIn('[804, -518]', member)
        self.assertIn('· 主世界 (-14764, 87, 25285) L_Morningstar 25/25 区块 active', admin)
        self.assertIn('/forceload 主世界：[804, -518]', admin)
        self.assertIn('AE2 空间锚（主世界）票据来源方块：(14, 72, 276401)', admin)
        (self.root / 'forced-chunks.json').unlink()
        self.assertIn('暂无快照', ws.format_chunks('[怀旧]', ws.collect_chunks(fake_query, self.root, self.root / 'x')))

    def test_entity_and_lag_reports(self):
        member = ws.format_entities('[怀旧]', ENTITIES, ORBS, 'minecraft:experience_orb')
        admin = ws.format_entities('[怀旧]', ENTITIES, ORBS, 'minecraft:experience_orb', detailed=True)
        self.assertIn('全服实体（实时）共 3210 个，最多的：掉落物（minecraft:item） 895、经验球（minecraft:experience_orb） 820', member)
        self.assertIn('最集中的区块：350、228、165（坐标只对管理员显示）', member)
        self.assertIn('区块 (-938, 800)（方块约 x=-15000 z=12808） 350 个', admin)
        self.assertIn('查不到这个实体类型', ws.format_entities('[怀旧]', ENTITIES, 'Can not find', 'x:y'))
        self.assertIn('暂时取不到', ws.format_entities('[怀旧]', ''))
        lag = ws.format_lag('[怀旧]', TPS, PERF, ENTITIES)
        self.assertIn('整体 TPS 17.4，每刻 61.0 ms', lag)
        self.assertIn('各维度耗时：Overworld 57.0 ms、The Nether 3.8 ms', lag)
        self.assertNotIn('outer_lands', lag)
        self.assertIn('SI 能量网络每刻耗时：主世界 中位 9.9 ms / p95 16.8 / 最高 50.5', lag)
        self.assertIn('实体共 3210 个，其中 掉落物（minecraft:item） 895、经验球（minecraft:experience_orb） 820', lag)
        extra = ws.lag_alert_lines(ENTITIES, PERF, ITEMS, ORBS)
        self.assertEqual(['实体共 3210 个：掉落物 895、经验球 820、botania:spark 179、僵尸 102',
                          '掉落物最集中：区块 (2149, 1625) 124 个、区块 (-922, 1581) 54 个',
                          '经验球最集中：区块 (-938, 800) 350 个、区块 (-938, 802) 228 个、区块 (-938, 801) 165 个',
                          'SI 能量网络（主世界）中位 9.9 ms / 最高 50.5 ms'], extra)


def nbt(name, kind, value):
    """Minimal NBT writer for the fixture: compound, list, string, int, long array."""
    def payload(kind, value):
        if kind == 3:
            return struct.pack('>i', value)
        if kind == 8:
            raw = value.encode()
            return struct.pack('>H', len(raw)) + raw
        if kind == 12:
            return struct.pack('>i', len(value)) + struct.pack('>%dq' % len(value), *value)
        if kind == 9:
            item, rows = value
            return bytes([item]) + struct.pack('>i', len(rows)) + b''.join(payload(item, r) for r in rows)
        if kind == 10:
            return b''.join(bytes([k]) + payload(8, n) + payload(k, v) for n, (k, v) in value.items()) + b'\x00'
        raise ValueError(kind)
    return bytes([kind]) + payload(8, name) + payload(kind, value)


def pack(x, z):
    """ChunkPos.toLong as a signed 64-bit value."""
    value = ((z & 0xffffffff) << 32) | (x & 0xffffffff)
    return value - (1 << 64) if value >= 1 << 63 else value


class ForcedChunkSnapshotTests(unittest.TestCase):
    def test_vanilla_and_mod_tickets_from_every_dimension(self):
        with tempfile.TemporaryDirectory() as temp:
            world = Path(temp) / 'world'
            owner = {'X': (3, 14), 'Y': (3, 72), 'Z': (3, 276401)}
            raw = nbt('', 10, {'data': (10, {'Forced': (12, [pack(804, -518), pack(-3, 7)]),
                                             'ModForced': (9, (10, [{'Controller': (8, 'ae2:default'),
                                                                     'ModForced': (9, (10, [{'TickingBlocks': (9, (10, [owner]))}]))}]))}),
                               'DataVersion': (3, 3955)})
            # The tiny writer has no single-long tag; splice in each ticket's "Chunk" long.
            chunk_tag = b'\x04' + struct.pack('>H', 5) + b'Chunk' + struct.pack('>q', pack(13, -20))
            raw = raw.replace(b'\x09\x00\x0dTickingBlocks', chunk_tag + b'\x09\x00\x0dTickingBlocks', 1)
            for rel in ('data', 'DIM-1/data', 'dimensions/twilightforest/twilight_forest/data'):
                (world / rel).mkdir(parents=True)
                (world / rel / 'chunks.dat').write_bytes(gzip.compress(raw))
            (world / 'DIM1' / 'data').mkdir(parents=True)
            (world / 'DIM1' / 'data' / 'chunks.dat').write_bytes(b'not gzip and not nbt')
            server = {'remote_ssh_host': 'remote-test', 'remote_os': 'posix', 'remote_server_dir': temp,
                      'remote_python': 'python3'}
            result = reader.forced_chunks(server, runner=run_local)
        self.assertEqual({'minecraft:overworld', 'minecraft:the_nether', 'twilightforest:twilight_forest'},
                         set(result['dims']))
        row = result['dims']['minecraft:overworld']
        self.assertEqual([[804, -518], [-3, 7]], row['vanilla'])
        self.assertEqual({'chunks': [[13, -20]], 'owners': [[14, 72, 276401]], 'entities': 0},
                         row['tickets']['ae2:default'])
        # A damaged file is reported by name and does not stop the other dimensions.
        self.assertEqual(1, len(result['errors']))
        self.assertTrue(result['errors'][0].replace(os.sep, '/').startswith('DIM1/data/chunks.dat: '))


class WiringTests(unittest.TestCase):
    def test_listing_commands_are_read_class_and_changes_are_not(self):
        for command in ('forceload query', 'forceload query 10 20', 'execute in minecraft:the_nether run forceload query',
                        'neoforge dimensions', 'neoforge entity list', 'neoforge entity list "minecraft:item"',
                        'mio_icif chunkloaders', 'mio_icif perf', 'warashi loader frozen list', 'chunky progress'):
            with self.subTest(command=command):
                self.assertEqual('read', cc.classify(command))
        for command in ('forceload add 0 0', 'forceload remove all', 'execute in minecraft:the_nether run forceload add 0 0',
                        'mio_icif chunkloaders disable_all', 'mio_icif chunkloaders disable 1 2 3 minecraft:overworld',
                        'warashi loader frozen clear', 'neoforge entity list "minecraft:item"; stop',
                        'execute in minecraft:the_nether run forceload query; stop'):
            with self.subTest(command=command):
                self.assertEqual('confirm', cc.classify(command))

    def test_tool_routes_and_member_detail(self):
        server = {'id': 'legacy', 'prefix': '[怀旧]', 'server_dir': 'unused'}
        with mock.patch.object(sanae_local_ops.server_registry, 'resolve_server', return_value=server), \
                mock.patch.object(sanae_local_ops.server_registry, 'query_server',
                                  side_effect=lambda srv, command: {'neoforge entity list': ENTITIES,
                                                                    'neoforge entity list "minecraft:experience_orb"': ORBS}
                                  .get(command, '')) as query:
            member = sanae_local_ops.execute_insight('server_world', {'topic': 'entities', 'entity': '经验球'},
                                                     {'id': 'legacy'}, False)
            bad = sanae_local_ops.execute_insight('server_world', {'topic': 'entities', 'entity': 'item"; stop'},
                                                  {'id': 'legacy'}, True)
        self.assertIn('最集中的区块：350、228、165（坐标只对管理员显示）', member)
        self.assertIn('要写完整 ID', bad)
        # Round 14 adds the entity limiter's status after the two entity lists.
        self.assertEqual(['neoforge entity list "minecraft:experience_orb"', 'neoforge entity list', 'chesirtab status'],
                         [c.args[1] for c in query.call_args_list])
        with self.assertRaises(ValueError), \
                mock.patch.object(sanae_local_ops.server_registry, 'resolve_server', return_value=server):
            sanae_local_ops.execute_insight('server_world', {'topic': 'weather'}, {'id': 'legacy'}, True)
        self.assertIn(s.SERVER_WORLD, s.TOOLS_MEMBER)
        self.assertIn(s.SERVER_WORLD, s.TOOLS_ADMIN)
        for text in ('服务器有多少强加载区块', '现在有多少个区块在加载', '哪里的掉落物最多', '经验球堆在哪',
                     '为什么这么卡', '实体数量多少', '主世界维度耗时多少'):
            with self.subTest(text=text):
                self.assertTrue(s._has_server_query_intent(text))

    def test_lag_alert_carries_the_extra_lines(self):
        _state, streak = None, None
        state = {}
        for i in range(5):
            state, streak = sw.lag_step(state, {'timestamp': 1000 + 60 * i, 'tps': 8, 'mspt': 120, 'tick_parser': 2})
        text = sw.format_lag_alert('[怀旧]', streak, TPS, '', ['实体共 3210 个：掉落物 895'])
        self.assertIn('实体共 3210 个：掉落物 895\n一小时内不再重复提醒。', text)


class MonitorWorldTests(unittest.TestCase):
    setUp = monitor_tests.RemoteMonitorTests.setUp

    def test_mirror_extras_writes_the_forced_chunk_snapshot(self):
        monitor = self.monitor
        monitor._mirror_extras = self.real_mirror_extras
        snapshot = {'generated_at': 1, 'saved_at': 2, 'dims': {'minecraft:overworld': {'vanilla': [], 'tickets': {}}},
                    'errors': []}
        with mock.patch.object(monitor.remote_mods_mirror, 'sync_tree', return_value={'fetched': 0, 'deleted': 0, 'missing': 0}), \
                mock.patch.object(monitor.remote_server_reader, 'player_stats', return_value={'players': []}), \
                mock.patch.object(monitor.remote_server_reader, 'forced_chunks', return_value=snapshot), \
                mock.patch.object(monitor.command_catalog, 'refresh_server', return_value={'count': 1}):
            monitor._mirror_extras()
        saved = json.loads((Path(self.temp.name) / 'forced-chunks.json').read_text(encoding='utf-8'))
        self.assertEqual(snapshot, saved)

    def test_lag_alert_queries_entities_and_si_perf(self):
        monitor = self.monitor
        monitor.LAG_ALERT_GROUPS = (2105,)
        answers = {'neoforge tps': TPS, 'list': 'There are 1 of a max of 20 players online: Alice',
                   'neoforge entity list': ENTITIES, 'mio_icif perf': PERF,
                   'neoforge entity list "minecraft:item"': ITEMS, 'neoforge entity list "minecraft:experience_orb"': ORBS}
        with mock.patch.object(monitor, 'query_server', side_effect=lambda server, command: answers[command]):
            texts = [monitor._lag_watch({'timestamp': 5000 + 60 * i, 'tps': 7.5, 'mspt': 130, 'tick_parser': 2})
                     for i in range(5)]
        self.assertIn('经验球最集中：区块 (-938, 800) 350 个', texts[4])
        self.assertIn('SI 能量网络（主世界）中位 9.9 ms', texts[4])


if __name__ == '__main__':
    unittest.main()
