"""Round 24: the game server's network, measured on the game host. Offline.

Fixture numbers are from the live check on 10-27 12:24 (machine time); addresses are made up.
"""
import base64
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests
import network_probe
import sanae_local_ops
import server_network

s = runtime_tests.s
NODES = [address for _label, address in network_probe.REFERENCE_NODES]
HOST = {'ping': {NODES[0]: {'loss': 0.0, 'avg': 39.3}, NODES[1]: {'loss': 0.0, 'avg': 45.4},
                 NODES[2]: {'loss': 0.0, 'avg': 13.4}, NODES[3]: {'loss': 0.0, 'avg': 42.4},
                 'gateway': {'loss': 0.0, 'avg': 4.6}},
        'connections': {'public': 4, 'lan': 1, 'loopback': 0}, 'prefixes': [['203.0.113.x', 2], ['198.51.100.x', 2]],
        'egress': '203.0.113.9', 'dns': ['203.0.113.9'], 'hairpin': True, 'errors': []}
DATA = {'port': 25565, 'public_host': 'mc.example.org', 'public_port': 25566, 'at': 1793070000,
        'status': {'ms': 31.0, 'online': 5, 'max': 20}, 'host': HOST}


def with_host(**changes):
    host = dict(HOST, **changes)
    return dict(DATA, host=host)


class ReportTests(unittest.TestCase):
    def test_a_healthy_server(self):
        text = server_network.format_report('[怀旧]', DATA, online=5)
        self.assertIn('- 服务端：局域网状态查询 31 ms 回应，在线 5/20', text)
        self.assertIn('电信 39.3 ms，联通 45.4 ms，移动 13.4 ms', text)
        self.assertIn('- 局域网网关：4.6 ms', text)
        self.assertIn('游戏端口 25565 的连接：5 个（公网 4、局域网 1、本机 0）；在线名单 5 人', text)
        self.assertIn('域名解析和服务器出口地址一致（DDNS）', text)
        self.assertIn('从服务器回连公网端口：通', text)
        self.assertIn('判断：没看到明显问题。', text)
        self.assertNotIn('203.0.113', text)

    def test_admins_see_addresses(self):
        text = server_network.format_report('[怀旧]', DATA, detailed=True, online=5)
        self.assertIn('管理员可见：出口 203.0.113.9；解析 203.0.113.9；公网连接来源：203.0.113.x ×2、198.51.100.x ×2', text)

    def test_problems_are_named(self):
        pings = dict(HOST['ping'], **{NODES[1]: {'loss': 50.0, 'avg': 45.8}})
        text = server_network.format_report('[怀旧]', with_host(ping=pings), online=5)
        self.assertIn('联通 45.8 ms（丢包 50%）', text)
        self.assertIn('联通方向有丢包', text)
        gateway = dict(HOST['ping'], gateway={'loss': 25.0, 'avg': 9.0})
        self.assertIn('问题在局域网', server_network.format_report('[怀旧]', with_host(ping=gateway)))
        crowded = server_network.format_report('[怀旧]', with_host(connections={'public': 9, 'lan': 0, 'loopback': 0}), online=5)
        self.assertIn('连接比在线人数多 4 个', crowded)
        moved = server_network.format_report('[怀旧]', with_host(dns=['198.51.100.7']))
        self.assertIn('和服务器出口地址不一致', moved)
        self.assertIn('DDNS 没更新', moved)
        down = server_network.format_report('[怀旧]', dict(DATA, status={'error': 'StatusError'}))
        self.assertIn('局域网状态查询没有回应（StatusError）', down)
        unreachable = server_network.format_report('[怀旧]', dict(DATA, host={'error': 'OSError'}))
        self.assertIn('服务器主机连不上（OSError）', unreachable)
        self.assertIn('不通', server_network._ping_text({'loss': 100.0, 'avg': None}))


class CollectTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(server_network, '_CACHE', {})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        Path(self.temp.name, 'server.properties').write_text('server-port=25565\n', encoding='utf-8')
        self.server = {'id': 'legacy', 'host': '192.0.2.10', 'address': 'mc.example.org:25566',
                       'server_dir': self.temp.name, 'remote_ssh_host': 'game-host', 'remote_server_dir': '/srv/mc'}

    def test_one_run_per_minute_and_fixed_inputs(self):
        status = mock.Mock(return_value={'players': {'online': 5, 'max': 20}})
        with mock.patch.object(server_network.remote_server_reader, '_run', return_value=HOST) as run:
            first = server_network.collect(self.server, status=status, now=1000, players=lambda: TCP)
            again = server_network.collect(self.server, status=status, now=1050, players=lambda: TCP)
            later = server_network.collect(self.server, status=status, now=1061, players=lambda: TCP)
        self.assertIs(first, again)
        self.assertIsNot(first, later)
        self.assertEqual(2, run.call_count)
        self.assertIs(TCP, first['tcp'])
        status.assert_called_with('192.0.2.10', 25565, timeout=5)
        config = run.call_args.args[2]
        self.assertEqual({'nodes': NODES + list(network_probe.BACKUP_NODES.values()), 'port': 25565,
                          'public_host': 'mc.example.org', 'public_port': 25566}, config)

    def test_a_bad_address_is_not_passed_to_the_host(self):
        self.server['address'] = "evil.example;rm -rf /:25566"
        with mock.patch.object(server_network.remote_server_reader, '_run', return_value=HOST) as run:
            server_network.collect(self.server, status=mock.Mock(return_value={}), now=1, players=lambda: {})
        self.assertEqual('', run.call_args.args[2]['public_host'])

    def test_the_remote_script_compiles(self):
        compile(server_network._SCRIPT.replace('__CONFIG__', 'e30='), 'remote', 'exec')

    def test_a_failed_tcp_fetch_does_not_stop_the_report(self):
        def broken():
            raise OSError('ssh')
        with mock.patch.object(server_network.remote_server_reader, '_run', return_value=HOST):
            data = server_network.collect(self.server, status=mock.Mock(return_value={}), now=1, players=broken)
        self.assertEqual({'error': 'OSError'}, data['tcp'])
        self.assertIn('服务器主机到运营商', server_network.format_report('[怀旧]', data))


class WiringTests(unittest.TestCase):
    def test_tool_and_questions(self):
        self.assertIn(s.SERVER_NETWORK, s.TOOLS_MEMBER)
        self.assertIn('server_network', s.READ_QUERY_TOOL_NAMES)
        self.assertIn('server_network', sanae_local_ops.INSIGHT_TOOLS)
        for text in ('早苗，服务器网络怎么样', '服务器延迟多少', '今天服务器丢包吗', '连不上服务器了', '老是掉线', '服务器 ping 多少'):
            with self.subTest(text=text):
                self.assertTrue(s._has_server_query_intent(text))
        self.assertFalse(s._has_server_query_intent('我家网络不太好'))

    def test_execute_insight_passes_the_online_count(self):
        server = {'id': 'legacy', 'prefix': '[怀旧]', 'server_dir': ''}
        with mock.patch.object(sanae_local_ops, '_resolved_server', return_value=server), \
                mock.patch.object(sanae_local_ops.server_registry, 'query_server',
                                  return_value='There are 2 of a max of 20 players online: A, B'), \
                mock.patch.object(sanae_local_ops.server_network, 'report', return_value='REPORT') as report:
            self.assertEqual('REPORT', sanae_local_ops.execute_insight('server_network', {}, server, True))
        report.assert_called_once_with(server, '[怀旧]', True, online=2)

    def test_ping_says_where_it_measures(self):
        result = {'mode': 'normal', 'latency': [], 'upload': {'status': 'unknown', 'max_upload_bytes': 48_000_000},
                  'elapsed_seconds': 1.0}
        text = network_probe.render_network(result)
        self.assertIn('机器人主机 ICMP', text)
        self.assertNotIn('服务器侧', text)


# Round 27 (10-27 14:42 live): public players 3.8 % retransmitted, LAN 0.4 %; 20 timeouts, all after stalls.
def conn(player, kind, out_total, retx_total, rtt=40.0):
    return {'player': player, 'kind': kind, 'in': 0, 'out': 0, 'in_total': 0, 'out_total': out_total,
            'retx': 0, 'retx_total': retx_total, 'rtt': rtt}


TCP = {'found': True, 'conns': [conn('Alice', 'public', 100_000_000, 5_100_000, 39.0),
                                conn('Bob', 'public', 50_000_000, 1_600_000, 48.0),
                                conn('Carol', 'lan', 80_000_000, 320_000, 10.0),
                                conn('', 'public', 1000, 900)]}
LOG = {'since': '10:11', 'keepup': 599, 'keepup_max_ms': 28100, 'timeouts': [
    {'time': '10:21', 'player': 'Carol', 'lan': True, 'stall_ms': 2427},
    {'time': '10:56', 'player': 'Alice', 'lan': False, 'stall_ms': 9000},
    {'time': '11:22', 'player': 'Carol', 'lan': True, 'stall_ms': 0}]}


class TcpAndTimeoutTests(unittest.TestCase):
    def data(self, tcp=TCP, log=LOG, **host):
        return dict(DATA, tcp=tcp, host=dict(HOST, log=log, **host))

    def test_members_see_rates_and_counts_without_names(self):
        text = server_network.format_report('[怀旧]', self.data(), online=5)
        self.assertIn('- 玩家连接的 TCP 重传（本次登录以来）：公网 4.5%（2 人），局域网 0.4%（1 人）；公网里有 2 人超过 3%', text)
        self.assertIn('- 超时掉线（本次日志，从 10:11 起）：3 次，其中 2 次掉线前 45 秒内服务器落后 2 秒以上（最多 9.0 秒）；'
                      '局域网玩家也掉了 2 次', text)
        self.assertIn('- 服务器“跟不上”警告 599 次，最多落后 28.1 秒', text)
        self.assertIn('公网玩家的 TCP 重传 4.5%（局域网只有 0.4%），公网路径上有丢包；ping 运营商不丢包，多半是', text)
        self.assertIn('3 次超时掉线里 2 次发生在服务器卡住之后，局域网玩家也会掉：掉线主要是服务器卡顿造成的，不是网络', text)
        for name in ('Alice', 'Bob', 'Carol'):
            self.assertNotIn(name, text)

    def test_admins_see_players(self):
        text = server_network.format_report('[怀旧]', self.data(), detailed=True, online=5)
        self.assertIn('按玩家（管理员可见）：Alice 5.1%/39 ms、Bob 3.2%/48 ms、Carol 0.4%/10 ms', text)
        self.assertIn('按玩家（管理员可见）：Carol 2 次、Alice 1 次', text)

    def test_quiet_cases(self):
        clean = {'conns': [conn('Alice', 'public', 100_000_000, 900_000)]}
        text = server_network.format_report('[怀旧]', self.data(tcp=clean, log={'since': '10:11', 'timeouts': []}))
        self.assertIn('公网 0.9%（1 人）', text)
        self.assertIn('- 超时掉线（本次日志，从 10:11 起）：没有', text)
        self.assertIn('判断：没看到明显问题。', text)
        unstalled = dict(LOG, timeouts=[dict(row, stall_ms=0) for row in LOG['timeouts']])
        self.assertNotIn('服务器卡顿造成的', server_network.format_report('[怀旧]', self.data(log=unstalled)))
        self.assertNotIn('玩家连接的 TCP 重传', server_network.format_report('[怀旧]', self.data(tcp={'error': 'OSError'})))

    def test_one_lost_echo_is_not_packet_loss(self):
        pings = dict(HOST['ping'], **{NODES[1]: {'loss': 8.3, 'avg': 45.0}})
        text = server_network.format_report('[怀旧]', with_host(ping=pings))
        self.assertIn('联通 45.0 ms（丢包 8.3%）', text)
        self.assertNotIn('方向有丢包', text)
        pings = dict(HOST['ping'], **{NODES[1]: {'loss': 25.0, 'avg': 45.0}})
        self.assertIn('联通方向有丢包', server_network.format_report('[怀旧]', with_host(ping=pings)))
        loud = server_network.format_report('[怀旧]', dict(self.data(), host=dict(HOST, ping=pings, log=LOG)))
        self.assertNotIn('ping 运营商不丢包', loud)

    def test_the_unicom_reference_answers_ping(self):
        # 221.5.88.88 and later 210.21.196.6 dropped pings themselves; the routers in front of them did not.
        self.assertEqual('202.99.160.68', dict(network_probe.REFERENCE_NODES)['联通'])
        self.assertEqual({'电信', '联通', '移动'}, set(network_probe.BACKUP_NODES))

    def test_a_node_that_limits_ping_is_not_path_loss(self):
        # 10-27 19:00: "联通丢包率飙升至 83.3%" was the reference node alone.
        backup = network_probe.BACKUP_NODES['联通']
        pings = dict(HOST['ping'], **{NODES[1]: {'loss': 83.3, 'avg': 63.7}, backup: {'loss': 0.0, 'avg': 41.3}})
        member = server_network.format_report('[怀旧]', with_host(ping=pings))
        self.assertIn('联通 41.3 ms，', member)
        self.assertNotIn('方向有丢包', member)
        admin = server_network.format_report('[怀旧]', with_host(ping=pings), detailed=True)
        self.assertIn(f'联通参考节点 {NODES[1]} 丢包 83.3%，备用节点 {backup} 正常，是节点自己限速 ping', admin)
        both = dict(pings, **{backup: {'loss': 50.0, 'avg': 44.0}})
        text = server_network.format_report('[怀旧]', with_host(ping=both))
        self.assertIn('联通 44.0 ms（丢包 50%）', text)
        self.assertIn('联通方向有丢包', text)
        dead = dict(pings, **{NODES[1]: {'loss': 100.0, 'avg': None}, backup: {'loss': 100.0, 'avg': None}})
        self.assertIn('联通 不通', server_network.format_report('[怀旧]', with_host(ping=dead)))


SERVER_LOG = """[2710月2026 10:11:00.000] [main/INFO] [cpw.mods.modlauncher.Launcher/MODLAUNCHER]: start
[2710月2026 10:20:00.000] [Server thread/INFO] [net.minecraft.server.players.PlayerList/]: Carol[/192.168.1.50:58494] logged in with entity id 1 at (1.0, 64.0, 2.0)
[2710月2026 10:20:30.000] [Server thread/INFO] [net.minecraft.server.players.PlayerList/]: Alice[/203.0.113.5:64886] logged in with entity id 2 at (1.0, 64.0, 2.0)
[2710月2026 10:21:26.773] [Server thread/WARN] [com.rtsbuilding.rtsbuilding.RtsbuildingMod/]: [RTS-SERVER-HEALTH] schema=2 event=TICK_GAP gap_ms=3497 severity=SEVERE rts_runnable=0
[2710月2026 10:21:40.421] [Server thread/WARN] [net.minecraft.server.MinecraftServer/]: Can't keep up! Is the server overloaded? Running 2388ms or 47 ticks behind
[2710月2026 10:21:49.493] [Server thread/INFO] [net.minecraft.server.network.ServerGamePacketListenerImpl/]: Carol lost connection: Timed out
[2710月2026 10:40:00.000] [Server thread/WARN] [net.minecraft.server.MinecraftServer/]: Can't keep up! Is the server overloaded? Running 9000ms or 180 ticks behind
[2710月2026 10:41:00.000] [Server thread/INFO] [net.minecraft.server.network.ServerGamePacketListenerImpl/]: Alice lost connection: Timed out
[2710月2026 10:42:00.000] [Server thread/INFO] [net.minecraft.server.network.ServerGamePacketListenerImpl/]: Bob lost connection: Disconnected
"""


class HostLogTests(unittest.TestCase):
    def test_timeouts_are_matched_with_stalls_on_the_host(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'logs').mkdir()
            Path(root, 'logs', 'latest.log').write_text(SERVER_LOG, encoding='utf-8')
            config = {'nodes': NODES, 'port': 25565, 'public_host': '', 'public_port': 25566, 'root': root}
            code = server_network._SCRIPT.replace('__CONFIG__', base64.b64encode(json.dumps(config).encode()).decode())
            fake = lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout='', stderr='')  # noqa: E731
            buffer = io.StringIO()
            with mock.patch('subprocess.run', side_effect=fake), contextlib.redirect_stdout(buffer):
                exec(compile(code, 'remote', 'exec'), {'__name__': '__main__'})
        log = json.loads(buffer.getvalue())['log']
        self.assertEqual('10:11', log['since'])
        self.assertEqual((2, 9000), (log['keepup'], log['keepup_max_ms']))
        self.assertEqual([{'time': '10:21', 'player': 'Carol', 'lan': True, 'stall_ms': 3497},
                          {'time': '10:41', 'player': 'Alice', 'lan': False, 'stall_ms': 0}], log['timeouts'])


class TrafficRetransmissionTests(unittest.TestCase):
    def test_nettop_retransmissions_and_round_trip_per_player(self):
        from tests.test_server_traffic_1027 import LOG as JOINS, PS, run_remote
        import server_traffic
        nettop = ('\n'.join([',bytes_in,bytes_out,re-tx,rtt_avg,',
                             'java.84240,4356591,1267403598,,,',
                             'tcp4 192.168.1.102:25565<->203.0.113.5:64886,103026,181389855,9000000,42.25 ms,',
                             ',bytes_in,bytes_out,re-tx,rtt_avg,',
                             'java.84240,4362662,1268369161,,,',
                             'tcp4 192.168.1.102:25565<->203.0.113.5:64886,103551,182161596,9050000,41.50 ms,']) + '\n')
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'logs').mkdir()
            Path(root, 'logs', 'latest.log').write_text(JOINS, encoding='utf-8')
            out, _calls = run_remote(server_traffic._PLAYERS_SCRIPT, {'seconds': 5, 'port': 25565}, Path(root),
                                     {'ps': PS, 'nettop': nettop})
        row = out['conns'][0]
        self.assertEqual(('Alice', 50000, 9050000, 41.5), (row['player'], row['retx'], row['retx_total'], row['rtt']))


class QuestionTests(unittest.TestCase):
    def test_disconnect_questions(self):
        for text in ('怎么又掉线了', '服务器老是掉线', '游戏总掉线是不是网络问题', '超时掉线是怎么回事', 'Timed out 是网络问题吗'):
            with self.subTest(text=text):
                self.assertTrue(s._has_server_query_intent(text))
        self.assertFalse(s._has_server_query_intent('早苗账号掉线了'))


if __name__ == '__main__':
    unittest.main()
