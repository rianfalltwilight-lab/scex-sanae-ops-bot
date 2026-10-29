"""Round 37 (10-29 "尤其是对服务器信息掌控上的"): world backups had not worked since 10-26 03:12 and
nobody knew; 23:21-01:15 TPS sat at 7.6-14.3 without a lag alert; "早苗，分析下服务器掉刻原因" went to
tool-free chat; "服里喊话 问问他们干啥呢" from an admin was refused as a question. Offline."""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests import test_remote_legacy_monitor as monitor_tests
from tests import test_yachiyo_runtime as runtime_tests
import backup_status
import sanae_local_ops
import server_watch as sw
import world_status

s, bridge = runtime_tests.s, runtime_tests.bridge
NOW = 1793240700.0
GIB = 2 ** 30
FULL = 'Insufficient target disk space: 89900556288 free, 103079215104 required'


def write(path, data, bom=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8-sig' if bom else 'utf-8')


class BackupStatusTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tool = Path(temp.name) / 'tool'
        self.out = Path(temp.name) / 'out'
        write(self.tool / 'config.json', {'enabled': True, 'output_directory': str(self.out), 'interval_minutes': 120,
                                          'keep_complete_backups': 2, 'expected_archive_GiB': 32})
        write(self.out / 'state.json', {'last_success_epoch': NOW - 80 * 3600, 'last_complete': str(self.out / 'world_a.tar.gz'),
                                        'last_archive_bytes': 33636050324})
        (self.out / 'world_a.tar.gz').write_bytes(b'x' * 10)
        finished = datetime.fromtimestamp(NOW - 120, timezone.utc).isoformat().replace('+00:00', 'Z')
        # Run-BackupTask.ps1 writes last-task.json with Set-Content -Encoding UTF8 (a BOM).
        write(self.tool / 'last-task.json', {'finishedAt': finished, 'exitCode': 1}, bom=True)
        write(self.tool / 'last-task.stderr.json', {'status': 'FAILED', 'state': 'failed', 'message': FULL})
        (self.tool / 'last-task.stdout.json').write_text('', encoding='utf-8')

    def test_a_failing_backup_is_told_with_its_reason_to_admins(self):
        data = backup_status.read(self.tool, now=NOW)
        self.assertTrue(backup_status.failing(data))
        self.assertAlmostEqual(80, backup_status.stale_hours(data))
        self.assertEqual(96 * GIB, data['required'])                 # 20 GiB + 2 x ceil(31.3 GiB x 1.2)
        admin = backup_status.format_status('[怀旧]', data, detailed=True)
        self.assertIn('（3 天 8 小时前），31.3 GiB，完整校验通过', admin)
        self.assertIn('之后的备份没做成：最近一次', admin)
        self.assertIn('空间不够：剩 83.7 GiB，备份工具要求至少 96.0 GiB', admin)
        self.assertIn('保留 1 份', admin)
        member = backup_status.format_status('[怀旧]', data, detailed=False)
        self.assertIn('之后的备份没做成', member)
        self.assertNotIn('空间', member)
        alert = backup_status.format_alert('[怀旧]', data)
        self.assertIn('【备份提醒】世界已经 3 天 8 小时没有成功备份了', alert)

    def test_skipped_runs_are_not_failures(self):
        (self.tool / 'last-task.stderr.json').write_text('', encoding='utf-8')
        write(self.tool / 'last-task.stdout.json', {'status': 'NO_PLAYERS', 'state': 'no_players'})
        write(self.tool / 'last-task.json', {'finishedAt': '2026-10-29T02:21:08Z', 'exitCode': 0}, bom=True)
        data = backup_status.read(self.tool, now=NOW)
        self.assertFalse(backup_status.failing(data))
        self.assertIn('没人在线，按规则跳过', backup_status.format_status('[怀旧]', data))

    def test_without_the_tool(self):
        data = backup_status.read(Path(self.tool.parent) / 'missing', now=NOW)
        self.assertFalse(data['available'])
        self.assertIn('备份情况暂时查不到', backup_status.format_status('[怀旧]', data))

    def test_topic_backup_members_and_admins(self):
        data = backup_status.read(self.tool, now=NOW)
        server = {'id': 'legacy', 'prefix': '[怀旧]', 'server_dir': str(self.out)}
        with mock.patch.dict(os.environ, {'OPS_STATE_BASE': str(self.out)}), \
                mock.patch.object(sanae_local_ops, '_resolved_server', return_value=server), \
                mock.patch.object(sanae_local_ops.server_traffic, 'recover'), \
                mock.patch.object(sanae_local_ops.backup_status, 'read', return_value=data):
            member = sanae_local_ops.execute_insight('server_world', {'topic': 'backup'}, server, False)
            admin = sanae_local_ops.execute_insight('server_world', {'topic': 'backup'}, server, True)
        self.assertNotIn('空间不够', member)
        self.assertIn('空间不够', admin)
        self.assertIn('backup', s.SERVER_WORLD['function']['parameters']['properties']['topic']['enum'])


def failing(last_success, finished=NOW):
    return {'available': True, 'enabled': True, 'now': finished, 'interval_minutes': 120, 'last_success': last_success,
            'last_bytes': 33636050324, 'task_finished': finished, 'task_exit': 1, 'task_status': 'FAILED',
            'task_message': FULL, 'drive': 'E:', 'free': 89900556288, 'required': 103079215104, 'archives': []}


class BackupWatchTests(unittest.TestCase):
    setUp = monitor_tests.RemoteMonitorTests.setUp

    def queue(self):
        return (self.monitor.load_state('legacy-backup-notify-2105', {'items': []}) or {}).get('items', [])

    def test_alert_once_then_every_twelve_hours_then_recovered(self):
        monitor = self.monitor
        monitor.BACKUP_ALERT_GROUPS = (2105,)
        old = NOW - 80 * 3600
        first = monitor._backup_watch(now=NOW, reader=lambda now: failing(old, now))
        self.assertIn('【备份提醒】', first)
        self.assertIsNone(monitor._backup_watch(now=NOW + 100, reader=lambda now: failing(old, now)))   # checked 5-minutely
        self.assertIsNone(monitor._backup_watch(now=NOW + 3600, reader=lambda now: failing(old, now)))
        self.assertIn('【备份提醒】', monitor._backup_watch(now=NOW + 12 * 3600 + 1, reader=lambda now: failing(old, now)))
        fixed = dict(failing(NOW + 13 * 3600), task_status='COMPLETE', task_exit=0)
        self.assertIn('【备份恢复】', monitor._backup_watch(now=NOW + 13 * 3600 + 400, reader=lambda now: dict(fixed, now=now)))
        self.assertEqual(3, len(self.queue()))
        push = mock.Mock(return_value=True)
        monitor._flush_notices(push)
        self.assertEqual(2105, push.call_args.args[1])

    def test_quiet_when_recent_skipped_or_off(self):
        monitor = self.monitor
        monitor.BACKUP_ALERT_GROUPS = (2105,)
        self.assertIsNone(monitor._backup_watch(now=NOW, reader=lambda now: failing(NOW - 3 * 3600, now)))  # under 6 h
        skipped = dict(failing(NOW - 80 * 3600), task_status='NO_PLAYERS', task_exit=0)
        self.assertIsNone(monitor._backup_watch(now=NOW + 400, reader=lambda now: dict(skipped, now=now)))
        monitor.BACKUP_ALERT_GROUPS = ()
        self.assertIsNone(monitor._backup_watch(now=NOW + 800, reader=mock.Mock(side_effect=AssertionError)))


def sample(i, tps, step=60):
    return {'timestamp': NOW + step * i, 'tps': tps, 'mspt': 1000 / tps, 'tick_parser': 2}


BY_PLAYER = 'Entities by Player\n1. L_Morningstar 1211\n2. Alice 320\n<< Page 1 of 1 >>'


class SustainedLagTests(unittest.TestCase):
    def feed(self, values, state=None):
        out = []
        for i, tps in enumerate(values):
            state, alert = sw.sustained_step(state, sample(i, tps))
            out.append(alert)
        return state, out

    def test_twenty_minutes_under_fourteen(self):
        # 10-29 00:25-00:42: 12.0 and under, never under 10 for five minutes.
        _state, out = self.feed([12.0] * 25)
        hits = [i for i, alert in enumerate(out) if alert]
        self.assertEqual([19], hits)                                  # once, after 20 samples
        alert = out[19]
        self.assertEqual('sustained', alert['kind'])
        text = sw.format_lag_alert('[怀旧]', alert)
        self.assertIn('最近 20 分钟平均 TPS 12.0，低于 14', text)

    def test_healthy_gaps_and_failed_readings(self):
        self.assertFalse(any(self.feed([15.0] * 25)[1]))
        state, out = self.feed([12.0] * 15)
        state, alert = sw.sustained_step(state, {'timestamp': NOW + 16 * 60, 'tps': None, 'tick_parser': 2})
        self.assertEqual([], state['window'])
        gap_state, alert = sw.sustained_step({'window': [[NOW, 12.0, 80.0]]}, sample(5, 12.0))
        self.assertEqual(1, len(gap_state['window']))

    def test_alert_lines_name_the_players(self):
        lines = world_status.lag_alert_lines('', '', entities_by_player=BY_PLAYER,
                                             blocks_by_player=BY_PLAYER.replace('Entities', 'Block Entities'))
        self.assertIn('实体最多的玩家周围：L_Morningstar 1211、Alice 320', lines)
        self.assertIn('方块实体最多的玩家周围：L_Morningstar 1211、Alice 320', lines)


class SustainedWatchTests(unittest.TestCase):
    setUp = monitor_tests.RemoteMonitorTests.setUp

    def test_the_monitor_sends_one_sustained_alert(self):
        monitor = self.monitor
        monitor.LAG_ALERT_GROUPS = (2105,)
        replies = {'statistics entities byPlayer': BY_PLAYER}
        rcon = mock.Mock(side_effect=lambda server, command: replies.get(command, ''))
        with mock.patch.object(monitor, 'query_server', rcon):
            texts = [monitor._lag_watch(sample(i, 12.0)) for i in range(25)]
        sent = [text for text in texts if text]
        self.assertEqual(1, len(sent))
        self.assertIn('最近 20 分钟平均 TPS 12.0', sent[0])
        self.assertIn('实体最多的玩家周围：L_Morningstar 1211', sent[0])


class RoutingTests(unittest.TestCase):
    def test_lag_and_backup_questions_by_name_get_tools(self):
        for text in ('早苗，分析下服务器掉刻原因', '早苗 服务器怎么这么卡', '早苗 服务器卡吗', '早苗，末地实体多不多',
                     '早苗，最近备份了吗', '早苗，世界备份正常吗', '早苗 有多少在运行的水车'):
            self.assertTrue(s._has_server_query_intent(text), text)
            self.assertTrue(bridge._natural_server_operation_candidate(text, False, direct_call=True), text)
        for text in ('早苗，我电脑好卡', '早苗，今天中午吃什么', '早苗 你说备份是什么'):
            self.assertFalse(s._has_server_query_intent(text), text)

    def test_admin_broadcast_requests(self):
        for text in ('[CQ:at,qq=1002] 服里喊话 问问他们干啥呢', '早苗，服里说一下明天维护',
                     '早苗 全服广播一下五分钟后重启', '早苗，服里问一下大家要不要重启'):
            self.assertTrue(s._has_operation_request(text), text)
            self.assertTrue(s._has_console_command_intent(text), text)
            self.assertTrue(bridge._natural_server_operation_candidate(text, True, at_call=True), text)
            self.assertFalse(bridge._natural_server_operation_candidate(text, False, at_call=True), text)
        for text in ('早苗 喊话是什么意思', '早苗 怎么在服里喊话', '早苗 他在服里说什么了',
                     '早苗 FixturePlayer在服里说了啥', '早苗 刚才服里说的那句是谁'):
            self.assertFalse(s._has_operation_request(text), text)


if __name__ == '__main__':
    unittest.main()
