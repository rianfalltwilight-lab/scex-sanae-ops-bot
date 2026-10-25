"""Offline fixtures only: local temporary logs, fake SSH/OneBot/RCON."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import remote_log_reader as reader


class RemoteReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / 'logs' / 'latest.log'
        self.log.parent.mkdir()
        self.server = {'remote_ssh_host': 'test-windows', 'remote_server_dir': self.temp.name}

    def transport(self, argv, **kwargs):
        if os.name != 'nt':
            self.skipTest('Windows log reader requires Windows PowerShell')
        # Execute only the generated read script against this test's temp tree.
        # No SSH process, network connection or Minecraft command is performed.
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertIn('BatchMode=yes', argv)
        script = base64.b64decode(argv[-1]).decode('utf-16-le')
        self.assertIn('[IO.FileAccess]::Read', script)
        return subprocess.run(argv[argv.index('powershell.exe'):], **kwargs)

    def read(self, state=None, **kwargs):
        return reader.read_remote_lines(self.server, state, runner=self.transport, **kwargs)

    def append(self, raw):
        with self.log.open('ab') as stream:
            stream.write(raw)

    def test_first_attach_and_source_change_skip_history(self):
        self.log.write_bytes('旧消息\n'.encode())
        lines, state, ok = self.read({'inode': 123, 'offset': 0})
        self.assertTrue(ok)
        self.assertEqual([], lines)
        self.assertEqual(self.log.stat().st_size, state['offset'])
        self.append('新消息\n'.encode())
        lines, state, ok = self.read(state)
        self.assertEqual(['新消息'], lines)
        self.server['remote_ssh_host'] = 'other-trusted-host'
        self.assertEqual([], self.read(state)[0])

    def test_partial_utf8_is_retained_until_newline_without_duplication(self):
        self.log.write_bytes(b'old\n')
        _, state, _ = self.read()
        message = '玩家: 中文\n'.encode()
        self.append(message[:2])  # inside the first UTF-8 code point
        self.assertEqual(([], state['offset']), (self.read(state)[0], self.read(state)[1]['offset']))
        self.append(message[2:-1])
        lines, middle, ok = self.read(state)
        self.assertTrue(ok)
        self.assertEqual([], lines)
        self.append(b'\n')
        lines, state, ok = self.read(middle)
        self.assertEqual(['玩家: 中文'], lines)
        self.assertEqual([], self.read(state)[0])

    def test_first_attach_mid_record_does_not_forward_old_fragment(self):
        self.log.write_bytes('old partial'.encode())
        _, state, _ = self.read()
        self.append(b' remainder\nfresh\n')
        lines, _, ok = self.read(state)
        self.assertTrue(ok)
        self.assertEqual(['fresh'], lines)

    def test_truncate_and_rewrite_larger_log_are_detected(self):
        self.log.write_bytes(b'original-start\n' + b'x' * 200 + b'\n')
        _, state, _ = self.read()
        self.log.write_bytes(b'new\n')
        lines, state, ok = self.read(state)
        self.assertEqual(['new'], lines)
        # In-place rewrite larger than the committed offset keeps creation time.
        self.log.write_bytes(b'different-start\n' + b'z' * 300 + b'\n')
        lines, _, ok = self.read(state)
        self.assertTrue(ok)
        self.assertEqual('different-start', lines[0])

    def test_rotation_with_new_file_is_read_once(self):
        self.log.write_bytes(b'old\n')
        _, state, _ = self.read()
        self.log.rename(self.log.with_name('old.log'))
        self.log.write_bytes(b'new-startup\n')
        lines, state, ok = self.read(state)
        self.assertTrue(ok)
        self.assertEqual(['new-startup'], lines)
        self.assertEqual([], self.read(state)[0])

    def test_failures_preserve_cursor_and_recovery_reads_unseen_lines(self):
        self.log.write_bytes(b'old\n')
        _, state, _ = self.read()
        self.append(b'new\n')
        for transport in (
                mock.Mock(side_effect=subprocess.TimeoutExpired('test', 20)),
                mock.Mock(return_value=types.SimpleNamespace(returncode=1, stdout=b'')),
                mock.Mock(return_value=types.SimpleNamespace(returncode=0, stdout=b'invalid'))):
            self.assertEqual(([], state, False),
                             reader.read_remote_lines(self.server, state, runner=transport))
        self.assertEqual(['new'], self.read(state)[0])

    def test_oversized_unfinished_line_is_not_forwarded_or_pinned(self):
        self.log.write_bytes(b'old\n')
        _, state, _ = self.read()
        self.append(b'x' * 5000 + b'\nvalid\n')
        lines, state, ok = self.read(state, max_bytes=4096)
        self.assertTrue(ok)
        self.assertEqual([], lines)
        self.assertGreater(state['offset'], 4)
        self.assertEqual(['valid'], self.read(state, max_bytes=4096)[0])


class PosixRemoteReaderTests(RemoteReaderTests):
    def setUp(self):
        super().setUp()
        self.server.update(remote_ssh_host='test-posix', remote_os='posix',
                           remote_python=sys.executable)

    def transport(self, argv, **kwargs):
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertIn('BatchMode=yes', argv)
        command = shlex.split(argv[-1])
        self.assertEqual(sys.executable, command[0])
        self.assertEqual('-c', command[1])
        self.assertIn("open(cfg['path'], 'rb')", command[2])
        return subprocess.run(command, **kwargs)

    def test_migrated_windows_cursor_skips_old_mac_history(self):
        self.log.write_bytes('copied old chat\n'.encode())
        lines, state, ok = self.read({'source': r'test-windows|d:\old\logs\latest.log',
                                      'offset': 0, 'head_size': 0})
        self.assertTrue(ok)
        self.assertEqual([], lines)
        self.append('new chat\n'.encode())
        self.assertEqual(['new chat'], self.read(state)[0])

    def test_posix_source_paths_keep_case(self):
        a = dict(self.server, remote_server_dir='/srv/Legacy')
        b = dict(self.server, remote_server_dir='/srv/legacy')
        self.assertNotEqual(reader.source_id(a), reader.source_id(b))
        self.assertEqual('test-posix|/srv/Legacy/logs/latest.log', reader.source_id(a))


class RemoteMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = {'id': 'legacy', 'server_dir': self.temp.name,
                       'remote_ssh_host': 'test-windows',
                       'remote_server_dir': r'D:\TestServer'}
        self.ops = mock.Mock(root=Path(self.temp.name))
        self.ops.observe_lines.return_value = []
        self.startup = mock.Mock()
        self.coordinator = mock.Mock(side_effect=AssertionError('local probe must not run'))
        self.weekly = mock.Mock(side_effect=AssertionError('local reports must not run'))
        modules = {
            'ops_contract': types.SimpleNamespace(read_incremental_lines=mock.Mock(
                side_effect=AssertionError('local log read must not run'))),
            'ops_telemetry': types.SimpleNamespace(OpsTelemetry=lambda *a, **kw: self.ops),
            'weekly_reports': types.SimpleNamespace(publish_due=self.weekly),
            'startup_notice': types.SimpleNamespace(StartupNotifier=self.startup,
                                                    parse_lifecycle=mock.Mock(return_value=None)),
            'lag_forensics': types.SimpleNamespace(coordinator=self.coordinator),
            'server_registry': types.SimpleNamespace(resolve_server=lambda key: self.server,
                server_prefix=lambda server: '[怀旧]', parse_player_list=mock.Mock(),
                query_server=mock.Mock(side_effect=AssertionError('real RCON forbidden'))),
            'advancement_localization': types.SimpleNamespace(AdvancementLocalizer=mock.Mock()),
        }
        spec = importlib.util.spec_from_file_location('remote_monitor_fixture', ROOT / 'legacy_monitor.py')
        self.monitor = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, {
                'LEGACY_MONITOR_GROUPS': '1001,1003,1001'}):
            spec.loader.exec_module(self.monitor)
        self.monitor.STATE_DIR = self.temp.name

    def test_one_group_failure_retries_without_duplicate_successful_group(self):
        monitor = self.monitor
        calls = []

        def push(text, group):
            calls.append((text, group))
            return group != 1003 or sum(g == group for _, g in calls) > 1

        with mock.patch.object(monitor, 'logwatch', side_effect=[(['message'], []), ([], [])]):
            monitor.scan_once(push)
            monitor.scan_once(push)
        self.assertEqual([1001, 1003, 1003], [g for _, g in calls])
        self.assertEqual([], monitor.load_state('legacy-monitor-pending')['items'])
        self.assertEqual([], monitor.load_state('legacy-monitor-pending-1003')['items'])

    def test_remote_mode_skips_local_collectors_and_samples_rcon_once(self):
        monitor = self.monitor
        push = mock.Mock(return_value=True)
        with mock.patch.object(monitor, 'push_group', push), \
                mock.patch.object(monitor, 'logwatch', return_value=([], [])):
            monitor.scan_once(push)
            monitor.scan_once(push)
        self.assertIsNone(monitor.STARTUP)
        self.startup.assert_not_called()
        self.coordinator.assert_not_called()
        self.weekly.assert_not_called()
        self.ops.periodic_maintenance.assert_not_called()
        self.ops.sample_runtime.assert_called_once()
        push.assert_not_called()
        self.assertEqual([], monitor.crashwatch())

    def test_failed_remote_read_does_not_update_cursor_or_read_local_log(self):
        monitor = self.monitor
        old = {'source': 'test-windows|fixture', 'offset': 123}
        monitor.save_state('legacy-logwatch', old)
        with mock.patch.object(monitor, 'read_remote_lines', return_value=([], old, False)), \
                mock.patch.object(monitor.os.path, 'isfile', side_effect=AssertionError('local file access')):
            self.assertEqual(([], []), monitor.logwatch(include_ops=True))
        self.assertEqual(old, monitor.load_state('legacy-logwatch'))
        self.ops.observe_lines.assert_not_called()

    def test_chat_filter_retains_player_text_and_rejects_echoes(self):
        prefix = '[Server thread/INFO] [net.minecraft.server.MinecraftServer/]: '
        monitor = self.monitor
        for line in ('[Server] [QQ群] sender: msg', '<Server> msg', '<player> [QQ群] msg',
                     '<player> [Server] msg', '<player> <Server> msg'):
            self.assertIsNone(monitor._player_chat_message(prefix + line))
        self.assertEqual('[怀旧] <player> ［CQ:at,qq=all] 中文',
            monitor._player_chat_message(prefix + '[Not Secure] <player> [CQ:at,qq=all] 中文'))

    def test_fresh_done_notice_is_deduplicated_without_native_probe(self):
        monitor = self.monitor
        ready = {'type': 'ready', 'timestamp': 1000, 'internal_text': '12.3'}
        with mock.patch.object(monitor, 'parse_lifecycle', return_value=ready), \
                mock.patch.object(monitor.time, 'time', return_value=1100):
            self.assertIn('12.3s', monitor._remote_startup_notice('Done fixture'))
            self.assertIsNone(monitor._remote_startup_notice('Done fixture'))
        with mock.patch.object(monitor, 'parse_lifecycle', return_value=ready), \
                mock.patch.object(monitor.time, 'time', return_value=2000):
            self.assertIsNone(monitor._remote_startup_notice('old Done fixture'))


if __name__ == '__main__':
    unittest.main()
