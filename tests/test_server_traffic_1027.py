"""Round 26: who and what uses the game server's network (server_traffic). Offline.

Fixture numbers follow the live checks on 10-27 (nettop at 11:57, the 30-second JFR test at 11:47);
player names, addresses and ports are made up.
"""
import base64
import contextlib
import copy
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests
from tests.test_server_round8 import text_reply, tool_call
import sanae_local_ops
import server_traffic as t

s = runtime_tests.s

PS = ('  501 /usr/sbin/cfprefsd agent\n'
      '84240 /Users/u/runtime/jdk/Contents/Home/bin/java -Xmx10G @libraries/net/neoforged/neoforge/unix_args.txt nogui\n'
      '90000 /Users/u/runtime/python/bin/python3 -c script\n')
NETTOP = """,bytes_in,bytes_out,
java.84240,4356591,1267403598,
tcp4 192.168.1.102:25565<->203.0.113.5:64886,103026,181389855,
tcp4 192.168.1.102:25565<->198.51.100.7:9064,505354,192365904,
tcp4 192.168.1.102:25565<->192.168.1.50:58494,968111,338554630,
tcp4 192.168.1.102:25575<->192.168.1.20:50000,5000,9000,
tcp6 *.25565<->*.*,,,
,bytes_in,bytes_out,
java.84240,4362662,1268369161,
tcp4 192.168.1.102:25565<->203.0.113.5:64886,103551,182161596,
tcp4 192.168.1.102:25565<->198.51.100.7:9064,505438,192440054,
tcp4 192.168.1.102:25565<->192.168.1.50:58494,968195,338696842,
tcp4 192.168.1.102:25565<->203.0.113.99:40000,10,500,
tcp4 192.168.1.102:25575<->192.168.1.20:50000,6000,19000,
tcp6 *.25565<->*.*,,,
"""
LOG = ('[27Oct2026 10:01:02.778] [Server thread/INFO] [net.minecraft.server.players.PlayerList/]: '
       'Alice[/203.0.113.5:64886] logged in with entity id 1134446 at (1.0, 64.0, 2.0)\n'
       '[27Oct2026 10:05:00.100] [Server thread/INFO] [net.minecraft.server.players.PlayerList/]: '
       'Bob[/198.51.100.7:9064] logged in with entity id 1165002 at (1.0, 64.0, 2.0)\n'
       '[27Oct2026 10:06:00.100] [Server thread/INFO] [net.minecraft.server.players.PlayerList/]: '
       'Carol[/192.168.1.50:58494] logged in with entity id 1167071 at (1.0, 64.0, 2.0)\n')
NETWORK = {'sent': {'totalBytes': 27_100_000, 'count': 217954, 'bytesPerSecond': 848000, 'countPerSecond': 6800,
                    'topContributors': [
                        {'protocolId': 'play', 'packetId': 'minecraft:block_entity_data', 'totalBytes': 16_348_256, 'count': 55850},
                        {'protocolId': 'play', 'packetId': 'minecraft:custom_payload', 'totalBytes': 4_200_000, 'count': 68000},
                        {'protocolId': 'play', 'packetId': 'minecraft:level_chunk_with_light', 'totalBytes': 3_700_000, 'count': 88},
                        {'protocolId': 'play', 'packetId': 'minecraft:update_attributes', 'totalBytes': 1_200_000, 'count': 2764}]},
           'received': {'totalBytes': 49_000, 'count': 3000, 'topContributors': [
               {'protocolId': 'play', 'packetId': 'minecraft:container_click', 'totalBytes': 22_000, 'count': 40}]}}
REPORT = {'durationMs': 32316, 'serverTick': {'averageMs': 53.3, 'p90': 70.61}, 'network': NETWORK,
          'cpuPercent': {}, 'heap': {}}
SUMMARY = {'recording': {'events': [
    {'type': 'minecraft.NetworkSummary', 'values': {'remoteAddress': '/203.0.113.5:64886', 'sentBytes': 6_000_000,
                                                    'sentPackets': 900, 'receivedBytes': 9000, 'receivedPackets': 50}},
    {'type': 'minecraft.NetworkSummary', 'values': {'remoteAddress': '/203.0.113.5:64886', 'sentBytes': 3_600_000,
                                                    'sentPackets': 600, 'receivedBytes': 6000, 'receivedPackets': 40}},
    {'type': 'minecraft.NetworkSummary', 'values': {'remoteAddress': '/198.51.100.7:9064', 'sentBytes': 4_600_000,
                                                    'sentPackets': 700, 'receivedBytes': 3000, 'receivedPackets': 30}},
    {'type': 'minecraft.NetworkSummary', 'values': {'remoteAddress': '/203.0.113.77:1001', 'sentBytes': 476,
                                                    'sentPackets': 1, 'receivedBytes': 20, 'receivedPackets': 2}}]}}
SERVER = {'id': 'legacy', 'prefix': '[怀旧]', 'server_dir': '', 'remote_ssh_host': 'game-host',
          'remote_server_dir': '/srv/mc'}


def run_remote(script, config, root, outputs):
    """Run a host script here with a fake subprocess.run; returns its printed JSON."""
    config = dict(config, root=str(root))
    code = script.replace('__CONFIG__', base64.b64encode(json.dumps(config).encode()).decode())
    calls = []

    def fake(args, **_kwargs):
        calls.append(args)
        name = args[0].rsplit('/', 1)[-1]
        return subprocess.CompletedProcess(args, 0, stdout=outputs.get(name, ''), stderr='')
    buffer = io.StringIO()
    with mock.patch('subprocess.run', side_effect=fake), contextlib.redirect_stdout(buffer):
        exec(compile(code, 'remote', 'exec'), {'__name__': '__main__'})
    return json.loads(buffer.getvalue()), calls


class HostScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'logs').mkdir()
        (self.root / 'logs' / 'latest.log').write_text(LOG, encoding='utf-8')

    def test_players_script_matches_connections_to_names_and_returns_no_addresses(self):
        out, calls = run_remote(t._PLAYERS_SCRIPT, {'seconds': 5, 'port': 25565}, self.root,
                                {'ps': PS, 'nettop': NETTOP})
        self.assertTrue(out['found'])
        nettop = next(args for args in calls if args[0].endswith('nettop'))
        self.assertEqual(['-p', '84240'], nettop[nettop.index('-p'):nettop.index('-p') + 2])
        rows = {row['player']: row for row in out['conns']}
        self.assertEqual(['Alice', 'Carol', 'Bob', ''], [row['player'] for row in out['conns']])
        self.assertEqual((525, 771741, 182161596), (rows['Alice']['in'], rows['Alice']['out'], rows['Alice']['out_total']))
        self.assertEqual(('public', 'lan'), (rows['Alice']['kind'], rows['Carol']['kind']))
        self.assertEqual(0, rows['']['out'])          # connected between the two samples
        self.assertEqual(4, len(out['conns']))        # the RCON port is not a game connection
        self.assertNotRegex(json.dumps(out), r'\d+\.\d+\.\d+\.\d+')

    def test_older_logs_fill_in_players_from_before_the_midnight_roll(self):
        import gzip
        (self.root / 'logs' / 'latest.log').write_text(LOG.split('\n', 1)[1], encoding='utf-8')
        with gzip.open(self.root / 'logs' / '2026-10-26-1.log.gz', 'wt', encoding='utf-8') as stream:
            stream.write(LOG.split('\n', 1)[0] + '\n')
        out, _calls = run_remote(t._PLAYERS_SCRIPT, {'seconds': 5, 'port': 25565}, self.root,
                                 {'ps': PS, 'nettop': NETTOP})
        self.assertIn('Alice', [row['player'] for row in out['conns']])

    def test_players_script_without_the_server(self):
        out, calls = run_remote(t._PLAYERS_SCRIPT, {'seconds': 5, 'port': 25565}, self.root, {'ps': '  1 /sbin/launchd\n'})
        self.assertFalse(out['found'])
        self.assertNotIn('conns', out)
        self.assertFalse(any(args[0].endswith('nettop') for args in calls))

    def test_packets_script_reads_the_report_and_sums_connections(self):
        debug = self.root / 'debug'
        debug.mkdir()
        (debug / 'server-2026-10-27-114707.jfr').write_bytes(b'x' * 1000)
        (debug / 'jfr-report-server-2026-10-27-114707.json').write_text(json.dumps(REPORT), encoding='utf-8')
        out, calls = run_remote(t._PACKETS_SCRIPT, {'file': 'server-2026-10-27-114707.jfr', 'wait': 1}, self.root,
                                {'ps': PS, 'jfr': json.dumps(SUMMARY)})
        self.assertEqual([], out['errors'])
        self.assertEqual(NETWORK, out['report']['network'])
        printed = next(args for args in calls if args[0].endswith('/jfr'))
        self.assertEqual('/Users/u/runtime/jdk/Contents/Home/bin/jfr', printed[0])
        self.assertIn('minecraft.NetworkSummary', printed)
        self.assertEqual([('Alice', 9_600_000), ('Bob', 4_600_000), ('', 476)],
                         [(row['player'], row['sent']) for row in out['players']])
        self.assertEqual({'files': 2, 'bytes': 1000 + len(json.dumps(REPORT))}, out['debug'])
        self.assertNotRegex(json.dumps(out), r'\d+\.\d+\.\d+\.\d+')

    def test_packets_script_rejects_paths_and_reports_a_missing_summary(self):
        (self.root / 'debug').mkdir()
        out, calls = run_remote(t._PACKETS_SCRIPT, {'file': '../../etc/passwd.jfr', 'wait': 0}, self.root, {'ps': PS})
        self.assertEqual('', out['file'])
        self.assertFalse(any(args[0].endswith('/jfr') for args in calls))
        (self.root / 'debug' / 'server-1.jfr').write_bytes(b'x')
        out, _calls = run_remote(t._PACKETS_SCRIPT, {'file': '', 'wait': 0}, self.root, {'ps': PS, 'jfr': ''})
        self.assertEqual('server-1.jfr', out['file'])
        self.assertIn('report:missing', out['errors'])
        self.assertIn('jfr:print', out['errors'])


PLAYERS = {'found': True, 'at': 1793072000, 'seconds': 5, 'errors': [], 'conns': [
    {'player': 'Alice', 'kind': 'public', 'in': 3215, 'out': 795_000, 'in_total': 1, 'out_total': 266_000_000},
    {'player': 'Bob', 'kind': 'public', 'in': 1825, 'out': 240_000, 'in_total': 1, 'out_total': 249_000_000},
    {'player': 'Carol', 'kind': 'lan', 'in': 180, 'out': 174_000, 'in_total': 1, 'out_total': 372_000_000},
    {'player': '', 'kind': 'public', 'in': 10, 'out': 500, 'in_total': 10, 'out_total': 500}]}
TICK = {'average': 75.9, 'target': 50.0, 'p50': 72.7, 'p95': 126.8, 'p99': 137.1, 'behind': True}


class FormatTests(unittest.TestCase):
    def test_members_see_totals_and_the_spread_without_names(self):
        text = t.format_players('[怀旧]', PLAYERS, tick=TICK)
        self.assertIn('游戏端口 4 个连接，对应到在线玩家 3 个', text)
        self.assertIn('各玩家的下行（不显示名字）：155 KB/s、47 KB/s、34 KB/s；最多的一位占 66%', text)
        self.assertIn('另有 1 个连接没对应到玩家', text)
        self.assertIn('流量集中在一位玩家身上，占 66%', text)
        self.assertIn('不大，一般家用宽带上行扛得住', text)
        self.assertIn('每刻平均 76 ms，超过 50 ms', text)
        for name in ('Alice', 'Bob', 'Carol'):
            self.assertNotIn(name, text)

    def test_admins_see_names_and_what_is_loaded_around_them(self):
        text = t.format_players('[怀旧]', PLAYERS, detailed=True, nearby={'Alice': [1278, 301], 'Bob': [944, None]})
        self.assertIn('Alice ↓155 KB/s ↑643 B/s（本次登录共收 253.7 MB）［附近方块实体 1278、实体 301］', text)
        self.assertIn('Bob ↓47 KB/s', text)
        self.assertIn('［附近方块实体 944、实体 ?］', text)
        self.assertIn('流量集中在一位玩家身上（Alice）', text)

    def test_the_latest_packet_sample_is_mentioned_without_names(self):
        sample = {'at': 1793072000, 'report': REPORT, 'players': [{'player': 'Alice', 'sent': 1}]}
        text = t.format_players('[怀旧]', PLAYERS, last_sample=sample)
        self.assertIn('里发得最多的：方块实体数据 60%、模组自定义包 15%、区块（含光照） 14%、实体属性更新 4%', text)
        self.assertNotIn('Alice', text)

    def test_failures(self):
        self.assertIn('没找到服务器的 Java 进程', t.format_players('[怀旧]', {'found': False}))
        self.assertIn('nettop:no-sample', t.format_players('[怀旧]', {'found': True, 'errors': ['nettop:no-sample']}))
        busy = dict(PLAYERS, conns=[dict(row, out=row['out'] * 20) for row in PLAYERS['conns']])
        self.assertIn('已经不小', t.format_players('[怀旧]', busy))

    def test_packet_report_for_admins_and_members(self):
        data = {'at': 1793072000, 'seconds': 30, 'size': 15_187_233, 'report': REPORT, 'debug': {'files': 4, 'bytes': 30_400_000},
                'players': [{'player': 'Alice', 'sent': 9_600_000}, {'player': 'Bob', 'sent': 4_600_000},
                            {'player': '', 'sent': 476}]}
        admin = t.format_packets('[怀旧]', data, detailed=True)
        self.assertIn('数据包采样（原版 JFR，32 秒', admin)
        self.assertIn('服务器发出 25.8 MB（819 KB/s，约 6.7 Mbps，21.8万个包）', admin)
        self.assertIn('方块实体数据 15.6 MB（60%，5.6万个）；模组自定义包 4.0 MB（15%，6.8万个）；区块（含光照） 3.5 MB（14%，88个）', admin)
        self.assertIn('方块实体数据是机器、管道、箱子、告示牌等方块的状态同步', admin)
        self.assertIn('玩家发来最多的：点击容器格子 21 KB', admin)
        self.assertIn('Alice 9.2 MB（68%）；Bob 4.4 MB（32%）；另有 1 个连接没对应到玩家', admin)
        self.assertIn('每刻平均 53.3 ms（P90 70.6 ms）', admin)
        self.assertIn('debug/ 现有 4 个采样文件，共 29.0 MB', admin)
        self.assertIn('大头是方块实体数据（60%）', admin)
        member = t.format_packets('[怀旧]', data)
        self.assertIn('2 位玩家里收到数据最多的一位占 68%', member)
        self.assertNotIn('Alice', member)
        self.assertNotIn('debug/', member)
        # 10-27 12:02: the second sample's report said durationMs 905156 for a 30-second recording.
        long = dict(data, report=dict(REPORT, durationMs=905_156), elapsed=0)
        self.assertIn('数据包采样（原版 JFR，30 秒', t.format_packets('[怀旧]', long))
        self.assertIn('（882 KB/s，约 7.2 Mbps', t.format_packets('[怀旧]', long))
        self.assertIn('数据包采样（原版 JFR，31 秒', t.format_packets('[怀旧]', dict(long, elapsed=31.4)))
        self.assertEqual('方块实体数据', t.packet_label('minecraft:block_entity_data', 'play'))
        self.assertEqual('mod:thing（进服配置阶段）', t.packet_label('mod:thing', 'configuration'))


class SampleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.sleeps = []
        patcher = mock.patch.object(t, '_sleep', side_effect=self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.commands = []
        self.answers = {'jfr start': 'JFR profiling started',
                        'jfr stop': 'JFR profiling stopped and dumped to debug/server-2026-10-27-114707.jfr'}

    def query(self, command):
        self.commands.append(command)
        self.assertFalse((self.state / t.MARKER_FILE).exists() and command == 'jfr start')
        return self.answers[command]

    def test_start_wait_stop_read_and_cooldown(self):
        clock = iter([1000.0, 1001.0, 1033.0, 1100.0, 1200.0])
        data = {'file': 'server-2026-10-27-114707.jfr', 'report': REPORT, 'players': [], 'errors': []}
        with mock.patch.object(t.remote_server_reader, '_run', return_value=dict(data)) as run:
            first, state = t.sample_packets(SERVER, self.query, self.state, 45, now=lambda: next(clock))
            again, state_again = t.sample_packets(SERVER, self.query, self.state, now=lambda: next(clock))
        self.assertEqual(('fresh', 'cached'), (state, state_again))
        self.assertEqual(['jfr start', 'jfr stop'], self.commands)
        self.assertEqual([45], self.sleeps)
        self.assertEqual({'file': 'server-2026-10-27-114707.jfr', 'wait': 30}, run.call_args.args[2])
        self.assertEqual(1, run.call_count)
        self.assertEqual(first['report'], again['report'])
        self.assertFalse((self.state / t.MARKER_FILE).exists())
        self.assertTrue((self.state / t.SAMPLE_FILE).exists())

    def test_seconds_are_clamped(self):
        for asked, slept in ((5, 20), (600, 60), ('x', 30), (None, 30)):
            self.sleeps.clear()
            (self.state / t.SAMPLE_FILE).unlink(missing_ok=True)
            with mock.patch.object(t.remote_server_reader, '_run', return_value={'report': REPORT}):
                t.sample_packets(SERVER, self.query, self.state, asked)
            self.assertEqual([slept], self.sleeps, asked)

    def test_a_recording_it_did_not_start_is_left_alone(self):
        self.answers['jfr start'] = 'Failed to start JFR profiling'
        data, state = t.sample_packets(SERVER, self.query, self.state)
        self.assertIsNone(data)
        self.assertEqual('failed: Failed to start JFR profiling', state)
        self.assertEqual(['jfr start'], self.commands)

    def test_the_stop_runs_even_when_the_wait_is_interrupted(self):
        t._sleep.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            t.sample_packets(SERVER, self.query, self.state)
        self.assertEqual(['jfr start', 'jfr stop'], self.commands)
        self.assertFalse((self.state / t.MARKER_FILE).exists())
        self.assertFalse(t._LOCK.locked())

    def test_one_sample_at_a_time(self):
        with t._LOCK:
            self.assertEqual((None, 'busy'), t.sample_packets(SERVER, self.query, self.state))
        self.assertEqual([], self.commands)

    def test_a_sample_left_running_by_a_restart_is_stopped_later(self):
        t._write_json(self.state / t.MARKER_FILE, {'at': 1000, 'seconds': 30})
        self.assertFalse(t.recover(self.state, self.query, now=1100))      # may still be running
        self.assertEqual([], self.commands)
        self.assertTrue(t.recover(self.state, self.query, now=1200))
        self.assertEqual(['jfr stop'], self.commands)
        self.assertFalse((self.state / t.MARKER_FILE).exists())
        self.assertFalse(t.recover(self.state, self.query, now=1300))

    def test_reports(self):
        with mock.patch.object(t, 'sample_packets', return_value=(None, 'busy')):
            self.assertIn('已经有一次数据包采样在进行', t.packets_report(SERVER, self.query, self.state))
        with mock.patch.object(t, 'sample_packets', return_value=(None, 'failed: Failed to start JFR profiling')):
            self.assertIn('数据包采样没成功：Failed to start JFR profiling。', t.packets_report(SERVER, self.query, self.state))
        self.assertIn('最近 24 小时没有数据包采样', t.last_packets_report(SERVER, self.state))
        t._write_json(self.state / t.SAMPLE_FILE, {'at': 1, 'report': REPORT})
        self.assertIn('最近 24 小时没有数据包采样', t.last_packets_report(SERVER, self.state))


class WiringTests(unittest.TestCase):
    def test_tool_offered_to_everyone(self):
        self.assertIn(s.SERVER_TRAFFIC, s.TOOLS_MEMBER)
        self.assertIn(s.SERVER_TRAFFIC, s.TOOLS_ADMIN)
        self.assertIn('server_traffic', s.READ_QUERY_TOOL_NAMES)
        self.assertIn('server_traffic', sanae_local_ops.INSIGHT_TOOLS)
        self.assertEqual(['players', 'packets'],
                         s.SERVER_TRAFFIC['function']['parameters']['properties']['topic']['enum'])
        self.assertIn('server_traffic', s.SANAE_OPERATIONAL_POLICY)

    def test_questions(self):
        for text in ('服务器里面谁发包最多', '什么东西发包最多导致网络延迟', '谁流量最大', '看看服务器流量', '带宽占用多少',
                     '查一下数据包类型', '开一次 JFR 采样', '是谁占用了带宽'):
            with self.subTest(text=text):
                self.assertTrue(s._has_server_query_intent(text))
        for text in ('我手机流量还剩多少', '流量卡哪家好'):
            with self.subTest(text=text):
                self.assertFalse(s._has_server_query_intent(text))

    def test_members_never_start_a_sample(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.dict(os.environ, {'OPS_STATE_BASE': temp}), \
                mock.patch.object(sanae_local_ops, '_resolved_server', return_value=SERVER), \
                mock.patch.object(sanae_local_ops.server_registry, 'query_server') as query, \
                mock.patch.object(sanae_local_ops.server_traffic, 'sample_packets') as sample:
            text = sanae_local_ops.execute_insight('server_traffic', {'topic': 'packets'}, SERVER, False)
        self.assertIn('没有数据包采样', text)
        sample.assert_not_called()
        query.assert_not_called()

    def test_routing(self):
        with mock.patch.object(sanae_local_ops, '_resolved_server', return_value=SERVER), \
                mock.patch.object(sanae_local_ops.server_traffic, 'players_report', return_value='P') as players, \
                mock.patch.object(sanae_local_ops.server_traffic, 'packets_report', return_value='K') as packets:
            self.assertEqual('P', sanae_local_ops.execute_insight('server_traffic', {}, SERVER, False))
            self.assertEqual('K', sanae_local_ops.execute_insight('server_traffic', {'topic': 'packets', 'seconds': 40},
                                                                  SERVER, True))
            with self.assertRaises(ValueError):
                sanae_local_ops.execute_insight('server_traffic', {'topic': 'pcap'}, SERVER, True)
        self.assertFalse(players.call_args.args[3])
        self.assertEqual(40, packets.call_args.args[3])
        local = dict(SERVER, remote_ssh_host='')
        with mock.patch.object(sanae_local_ops, '_resolved_server', return_value=local):
            self.assertIn('不在远端主机上', sanae_local_ops.execute_insight('server_traffic', {}, local, True))

    def run_turn(self, responses, tool_result='[怀旧] 服务器流量'):
        sent, replies = [], iter(responses)

        def model(payload, **kwargs):
            sent.append(copy.deepcopy(payload))
            return next(replies)
        with mock.patch.object(s, '_text_post', side_effect=model), \
                mock.patch.object(s, 'execute_tool', return_value=tool_result) as tool:
            answer, _usage, _used = s.run_agent('早苗，服务器里面谁发包最多？是不是有人占网导致延迟', 'member', False,
                                                'fixture-group', selected_server=SERVER, explicit_server=True)
        return answer, sent, tool

    def test_a_live_answer_without_any_tool_is_sent_back_once(self):
        # 10-27 replay: tool_choice=required, yet the relay answered with invented findings and no call.
        made_up = text_reply('我查看了服务器的网络现状，以下是相关记录的结果：网络整体性能正常')
        answer, sent, tool = self.run_turn([made_up, tool_call('server_traffic', {'topic': 'players'}),
                                            text_reply('流量不大，延迟主要是每刻耗时高')])
        self.assertEqual('流量不大，延迟主要是每刻耗时高', answer)
        self.assertEqual(['required', 'required'], [payload.get('tool_choice') for payload in sent[:2]])
        self.assertIn('没有调用任何查询工具', sent[1]['messages'][-1]['content'])
        self.assertIn('不要提到它', sent[1]['messages'][-1]['content'])
        self.assertNotIn('QQ 群操作', sent[1]['messages'][-1]['content'])
        self.assertEqual('server_traffic', tool.call_args.args[0])
        answer, sent, tool = self.run_turn([made_up, made_up])
        self.assertTrue(answer.startswith('这次没拿到实时数据'))
        self.assertEqual(2, len(sent))
        tool.assert_not_called()

    def test_admin_players_view_asks_servercore_for_the_neighbourhood(self):
        answers = {'tick query': 'Average time per tick: 75.9ms (Target: 50.0ms)',
                   'statistics block-entities byPlayer': 'Block Entities by Player\n 1. Alice 1278\n 2. Bob 944\n',
                   'statistics entities byPlayer': 'Entities by Player\n 1. Alice 301\n'}
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(t, 'collect_players', return_value=PLAYERS):
            admin = t.players_report(SERVER, answers.get, temp, True)
            calls = []
            member = t.players_report(SERVER, lambda command: calls.append(command) or answers.get(command), temp, False)
        self.assertIn('［附近方块实体 1278、实体 301］', admin)
        self.assertEqual(['tick query'], calls)
        self.assertNotIn('Alice', member)


if __name__ == '__main__':
    unittest.main()
