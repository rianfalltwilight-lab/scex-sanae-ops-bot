"""Weekly report for a remote server: only RCON samples and recorded events count.

Offline fixtures in a temp tree; nothing is sent and no server is queried.
"""
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

os.environ["SCE_BOT_TEST_MODE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ops_commands  # noqa: E402
import weekly_reports as w  # noqa: E402

NOW = datetime(2026, 10, 26, 20, 0, tzinfo=w.TZ).timestamp()
HOUR = 3600


def write_rows(path, rows):
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows),
                    encoding='utf-8')


class RemoteWeeklyReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'ops' / 'legacy'
        self.root.mkdir(parents=True)
        since = NOW - w.WEEK
        # Samples every 10 minutes, except for a 3-day collector outage.
        stamps = [since + 600 * i for i in range(1, 1008)
                  if not (since + 2 * 86400 <= since + 600 * i <= since + 5 * 86400)]
        write_rows(self.root / 'perf-samples.jsonl', [
            {'timestamp': t, 'server': 'legacy', 'players': 4, 'max_players': 20,
             'tps': 20.0, 'mspt': 30.0, 'errors': [], 'tick_parser': 2} for t in stamps])
        write_rows(self.root / 'events.jsonl', [
            {'timestamp': since + HOUR, 'type': 'join', 'text': 'a', 'details': {'player': 'Alice'}},
            {'timestamp': since + 2 * HOUR, 'type': 'join', 'text': 'a', 'details': {'player': 'alice'}},
            {'timestamp': since + 3 * HOUR, 'type': 'join', 'text': 'b', 'details': {'player': 'Bob'}},
            {'timestamp': since + 4 * HOUR, 'type': 'minecraft_event', 'text': 'x', 'details': {}},
            {'timestamp': since - HOUR, 'type': 'join', 'text': 'old', 'details': {'player': 'Old'}},
        ])
        write_rows(self.root / 'error-fingerprints.jsonl', [
            {'timestamp': since + HOUR, 'action': 'first'},
            {'timestamp': since + HOUR, 'action': 'aggregate'}])
        # Left over from the local era; a remote report must not quote it.
        (self.root / 'backup-verify.json').write_text(json.dumps(
            {'ok': True, 'generated_at': since - 86400}), encoding='utf-8')
        (self.root / 'runtime.json').write_text(json.dumps(
            {'timestamp': NOW - 30, 'players': 5, 'tps': 20.0, 'mspt': 31.0, 'tick_parser': 2}),
            encoding='utf-8')

    def telemetry(self, remote=True):
        return types.SimpleNamespace(root=self.root, server_id='legacy', prefix='[怀旧]',
                                     server_dir=str(Path(self.temp.name) / 'no-such-server'),
                                     remote=remote)

    def test_remote_report_counts_joins_and_names_the_gap(self):
        data = w.build_report(self.telemetry(), now=NOW)
        self.assertTrue(data['remote'])
        self.assertEqual(3, data['logs']['join'])
        self.assertEqual(2, data['logs']['join_players'])
        for key in ('backup_ok', 'backup_checked_at', 'free_gib', 'backup_verify_batches'):
            self.assertIsNone(data[key], key)
        self.assertEqual(1, data['fingerprint_alert_records'])
        self.assertEqual(1, len(data['sample_gaps']))
        gap_start, gap_end = data['sample_gaps'][0]
        self.assertGreater(gap_end - gap_start, 2.9 * 86400)
        self.assertTrue(any(issue.startswith('采样中断') for issue in data['issues']))
        # No local log scan, so no cache file is produced.
        self.assertFalse((self.root / 'weekly-log-cache.json').exists())

    def test_remote_format_shows_only_collected_figures(self):
        data = w.build_report(self.telemetry(), now=NOW)
        short = w.format_report(data)
        self.assertIn('进服3次（2人）', short)
        self.assertIn('实时 在线5', short)
        self.assertIn('远端主机', short)
        # The caveat line names what is missing; no figure line for it remains.
        for absent in ('磁盘 剩', '备份核验', '启停 ', 'ERROR/FATAL'):
            self.assertNotIn(absent, short)
        self.assertIn('详情 !周报 详情', short)
        detail = w.format_report(data, details=True)
        self.assertIn('统计窗口 10-19 20:00～10-26 20:00', detail)
        self.assertIn('数据说明：采样中断', detail)
        self.assertNotIn('最近备份核验', detail)

    def test_no_events_means_unrecorded_not_zero(self):
        (self.root / 'events.jsonl').unlink()
        data = w.build_report(self.telemetry(), now=NOW)
        self.assertIsNone(data['logs']['join'])
        self.assertIn('进服未记录', w.format_report(data))

    def test_local_report_keeps_its_layout(self):
        data = w.build_report(self.telemetry(remote=False), now=NOW)
        self.assertFalse(data['remote'])
        self.assertEqual([], data['sample_gaps'])
        text = w.format_report(data)
        self.assertIn('磁盘 剩', text)
        self.assertIn('启停 启动成功', text)

    def test_sample_gaps(self):
        rows = [{'timestamp': t} for t in (100, 200, 5000)]
        self.assertEqual([[200, 5000], [5000, 9000]], w.sample_gaps(rows, 0, 9000, threshold=1000))
        self.assertEqual([[0, 9000]], w.sample_gaps([], 0, 9000, threshold=1000))

    def test_remote_crash_events_are_counted_by_report_name(self):
        since = NOW - w.WEEK
        with (self.root / 'events.jsonl').open('a', encoding='utf-8') as stream:
            for name in ('crash-a.txt', 'crash-a.txt', 'hs_err_pid1.log'):
                stream.write(json.dumps({'timestamp': since + 5 * HOUR, 'type': 'crash', 'text': 'x',
                                         'details': {'report': name, 'source': 'remote'}}) + '\n')
        data = w.build_report(self.telemetry(), now=NOW)
        self.assertEqual(2, data['crash_reports'])
        self.assertIn('可靠 崩溃报告2份', w.format_report(data))


class MultiGroupDeliveryTests(unittest.TestCase):
    CONFIG = {'enabled': True, 'server': 'legacy', 'timezone': 'Asia/Shanghai',
              'destination': 'players', 'weekday': 0, 'hour': 20, 'minute': 0, 'catch_up_hours': 24}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.telemetry = mock.Mock(root=self.root, server_id='legacy')
        self.telemetry.weekly_report.return_value = {'issues': [], 'logs': {}}
        self.telemetry.format_weekly.return_value = 'weekly text'
        self.format = self.enterContext(mock.patch.object(w, 'format_report', return_value='detail'))

    def publish(self, push, groups=(1001, 2103), now=NOW + 30):
        return w.publish_due(self.telemetry, push, groups, now=now, config=self.CONFIG)

    def test_each_group_gets_the_report_once(self):
        push = mock.Mock(return_value=True)
        self.assertEqual('sent', self.publish(push))
        self.assertEqual([mock.call('weekly text', 1001), mock.call('weekly text', 2103)],
                         push.call_args_list)
        self.telemetry.weekly_report.assert_called_once()
        self.assertEqual('sent', self.publish(push, now=NOW + 3600))
        self.assertEqual(2, push.call_count)
        self.assertEqual('not-due', self.publish(push, now=NOW - 60))

    def test_one_group_failing_does_not_resend_to_the_other(self):
        push = mock.Mock(side_effect=lambda text, group: group == 1001)
        self.assertEqual('sent,uncertain', self.publish(push))
        self.assertEqual('sent,uncertain', self.publish(push, now=NOW + 60))
        self.assertEqual(2, push.call_count)                   # uncertain is never replayed

    def test_legacy_single_group_entry_still_counts(self):
        due = datetime.fromtimestamp(NOW, w.TZ).isoformat()
        (self.root / 'weekly-delivery.json').write_text(json.dumps({'deliveries': {
            due: {'status': 'sent', 'group_id': 1001}}}), encoding='utf-8')
        push = mock.Mock(return_value=True)
        self.assertEqual('sent', self.publish(push))
        self.assertEqual([mock.call('weekly text', 2103)], push.call_args_list)

    def test_single_group_argument_still_works(self):
        push = mock.Mock(return_value=True)
        self.assertEqual('sent', self.publish(push, groups=1001))
        push.assert_called_once_with('weekly text', 1001)


class OpsCommandRemoteFlagTests(unittest.TestCase):
    def test_weekly_command_builds_a_remote_aware_telemetry(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(ops_commands, 'STATE_BASE', Path(temp)), \
                mock.patch.object(ops_commands, 'server_prefix', return_value='[怀旧]'):
            remote = ops_commands._telemetry({'id': 'legacy', 'server_dir': temp,
                                              'remote_ssh_host': 'remote-test'})
            local = ops_commands._telemetry({'id': 'nast', 'server_dir': temp})
        self.assertTrue(remote.remote)
        self.assertFalse(local.remote)

    def test_lag_and_backup_checks_say_what_a_remote_server_allows(self):
        server = {'id': 'legacy', 'server_dir': '.', 'remote_ssh_host': 'remote-test'}
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(ops_commands, 'STATE_BASE', Path(temp)), \
                mock.patch.object(ops_commands, 'server_prefix', return_value='[怀旧]'), \
                mock.patch.object(ops_commands, '_selected', return_value=(server, '')):
            lag = ops_commands.dispatch_ops_command('!卡顿取证', 1, 2, True)
            backup = ops_commands.dispatch_ops_command('!验备份', 1, 2, True)
        self.assertIn('不能做卡顿取证', lag.text)
        self.assertIn('无法校验', backup.text)

    def test_backup_command_refuses_for_a_remote_server(self):
        import rcon_ops
        with mock.patch.object(rcon_ops, '_cmd_backup', side_effect=AssertionError('local pipeline')), \
                mock.patch.object(rcon_ops, 'active_server', return_value={'remote_ssh_host': 'remote-test'}):
            self.assertIn('远端主机', rcon_ops.dispatch('backup', 'n', 'u', True))

    def test_ai_mod_list_reads_the_mirrored_index_for_a_remote_server(self):
        import sanae_local_ops
        server = {'id': 'legacy', 'prefix': '[怀旧]', 'server_dir': 'missing', 'remote_ssh_host': 'remote-test'}
        with tempfile.TemporaryDirectory() as temp:
            index = Path(temp) / 'legacy'
            index.mkdir()
            (index / 'mod-inventory.json').write_text(json.dumps({
                'schema': 2, 'generated_at': NOW, 'count': 1, 'prefix': '[怀旧]',
                'mods': [{'id': 'thaumcraft', 'name': 'Thaumcraft', 'file': 't.jar',
                          'version': 'port.277', 'category': 'content'}]}), encoding='utf-8')
            with mock.patch.dict(os.environ, {'OPS_STATE_BASE': temp}), \
                    mock.patch.object(sanae_local_ops.server_registry, 'resolve_server', return_value=server), \
                    mock.patch.object(sanae_local_ops.remote_server_reader, 'tail_log',
                                      return_value=['[12:00:00] [Server thread/INFO]: Done']) as tail:
                text = sanae_local_ops.execute_local_read('list_mods', {}, {'id': 'legacy'}, False)
                log = sanae_local_ops.execute_local_read('read_server_log', {}, {'id': 'legacy'}, True)
                denied = sanae_local_ops.execute_local_read('read_server_log', {}, {'id': 'legacy'}, False)
        self.assertIn('已安装模组：1 个', text)
        self.assertIn('同步自远端主机', text)
        self.assertIn('远端最新日志末尾 1 行', log)
        self.assertIn('Done', log)
        tail.assert_called_once_with(server, 150)
        self.assertTrue(denied.startswith('拒绝'))


if __name__ == '__main__':
    unittest.main()
