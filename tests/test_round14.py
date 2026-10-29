"""Round 14: login records, JVM crash logs, tick percentiles, advancements, futures market,
Thaumcraft world data and the admin-only timing sample.

Command outputs are copied from the live server on 10-26. Remote scripts run locally on
written fixture files; nothing reaches RCON, SSH or QQ.
"""
import gzip
import json
import os
import struct
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tests.test_remote_collectors import run_local
from tests import test_remote_legacy_monitor as monitor_tests
from tests import test_round13 as r13
from tests import test_world_status as world_tests
from tests import test_yachiyo_runtime as runtime_tests
import command_catalog as cc
import player_progress as pp
import remote_server_reader as reader
import sanae_local_ops
import server_insight
import world_records as wr
import world_status as ws

s = runtime_tests.s

TICK = """The game is running, but can't keep up with the target tick rate
Target tick rate: 20.0 per second.
Average time per tick: 52.6ms (Target: 50.0ms)
Percentiles: P50: 43.6ms P95: 81.7ms P99: 99.8ms, sample: 100"""
CHESIRTAB = """TAB=true, 超量区块保护=true
跟踪实体=2644, 跟踪区块=262, 累计检查=57552732, 累计清理实体=0, 保护计数=0
区块实体上限=512, 每tick检查上限=512, 每tick清理上限=64, 协作时间预算=2000 µs"""
TRACK_BLOCKS = """mio_icif:powder_advanced_elc - minecraft:overworld [1379, 75, 267868]: 688.52ms
mio_icif:energy_terminal - minecraft:overworld [12897, 256, -8267]: 554.92ms
minecraft:hopper - minecraft:the_nether [-3, 64, 9]: 812.40μs"""
TRACK_ENTITIES = """minecraft:player - minecraft:overworld [34548, 61, 26135]: 1407.82ms
create:seat - minecraft:the_end [-391, 2, -8]: 420.44ms"""


def server_for(root):
    return {'remote_ssh_host': 'remote-test', 'remote_os': 'posix', 'remote_server_dir': str(root), 'remote_python': 'python3'}


def nbt_payload(kind, value):
    """NBT writer for fixtures: byte, int, long, double, string, list, compound."""
    if kind == 1:
        return struct.pack('>b', value)
    if kind == 3:
        return struct.pack('>i', value)
    if kind == 4:
        return struct.pack('>q', value)
    if kind == 6:
        return struct.pack('>d', value)
    if kind == 8:
        raw = value.encode()
        return struct.pack('>H', len(raw)) + raw
    if kind == 9:
        item, rows = value
        return bytes([item]) + struct.pack('>i', len(rows)) + b''.join(nbt_payload(item, r) for r in rows)
    if kind == 10:
        return b''.join(bytes([k]) + nbt_payload(8, n) + nbt_payload(k, v) for n, (k, v) in value.items()) + b'\x00'
    raise ValueError(kind)


def saved_data(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    root = {'data': (10, data), 'DataVersion': (3, 3955)}
    path.write_bytes(gzip.compress(b'\x0a' + nbt_payload(8, '') + nbt_payload(10, root)))


def pack(x, y, z):
    """BlockPos.asLong as a signed 64-bit value."""
    value = ((x & 0x3FFFFFF) << 38) | ((z & 0x3FFFFFF) << 12) | (y & 0xFFF)
    return value - (1 << 64) if value >= 1 << 63 else value


class LoginRecordTests(unittest.TestCase):
    def test_panel_records_leave_the_host_without_ips_and_show_in_player_stats(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, 'mslx_players.json').write_text(json.dumps({'players': {
                'Charlie': {'name': 'Charlie', 'uuid': 'A1B2C3D4-101c', 'lastLoginTime': '2026-10-17 20:24:18',
                               'lastIp': '198.51.100.10', 'loginCount': 97},
                'Alice': {'name': 'Alice', 'uuid': 'b2c3d4e5', 'lastLoginTime': '2026-10-26 02:31:33',
                           'lastIp': '203.0.113.20', 'loginCount': 315}}}), encoding='utf-8')
            data = reader.login_records(server_for(temp), runner=run_local)
        self.assertNotIn('198.51.100', json.dumps(data))
        self.assertNotIn('lastIp', json.dumps(data))
        self.assertEqual({'name': 'Charlie', 'uuid': 'a1b2c3d4-101c', 'last_login': '2026-10-17 20:24:18',
                          'login_count': 97}, data['players'][0])
        with tempfile.TemporaryDirectory() as ops:
            Path(ops, 'player-stats.json').write_text(json.dumps({'generated_at': 1792997848, 'players': [
                {'name': 'Charlie', 'play_hours': 41.5, 'last_seen': 1792241580}]}), encoding='utf-8')
            Path(ops, 'login-records.json').write_text(json.dumps(data), encoding='utf-8')
            text = server_insight.player_stats(ops, player='Charlie')
            ranking = server_insight.player_stats(ops, top='logins')
        self.assertIn('- 上次登录：2026-10-17 20:24（服务器面板记录），累计登录 97 次', text)
        self.assertIn('- 存档最后更新：', text)
        self.assertIn('1. Alice：315 次（上次 2026-10-26 02:31）\n2. Charlie：97 次', ranking)


class JvmCrashLogTests(unittest.TestCase):
    def test_hs_err_logs_are_listed_and_readable(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp, 'crash-reports')
            folder.mkdir()
            (folder / 'crash-a.txt').write_text('Description: old\n', encoding='utf-8')
            os.utime(folder / 'crash-a.txt', (time.time() - 600, time.time() - 600))
            Path(temp, 'hs_err_pid735.log').write_text('#\n# A fatal error has been detected\n#  SIGBUS (0xa)\n',
                                                       encoding='utf-8')
            newest = reader.crash_report(server_for(temp), runner=run_local)
            named = reader.crash_report(server_for(temp), 'crash-a.txt', runner=run_local)
            crash_only = reader.crash_report(server_for(temp), runner=run_local, kind='crash')
            jvm_only = reader.crash_report(server_for(temp), runner=run_local, kind='jvm')
        self.assertEqual('hs_err_pid735.log', newest['name'])
        self.assertEqual(['#', '# A fatal error has been detected', '#  SIGBUS (0xa)'], newest['lines'])
        self.assertEqual(['crash-a.txt', 'hs_err_pid735.log'], newest['names'])
        self.assertEqual('crash-a.txt', named['name'])
        self.assertEqual(('crash-a.txt', 'hs_err_pid735.log'), (crash_only['name'], jvm_only['name']))
        for bad in ('hs_err_pid735.txt.log', '../hs_err_pid1.log', 'hs_err_pidX.log'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                reader.crash_report(server_for('/tmp'), bad)
        self.assertIn('hs_err_pid', s.READ_CRASH_REPORT['function']['description'])
        self.assertIn('name', s.READ_CRASH_REPORT['function']['parameters']['properties'])


class TickAndLimiterTests(unittest.TestCase):
    def test_parsers_and_reports(self):
        self.assertEqual({'average': 52.6, 'target': 50.0, 'behind': True, 'p50': 43.6, 'p95': 81.7, 'p99': 99.8},
                         ws.parse_tick_query(TICK))
        self.assertIsNone(ws.parse_tick_query('Unknown command'))
        self.assertEqual({'tracked': 2644, 'chunks': 262, 'removed': 0, 'limit': 512}, ws.parse_chesirtab(CHESIRTAB))
        self.assertIsNone(ws.parse_chesirtab(''))
        lag = ws.format_lag('[怀旧]', world_tests.TPS, world_tests.PERF, world_tests.ENTITIES, tick_text=TICK)
        self.assertIn('最近 100 刻：平均每刻 52.6 ms（目标 50 ms），P50 43.6 / P95 81.7 / P99 99.8 ms；服务器报告跟不上目标刻率', lag)
        alert = ws.lag_alert_lines(world_tests.ENTITIES, world_tests.PERF, tick_text=TICK)
        self.assertTrue(alert[0].startswith('最近 100 刻：平均每刻 52.6 ms'))
        entities = ws.format_entities('[怀旧]', world_tests.ENTITIES, limiter_text=CHESIRTAB)
        self.assertIn('超量区块保护（chesirtab）：正在跟踪 2644 个实体、262 个区块，每区块上限 512，累计清理 0 个', entities)


class ProfileTests(unittest.TestCase):
    def test_track_units_are_corrected(self):
        rows = ws.parse_track(TRACK_BLOCKS)
        # NeoForge prints µs with an "ms" label and ns with a "µs" label.
        self.assertEqual(('mio_icif:powder_advanced_elc', 'minecraft:overworld', 1379, 75, 267868), rows[0][:5])
        self.assertAlmostEqual(0.68852, rows[0][5])
        self.assertAlmostEqual(0.0008124, rows[2][5])
        self.assertEqual([], ws.parse_track('No data has been recorded yet.'))
        text = ws.format_profile('[怀旧]', 'blockentity', TRACK_BLOCKS, 20, TICK, {'minecraft:hopper': {'zh': '漏斗'}})
        self.assertIn('1. mio_icif:powder_advanced_elc 主世界 (1379, 75, 267868)：每次约 0.69 ms', text)
        self.assertIn('3. 漏斗（minecraft:hopper） 下界 (-3, 64, 9)：每次约 0.00 ms', text)
        self.assertIn('这 3 个合计每刻约 1.2 ms，占现在平均每刻 52.6 ms 的 2%。', text)
        self.assertIn('没有拿到数据', ws.format_profile('[怀旧]', 'entity', 'No data has been recorded yet.', 20))

    def setUp(self):
        self.server = {'id': 'legacy', 'prefix': '[怀旧]', 'server_dir': 'mirror', 'remote_ssh_host': 'remote-test'}
        sanae_local_ops._PROFILE_LAST.clear()

    def run_profile(self, args, privileged=True, answers=None):
        answers = answers or {'neoforge track start blockentity 20': 'Block Entity tracking enabled for 20 seconds.',
                              'neoforge track blockentity': TRACK_BLOCKS, 'tick query': TICK,
                              'neoforge track start entity 5': 'Entity tracking enabled for 5 seconds.',
                              'neoforge track entity': TRACK_ENTITIES}
        with mock.patch.object(sanae_local_ops.server_registry, 'resolve_server', return_value=self.server), \
                mock.patch.object(sanae_local_ops.server_registry, 'query_server',
                                  side_effect=lambda srv, command: answers[command]) as query, \
                mock.patch.object(sanae_local_ops, '_sleep') as sleep:
            text = sanae_local_ops.execute_insight('machine_profile', args, {'id': 'legacy'}, privileged)
        return text, [c.args[1] for c in query.call_args_list], sleep

    def test_admin_sample_flow_cooldown_and_member_refusal(self):
        text, commands, sleep = self.run_profile({})
        self.assertEqual(['neoforge track start blockentity 20', 'neoforge track blockentity', 'tick query'], commands)
        sleep.assert_called_once_with(22)
        self.assertIn('方块实体耗时采样（20 秒', text)
        again, commands, _ = self.run_profile({})
        self.assertEqual([], commands)                          # within 5 minutes: last result, no new sample
        self.assertIn('分钟前的采样结果', again)
        entity, commands, _ = self.run_profile({'kind': 'entity', 'seconds': 1})
        self.assertEqual('neoforge track start entity 5', commands[0])   # clamped to 5..30 seconds
        self.assertIn('1. 玩家（minecraft:player） 主世界 (34548, 61, 26135)：每次约 1.41 ms', entity)
        refused, commands, _ = self.run_profile({}, privileged=False)
        self.assertEqual(('拒绝：机器耗时采样只有管理员能开。', []), (refused, commands))
        sanae_local_ops._PROFILE_LAST.clear()                  # past the cooldown
        failed, _, _ = self.run_profile({'kind': 'entity'}, answers={'neoforge track start entity 20': 'Unknown command'})
        self.assertIn('采样没有启动：Unknown command', failed)
        with self.assertRaises(ValueError):
            self.run_profile({'kind': 'chunks'})
        self.assertTrue(sanae_local_ops._PROFILE_LOCK.acquire(blocking=False))
        try:
            busy, commands, _ = self.run_profile({'kind': 'entity'})
        finally:
            sanae_local_ops._PROFILE_LOCK.release()
        self.assertEqual([], commands)
        self.assertIn('已经有一次采样在进行', busy)


def write_jar(path, files):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in files.items():
            archive.writestr(name, data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))


ALICE = '11111111-1111-1111-1111-111111111111'
BOB = '22222222-2222-2222-2222-222222222222'


class AdvancementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        mods = self.root / 'mods'
        mods.mkdir()
        write_jar(mods / 'botania-1.jar', {
            'data/botania/advancement/main/flower_pickup.json': {'display': {'title': {'translate': 'adv.botania.flower'},
                                                                             'frame': 'task'}},
            'data/botania/advancement/challenge/gaia_guardian.json': {'display': {
                'title': {'translate': 'adv.botania.gaia'}, 'frame': 'challenge', 'hidden': True}},
            'data/botania/advancement/hidden_trigger.json': {'criteria': {}},
            'data/botania/advancement/recipes/misc/petal.json': {'display': {'title': 'x'}},
            'assets/botania/lang/zh_cn.json': {'adv.botania.flower': '花§a之  初'},
            'assets/botania/lang/en_us.json': {'adv.botania.flower': 'Flower', 'adv.botania.gaia': 'Gaia'}})
        (self.root / 'inventory.json').write_text(json.dumps({'mods': [{'file': 'botania-1.jar', 'name': 'Botania'}]}),
                                                  encoding='utf-8')
        overrides = self.root / 'overrides' / 'assets' / 'botania' / 'lang'
        overrides.mkdir(parents=True)
        (overrides / 'zh_cn.json').write_text(json.dumps({'adv.botania.gaia': '盖亚守护者'}), encoding='utf-8')
        (self.root / 'zh_cn.json').write_text(json.dumps({'advancements.story.mine_stone.title': '石器时代'}), encoding='utf-8')
        vanilla = {'minecraft:story/mine_stone': {'key': 'advancements.story.mine_stone.title', 'en': 'Stone Age',
                                                  'frame': 'task', 'hidden': False},
                   'minecraft:story/root': {'key': 'advancements.story.root.title', 'en': 'Minecraft', 'frame': 'task'}}
        self.index = pp.build_advancement_index(mods, self.root / 'inventory.json', vanilla, self.root / 'overrides',
                                                self.root / 'zh_cn.json')
        ops = self.root / 'ops'
        ops.mkdir()
        (ops / 'advancement-index.json').write_text(json.dumps(self.index), encoding='utf-8')
        (ops / 'login-records.json').write_text(json.dumps({'players': [{'name': 'Bob', 'uuid': BOB}]}), encoding='utf-8')
        folder = self.root / 'world' / 'advancements'
        folder.mkdir(parents=True)
        done = lambda when: {'criteria': {'a': when}, 'done': True}
        (folder / f'{ALICE}.json').write_text(json.dumps({
            'minecraft:story/mine_stone': done('2026-10-20 10:00:00 +0800'),
            'botania:main/flower_pickup': done('2026-10-21 10:00:00 +0800'),
            'botania:challenge/gaia_guardian': done('2026-10-22 10:00:00 +0800'),
            'minecraft:recipes/misc/torch': done('2026-10-22 11:00:00 +0800'),
            'minecraft:story/root': {'criteria': {}, 'done': False}, 'DataVersion': 3955}), encoding='utf-8')
        (folder / f'{BOB}.json').write_text(json.dumps({
            'minecraft:story/mine_stone': done('2026-10-19 09:00:00 +0800')}), encoding='utf-8')
        pp._ADV_FILES.clear()

    def report(self, **args):
        with mock.patch.object(pp, 'quest_teams', return_value={'players': {ALICE: 'Alice'}}):
            return pp.advancement_progress(self.root, self.root / 'ops', **args)

    def test_index_keeps_displayed_advancements_with_names(self):
        rows = self.index['advancements']
        self.assertEqual({'minecraft:story/mine_stone', 'minecraft:story/root', 'botania:main/flower_pickup',
                          'botania:challenge/gaia_guardian'}, set(rows))
        self.assertEqual({'name': '花之 初', 'mod': 'Botania', 'frame': 'task', 'hidden': False},
                         rows['botania:main/flower_pickup'])
        self.assertEqual(('盖亚守护者', 'challenge', True), (rows['botania:challenge/gaia_guardian']['name'],
                                                         rows['botania:challenge/gaia_guardian']['frame'],
                                                         rows['botania:challenge/gaia_guardian']['hidden']))
        self.assertEqual(('石器时代', 'Minecraft'), (rows['minecraft:story/mine_stone']['name'],
                                                 rows['minecraft:story/mine_stone']['mod']))
        self.assertEqual('Minecraft', rows['minecraft:story/root']['name'])     # no zh: English title

    def test_ranking_player_and_query(self):
        ranking = self.report()
        self.assertIn('进度完成排行（共 4 个进度，2 名玩家有记录）：\n1. Alice：3 个（75%）\n2. Bob：1 个（25%）', ranking)
        alice = self.report(player='alice')
        self.assertIn('- 已完成 3/4 个（75%）', alice)
        self.assertIn('- 按模组（完成最多的）：Botania 2/2（100%）、Minecraft 1/2（50%）', alice)
        self.assertIn('- 其中挑战类 1/1 个', alice)
        self.assertIn('- 最近完成：「盖亚守护者」（Botania，10-22 10:00）、「花之 初」（Botania，10-21 10:00）', alice)
        self.assertIn('- 少有人完成的：「盖亚守护者」（全服 1 人）', alice)
        stone = self.report(query='石器时代')
        self.assertIn('进度「石器时代」（Minecraft）：全服 2 人完成', stone)
        self.assertIn('- 最早完成：Bob（10-19 09:00）、Alice（10-20 10:00）', stone)
        self.assertIn('（Botania，挑战，隐藏）：全服 1 人完成', self.report(query='盖亚'))
        self.assertIn('有 2 个进度匹配「botania:」', self.report(query='botania:'))
        self.assertIn('没有找到名字里带「末影龙」的进度', self.report(query='末影龙'))
        self.assertIn('没有找到 Zed 的进度记录', self.report(player='Zed'))
        self.assertEqual(2, pp.warm_advancements(self.root, self.root / 'ops'))
        self.assertIn('进度目录还没有建立', pp.advancement_progress(self.root, self.root / 'missing'))

    def test_vanilla_definitions_come_from_the_server_jar(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp, 'libraries', 'net', 'minecraft', 'server', '1.21.1-x')
            folder.mkdir(parents=True)
            write_jar(folder / 'server-1.21.1-x-slim.jar', {'net/minecraft/A.class': 'x'})
            write_jar(folder / 'server-1.21.1-x-extra.jar', {
                'data/minecraft/advancement/story/mine_stone.json': {'display': {
                    'title': {'translate': 'advancements.story.mine_stone.title'}}},
                'data/minecraft/advancement/nether/all_effects.json': {'display': {
                    'title': {'translate': 'advancements.nether.all_effects.title'}, 'frame': 'challenge', 'hidden': True}},
                'data/minecraft/advancement/recipes/misc/torch.json': {'criteria': {}},
                'data/minecraft/advancement/adventure/no_display.json': {'criteria': {}},
                'assets/minecraft/lang/en_us.json': {'advancements.story.mine_stone.title': 'Stone Age'}})
            rows = reader.vanilla_advancements(server_for(temp), runner=run_local)
        self.assertEqual({'minecraft:story/mine_stone': {'key': 'advancements.story.mine_stone.title', 'en': 'Stone Age',
                                                         'frame': 'task', 'hidden': False},
                          'minecraft:nether/all_effects': {'key': 'advancements.nether.all_effects.title', 'en': '',
                                                           'frame': 'challenge', 'hidden': True}}, rows)


class MarketTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        data = self.root / 'world' / 'data'
        saved_data(data / 'mio_icif_future_market.dat', {
            'CurrentDay': (3, 248),
            'CurrentPrices': (10, {'minecraft:cooked_cod': (3, 235), 'minecraft:honey_bottle': (3, 6),
                                   'minecraft:redstone': (3, 55)}),
            'PriceHistory': (10, {'minecraft:cooked_cod': (9, (3, [90, 98, 123, 142, 167, 197, 235])),
                                  'minecraft:honey_bottle': (9, (3, [140, 150, 120, 60, 30, 9, 6])),
                                  'minecraft:redstone': (9, (3, [52, 43, 47, 47, 49, 52, 55]))}),
            'ActiveEvents': (10, {'minecraft:cooked_cod': (9, (10, [{
                'Type': (8, 'LONG_BUBBLE'), 'StartDay': (3, 244), 'Duration': (3, 7), 'Intensity': (6, 1.1849),
                'CommodityId': (8, 'minecraft:cooked_cod')}]))}),
            'PlayerVolume': (10, {'aaaa': (3, 99)})})
        saved_data(data / 'mio_icif_exchange.dat', {
            'Mail': (10, {'7723675d': (10, {'Coins': (3, 29955)})}),
            'Commissions': (9, (10, [{'Item': (8, 'minecraft:carrot'), 'Quantity': (3, 128), 'Delivered': (3, 0),
                                      'RewardTotal': (3, 1821), 'Kind': (8, 'COIN'), 'Day': (3, 247), 'Expiry': (3, 250),
                                      'System': (1, 1), 'Requester': (8, '')}])),
            'Bars': (10, {'minecraft:cooked_cod': (9, (10, [{'Day': (3, 247), 'O': (3, 197), 'H': (3, 197), 'L': (3, 197),
                                                             'C': (3, 197), 'V': (3, 12)}]))}),
            'Ledger': (9, (10, [{'Day': (3, 247), 'Minted': (3, 5040), 'Burned': (3, 32)}])),
            'Positions': (9, (10, [{'Owner': (8, 'secret-owner')}])), 'Treasury': (10, {'minecraft:gold_ingot': (3, 80)})})
        self.data = reader.market_snapshot(server_for(self.root), runner=run_local)
        self.names = {'minecraft:cooked_cod': {'zh': '熟鳕鱼'}, 'minecraft:honey_bottle': {'zh': '蜂蜜瓶'},
                      'minecraft:redstone': {'zh': '红石粉'}, 'minecraft:carrot': {'zh': '胡萝卜'},
                      'minecraft:cooked_salmon': {'zh': '熟鲑鱼'}}

    def test_snapshot_keeps_personal_data_on_the_host(self):
        dumped = json.dumps(self.data)
        for private in ('29955', '7723675d', 'secret-owner', 'aaaa'):
            self.assertNotIn(private, dumped)
        self.assertEqual([], self.data['errors'])
        self.assertEqual((248, 235), (self.data['day'], self.data['prices']['minecraft:cooked_cod']))
        self.assertEqual([{'type': 'LONG_BUBBLE', 'start': 244, 'duration': 7, 'intensity': 1.185}],
                         self.data['events']['minecraft:cooked_cod'])

    def test_overview_and_item_reports(self):
        overview = wr.format_market('[怀旧]', self.data, self.names)
        self.assertIn('5 天涨得最多：熟鳕鱼 235（+140%）、红石粉 55（+28%）\n5 天跌得最多：蜂蜜瓶 6（-96%）\n', overview)
        self.assertIn('进行中的行情事件 1 个：长期泡沫 1', overview)
        self.assertIn('收购委托：胡萝卜 ×128，报酬 1821 币（第 250 天截止，系统发布）', overview)
        self.assertIn('近 1 天交易所新发行 5040 币、回收 32 币', overview)
        self.assertIn('现在第 248 天', overview)
        cod = wr.format_market('[怀旧]', self.data, self.names, '鳕鱼')
        self.assertIn('熟鳕鱼（minecraft:cooked_cod）现价 235', cod)
        self.assertIn('- 最近 7 天：90 → 98 → 123 → 142 → 167 → 197 → 235（近 7 天最低 90、最高 235）', cod)
        self.assertIn('- 涨跌：1 天 +19%，5 天 +140%', cod)
        self.assertIn('- 进行中的行情：长期泡沫（第 244 天开始，持续 7 天，强度 1.185）', cod)
        self.assertIn('- 交易所近 1 天成交量 12', cod)
        self.assertIn('现价 55', wr.format_market('[怀旧]', self.data, self.names, 'minecraft:redstone'))
        self.assertIn('期货市场里没有「钻石」', wr.format_market('[怀旧]', self.data, self.names, '钻石'))
        self.assertIn('有多种商品匹配「minecraft」', wr.format_market('[怀旧]', self.data, self.names, 'minecraft'))
        self.assertIn('暂时取不到', wr.format_market('[怀旧]', {}, self.names))

    def test_vanilla_names_fill_the_empty_recipe_index_entries(self):
        with tempfile.TemporaryDirectory() as ops:
            Path(ops, 'recipe-index.json').write_text(json.dumps({'names': {
                'minecraft:redstone': {'zh': '', 'en': ''}, 'mod:thing': {'zh': '东西', 'en': 'Thing'}}}), encoding='utf-8')
            # Public source excludes the full Minecraft language files.
            lang = Path(ops, 'lang')
            lang.mkdir()
            (lang / 'zh_cn.json').write_text(json.dumps({'item.minecraft.redstone': '红石粉'}), encoding='utf-8')
            (lang / 'en_us.json').write_text(json.dumps({'item.minecraft.redstone': 'Redstone Dust'}), encoding='utf-8')
            with mock.patch.object(wr, 'VANILLA_LANG', lang):
                names = wr.item_names(ops)
        self.assertEqual('红石粉', names['minecraft:redstone']['zh'])
        self.assertEqual('Redstone Dust', names['minecraft:redstone']['en'])
        self.assertEqual('东西', names['mod:thing']['zh'])


class ThaumcraftWorldTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        world = Path(temp.name) / 'world'
        node = lambda kind, pos, aspects, energized=0, modifier=-1: {
            'TypeName': (8, kind), 'Pos': (4, pack(*pos)), 'Energized': (1, energized), 'Modifier': (3, modifier),
            'Aspects': (9, (10, [{'key': (8, k), 'amount': (3, n)} for k, n in aspects]))}
        saved_data(world / 'data' / 'thaumcraft_aura_nodes.dat', {'Nodes': (9, (10, [
            node('NORMAL', (387, 64, -1200), [('aqua', 1700), ('terra', 900)]),
            node('TAINTED', (-15259, 109, 12219), [('ignis', 3400), ('perditio', 100)], modifier=1),
            node('HUNGRY', (3806, 13, 295116), [('fames', 2100)]),
            node('PURE', (1, -60, 2), [('aqua', 500)], energized=1)]))})
        saved_data(world / 'DIM-1' / 'data' / 'thaumcraft_aura_nodes.dat', {'Nodes': (9, (10, [
            node('NORMAL', (0, 64, 0), [('ignis', 900)])]))})
        outer = world / 'dimensions' / 'thaumcraft' / 'outer_lands' / 'data'
        maze = lambda state, pos: {'GenerationState': (8, state), 'Width': (3, 11), 'Height': (3, 17),
                                   'SourceDimension': (8, 'minecraft:overworld'), 'SourcePortal': (4, pack(*pos)),
                                   'GeneratedCells': (9, (3, [1, 2]) if state == 'complete' else (3, []))}
        saved_data(outer / 'thaumcraft_outer_lands_mazes.dat', {'Mazes': (9, (10, [
            maze('reserved', (-3689, 128, 2877)), maze('complete', (535962, 66, 286188))]))})
        saved_data(outer / 'thaumcraft_outer_lands_bosses.dat', {'bossCount': (3, 3), 'Rooms': (9, (10, [
            {'BossType': (8, 'taint'), 'State': (8, 'defeated'), 'Complete': (1, 1),
             'SpawnedRoles': (8, 'taint_1\ntaint_2'), 'DefeatedRoles': (8, 'taint_1\ntaint_2')}]))})
        saved_data(world / 'data' / 'thaumcraft_shock_fields.dat', {'Positions': (9, (4, [1, 2, 3]))})
        self.data = reader.thaumcraft_world(server_for(temp.name), runner=run_local)
        self.names = {'aqua': '水', 'ignis': '火', 'fames': '饥饿'}

    def test_snapshot_decodes_positions_only_for_rare_nodes(self):
        self.assertEqual([], self.data['errors'])
        over = self.data['dims']['minecraft:overworld']
        self.assertEqual((4, {'NORMAL': 1, 'TAINTED': 1, 'HUNGRY': 1, 'PURE': 1}, 1),
                         (over['count'], over['types'], over['energized']))
        self.assertEqual([[-15259, 109, 12219], [3806, 13, 295116], [1, -60, 2]], [r['pos'] for r in over['rare']])
        self.assertEqual(1, self.data['dims']['minecraft:the_nether']['count'])
        self.assertEqual([-3689, 128, 2877], self.data['mazes']['portals'][0]['pos'])
        self.assertEqual({'minecraft:overworld': 3}, self.data['shock_fields'])

    def test_member_and_admin_reports(self):
        member = wr.format_thaumcraft_world('[怀旧]', self.data, names=self.names)
        self.assertIn('- 主世界 4 个：', member)
        self.assertIn('污染 1', member)
        self.assertIn('充能 1 个；亮度：苍白 1', member)
        self.assertIn('要素储量最多：火（ignis） 3400（1 个节点）、水（aqua） 2200（2 个节点）、饥饿（fames） 2100', member)
        self.assertIn('触电场：主世界 3 处', member)
        tainted = wr.format_thaumcraft_world('[怀旧]', self.data, node_type='污染', names=self.names)
        self.assertIn('污染节点：主世界 1 个、下界 0 个', tainted)
        self.assertNotIn('-15259', tainted)
        admin = wr.format_thaumcraft_world('[怀旧]', self.data, node_type='TAINTED', detailed=True, names=self.names)
        self.assertIn('(-15259, 109, 12219) 火（ignis） 3400', admin)
        self.assertIn('只有污染、饕餮和充能节点保存了坐标', wr.format_thaumcraft_world(
            '[怀旧]', self.data, node_type='纯净', detailed=True))
        self.assertIn('含水（aqua）的节点：主世界 2 个（合计 2200 点）', wr.format_thaumcraft_world(
            '[怀旧]', self.data, aspect='水', names=self.names))
        self.assertIn('节点类型只有', wr.format_thaumcraft_world('[怀旧]', self.data, node_type='金色'))
        outer = wr.format_thaumcraft_world('[怀旧]', self.data, 'outer_lands')
        self.assertIn('- 迷宫 2 个：', outer)
        self.assertIn('已完整生成 1', outer)
        self.assertIn('已生成的房间格 2 个', outer)
        self.assertIn('- 首领房间 3 个，有记录的 1 个：腐化触手群 已击败（击败 2/2）', outer)
        self.assertIn('入口坐标只对管理员显示', outer)
        self.assertIn('主世界 (-3689, 128, 2877) 已登记入口、内部未生成',
                      wr.format_thaumcraft_world('[怀旧]', self.data, 'outer_lands', detailed=True))


class WiringTests(unittest.TestCase):
    def test_tools_and_admin_only_profile(self):
        for tool in (s.ADVANCEMENT_PROGRESS, s.SERVER_MARKET, s.THAUMCRAFT_WORLD):
            self.assertIn(tool, s.TOOLS_MEMBER)
            self.assertIn(tool, s.TOOLS_ADMIN)
        for tool in (s.MACHINE_PROFILE, s.PLAYER_TRAIL):
            self.assertIn(tool, s.TOOLS_ADMIN)
            self.assertNotIn(tool, s.TOOLS_MEMBER)
            self.assertNotIn(tool, s.SERVER_INFO_TOOLS)
        self.assertIn('logins', s.PLAYER_STATS['function']['parameters']['properties']['top']['enum'])
        source = Path(s.__file__).read_text(encoding='utf-8')
        self.assertIn('+ ([MACHINE_PROFILE, PLAYER_TRAIL] if privileged else []))', source)
        self.assertIn('“上次登录”（服务器面板记录）', source)

    def test_read_commands(self):
        for command in ('tick query', 'chesirtab status', 'neoforge track blockentity', 'neoforge track entity'):
            with self.subTest(command=command):
                self.assertEqual('read', cc.classify(command))
        for command in ('neoforge track start blockentity 20', 'neoforge track reset entity', 'tick freeze',
                        'tick rate 40', 'chesirtab status; stop'):
            with self.subTest(command=command):
                self.assertEqual('confirm', cc.classify(command))

    def test_routes(self):
        server = {'id': 'legacy', 'prefix': '[怀旧]', 'server_dir': 'mirror', 'remote_ssh_host': 'remote-test'}
        with mock.patch.object(sanae_local_ops.server_registry, 'resolve_server', return_value=server), \
                mock.patch.object(sanae_local_ops.world_records, 'market', return_value={'prices': {}}), \
                mock.patch.object(sanae_local_ops.world_records, 'thaumcraft', return_value={}), \
                mock.patch.object(sanae_local_ops.player_progress, 'advancement_progress', return_value='A') as adv:
            run = lambda name, args, admin=False: sanae_local_ops.execute_insight(name, args, {'id': 'legacy'}, admin)
            self.assertIn('市场数据暂时取不到', run('server_market', {'item': '红石'}))
            self.assertIn('灵气节点数据暂时取不到', run('thaumcraft_world', {'topic': 'nodes'}))
            self.assertIn('外域数据暂时取不到', run('thaumcraft_world', {'topic': 'outer_lands'}))
            self.assertEqual('[怀旧] A', run('advancement_progress', {'player': 'Bob', 'query': ''}))
            with self.assertRaises(ValueError):
                run('thaumcraft_world', {'topic': 'taint'})
        self.assertEqual(('mirror', 'Bob', ''), (adv.call_args.args[0], adv.call_args.args[2], adv.call_args.args[3]))
        with mock.patch.object(sanae_local_ops.server_registry, 'resolve_server', return_value=server), \
                mock.patch.object(sanae_local_ops.world_records, 'market', side_effect=OSError('ssh')):
            self.assertIn('主机暂时连不上（OSError）', sanae_local_ops.execute_insight(
                'server_market', {}, {'id': 'legacy'}, False))

    def test_new_questions_need_tools(self):
        for text in ('早苗，我上次什么时候上的服？', '他最近进的服是几点', '谁成就最多', '我完成了多少个进度', '红石现在多少钱', '期货市场行情怎么样', '熟鳕鱼价格涨了吗',
                     '现在有什么收购委托', '服务器有多少灵气节点', '污染节点在哪', '外域迷宫生成了多少',
                     '外域的首领打了吗', '哪台机器最卡', '帮我采样一下耗时'):
            with self.subTest(text=text):
                self.assertTrue(s._has_server_query_intent(text))
        for text in ('这个进度条好慢', '我电脑内存不够了', '早苗你有什么爱好', '成就感是什么意思'):
            with self.subTest(text=text):
                self.assertFalse(s._has_server_query_intent(text))


class MonitorRound14Tests(unittest.TestCase):
    setUp = monitor_tests.RemoteMonitorTests.setUp

    def test_mirror_extras_writes_login_records_and_daily_advancement_index(self):
        monitor = self.monitor
        monitor._mirror_extras = self.real_mirror_extras
        self.assertIn('world/advancements', monitor.MIRROR_TREES)
        root = Path(self.temp.name)
        (root / 'mod-inventory.json').write_text(json.dumps({'signature': 'v1', 'mods': []}), encoding='utf-8')
        logins = {'generated_at': 1, 'saved_at': 2, 'players': [{'name': 'Bob', 'uuid': BOB, 'last_login': 'x',
                                                                 'login_count': 3}]}
        vanilla = mock.Mock(return_value={'minecraft:story/root': {'key': 'k', 'en': 'Minecraft', 'frame': 'task'}})
        with mock.patch.object(monitor.remote_mods_mirror, 'sync_tree', return_value={'fetched': 0, 'deleted': 0, 'missing': 0}), \
                mock.patch.object(monitor.remote_server_reader, 'player_stats', return_value={'players': []}), \
                mock.patch.object(monitor.remote_server_reader, 'forced_chunks', return_value={'dims': {}, 'errors': []}), \
                mock.patch.object(monitor.remote_server_reader, 'thaumcraft_progress',
                                  return_value={'players': {}, 'errors': 0}), \
                mock.patch.object(monitor.remote_server_reader, 'login_records', return_value=logins), \
                mock.patch.object(monitor.remote_server_reader, 'vanilla_advancements', vanilla), \
                mock.patch.object(monitor.command_catalog, 'refresh_server', return_value={'count': 1}), \
                mock.patch.object(monitor, 'query_server', return_value=''):
            monitor._mirror_extras()
            monitor._mirror_extras()                                   # the index is daily
        self.assertEqual(logins, json.loads((root / 'login-records.json').read_text(encoding='utf-8')))
        index = json.loads((root / 'advancement-index.json').read_text(encoding='utf-8'))
        self.assertEqual({'minecraft:story/root'}, set(index['advancements']))
        self.assertEqual('Minecraft', index['advancements']['minecraft:story/root']['name'])
        self.assertIn('minecraft:story/root', index['vanilla'])
        vanilla.assert_called_once()
        # Host unreachable on a later rebuild: the stored vanilla list is reused.
        (root / 'mod-inventory.json').write_text(json.dumps({'signature': 'v2', 'mods': []}), encoding='utf-8')
        with mock.patch.object(monitor.remote_server_reader, 'vanilla_advancements', side_effect=OSError('ssh')):
            monitor._refresh_advancement_index([])
        index = json.loads((root / 'advancement-index.json').read_text(encoding='utf-8'))
        self.assertIn('minecraft:story/root', index['advancements'])

    def test_lag_alert_adds_tick_percentiles(self):
        monitor = self.monitor
        monitor.LAG_ALERT_GROUPS = (2105,)
        answers = {'neoforge tps': world_tests.TPS, 'list': 'There are 1 of a max of 20 players online: Alice',
                   'neoforge entity list': world_tests.ENTITIES, 'mio_icif perf': world_tests.PERF, 'tick query': TICK}
        with mock.patch.object(monitor, 'query_server', side_effect=lambda server, command: answers.get(command, '')), \
                mock.patch.object(monitor.remote_server_reader, 'host_status', return_value=r13.HOST):
            texts = [monitor._lag_watch({'timestamp': 5000 + 60 * i, 'tps': 7.5, 'mspt': 130, 'tick_parser': 2})
                     for i in range(5)]
        self.assertIn('最近 100 刻：平均每刻 52.6 ms（目标 50 ms），P50 43.6 / P95 81.7 / P99 99.8 ms', texts[4])


if __name__ == '__main__':
    unittest.main()
