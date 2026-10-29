"""Migration routing tests; no real backup, RCON or group message."""
import importlib
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import legacy_backup_control as backup
import legacy_mslx_control as control
import rcon_ops as ops


class LegacyMacOpsTests(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(control, "FLEET", "fixture-control.ps1"),
                   mock.patch.object(backup, "PIPELINE", "fixture-backup.py")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_unconfigured_adapters_do_not_execute_external_commands(self):
        with mock.patch.object(backup, 'PIPELINE', ''), \
                mock.patch.object(control, 'FLEET', ''), \
                mock.patch.object(backup.subprocess, 'run') as run, \
                mock.patch.object(backup.subprocess, 'Popen') as popen:
            self.assertEqual({'enabled': False, 'state': 'disabled'}, backup.read_status())
            self.assertIn('未执行', backup.start())
            self.assertIn('未执行', control.start('restart', '123'))
            run.assert_not_called()
            popen.assert_not_called()

    def test_disabled_or_busy_backup_does_not_launch(self):
        for state in ({'enabled': False, 'state': 'disabled'},
                      {'enabled': True, 'state': 'busy'}):
            with mock.patch.object(backup, 'read_status', return_value=state), \
                    mock.patch.object(backup.subprocess, 'Popen') as popen:
                text = backup.start()
                popen.assert_not_called()
                self.assertNotIn('备份完成', text)

    def test_start_returns_acceptance_and_closes_log(self):
        log = mock.mock_open()
        with mock.patch.object(backup, 'read_status', return_value={'enabled': True, 'state': 'idle'}), \
                mock.patch.object(Path, 'mkdir'), mock.patch.object(Path, 'open', log), \
                mock.patch.object(backup.subprocess, 'Popen') as popen:
            text = backup.start()
        self.assertIn('已提交', text)
        self.assertNotIn('备份完成', text)
        args, kwargs = popen.call_args
        self.assertEqual('--run', args[0][-1])
        self.assertEqual(backup.NO_WINDOW, kwargs['creationflags'])
        log().__exit__.assert_called_once()

    def test_disabled_zero_exit_and_busy_are_not_success(self):
        self.assertIn('未执行', backup.completion_text(0, {'state': 'disabled'}))
        self.assertIn('没有重复', backup.completion_text(3, {'state': 'busy'}))
        self.assertIn('备份完成', backup.completion_text(0, {'state': 'complete', 'bytes': 1048576}))

    def test_preflight_failure_uses_current_process_error(self):
        result = types.SimpleNamespace(returncode=1, stdout=b'',
            stderr=json.dumps({'state': 'failed', 'message': 'Disk budget insufficient'}).encode())
        with mock.patch.object(backup.subprocess, 'run', return_value=result), \
                mock.patch.object(backup, 'read_status', side_effect=AssertionError('old state must not be read')), \
                mock.patch.object(backup, 'notify') as notify:
            backup.run()
        self.assertIn('Disk budget insufficient', notify.call_args.args[0])
        self.assertIn('备份失败', notify.call_args.args[0])

    def test_legacy_backup_routes_to_new_pipeline_only(self):
        with mock.patch.object(backup, 'start', return_value='accepted') as start, \
                mock.patch.object(ops.subprocess, 'Popen') as old_start:
            self.assertEqual('accepted', ops.dispatch('backup', 'n', 'u', True, server={'id': 'legacy'}))
        start.assert_called_once()
        old_start.assert_not_called()

    def test_legacy_backup_keeps_admin_gate(self):
        with mock.patch.object(backup, 'start') as start:
            self.assertIsNone(ops.dispatch('backup', 'n', 'u', False, server={'id': 'legacy'}))
        start.assert_not_called()

    def test_status_routes_without_starting_job(self):
        with mock.patch.object(backup, 'status_text', return_value='status') as status, \
                mock.patch.object(backup, 'start') as start:
            self.assertEqual('status', ops.dispatch('backup 状态', 'n', 'u', True, server={'id': 'legacy'}))
        status.assert_called_once()
        start.assert_not_called()

    def test_other_server_restart_keeps_existing_behavior(self):
        with mock.patch.object(ops, '_run') as run, \
                mock.patch.object(control, 'start') as start, \
                mock.patch.object(ops, 'active_server', return_value={'id': 'other'}):
            self.assertIn('MCSManager', ops._execute_restart())
        run.assert_called_once_with('stop')
        start.assert_not_called()

    def test_legacy_controls_require_admin_and_original_user_confirmation(self):
        for action in ('restart', 'stop'):
            with self.subTest(action=action), mock.patch.object(ops, '_audit'), \
                    mock.patch.object(control, 'start', return_value='accepted') as start, \
                    mock.patch.object(ops, '_run') as rcon:
                ops._PENDING_RISK.clear()
                server = {'id': 'legacy'}
                self.assertIsNone(ops.dispatch(action, 'n', '123', False, server=server))
                self.assertIn('高危确认', ops.dispatch(action, 'n', '123', True, server=server))
                start.assert_not_called()
                code = next(iter(ops._PENDING_RISK))
                self.assertIn('原发起人', ops.dispatch('确认 ' + code, 'n', '456', True, server=server))
                self.assertIn('不一致', ops.dispatch('确认 ' + code, 'n', '123', True, server={'id': 'other'}))
                start.assert_not_called()
                self.assertEqual('accepted', ops.dispatch('确认 ' + code, 'n', '123', True, server=server))
                start.assert_called_once_with(action, '123')
                rcon.assert_not_called()

    def test_control_arguments_use_audited_explicit_mslx_action(self):
        for action in ('restart', 'stop'):
            args = control.control_args(action, '123')
            script=base64.b64decode(args[args.index('-EncodedCommand') + 1]).decode('utf-16-le')
            self.assertIn("& '" + control.FLEET + "'", script)
            self.assertIn('-Target Legacy', script)
            self.assertIn("-Action '" + action + "'", script)
            self.assertIn('QQ 123', script)
            self.assertIn('-ApprovePlayerImpact', script)
            self.assertIn('-Confirm:$false', script)
            self.assertIn('-NoticeSeconds 60', script)

    def test_control_start_returns_acceptance_and_uses_background_process(self):
        log = mock.mock_open()
        with mock.patch.object(Path, 'mkdir'), mock.patch.object(Path, 'open', log), \
                mock.patch.object(control.subprocess, 'Popen') as popen:
            text = control.start('restart', '123')
        self.assertIn('已提交', text)
        self.assertNotIn('已完成', text)
        args, kwargs = popen.call_args
        self.assertEqual(['--run', 'restart', '123'], args[0][-3:])
        self.assertEqual(control.NO_WINDOW, kwargs['creationflags'])
        log().__exit__.assert_called_once()

    def test_control_worker_uses_exit_status_and_only_mock_notification(self):
        for returncode in (0, 1):
            process = mock.Mock()
            process.wait.return_value = returncode
            with mock.patch.object(control.subprocess, 'Popen', return_value=process) as popen, \
                    mock.patch.object(control, 'server_state', return_value='fixture server status') as state, \
                    mock.patch.object(control, 'notify') as notify:
                control.run('restart', '123')
            self.assertEqual(control.control_args('restart', '123'), popen.call_args.args[0])
            process.wait.assert_called_once_with(timeout=control.RUN_TIMEOUT_SECONDS)
            self.assertEqual(bool(returncode), state.called)
            self.assertIn('正在加载' if returncode == 0 else '失败', notify.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
