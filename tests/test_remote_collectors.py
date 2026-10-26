"""Remote crash watch and mods mirror, run against local temp trees.

The generated remote scripts run with this interpreter instead of over SSH; no
network connection, remote host or Minecraft server is involved.
"""
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import remote_crash_reader as crashes  # noqa: E402
import remote_mods_mirror as mirror  # noqa: E402


def local_command(argv):
    """Turn the SSH argv into the same python -c command run locally."""
    assert 'BatchMode=yes' in argv and 'StrictHostKeyChecking=yes' in argv
    command = shlex.split(argv[-1])
    return [sys.executable, command[1], command[2]]


def run_local(argv, **kwargs):
    kwargs.pop('creationflags', None)
    return subprocess.run(local_command(argv), **kwargs)


class CrashReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'crash-reports').mkdir()
        self.server = {'remote_ssh_host': 'remote-test', 'remote_os': 'posix',
                       'remote_server_dir': str(self.root), 'remote_python': 'python3'}

    def crash(self, name, age, body):
        path = self.root / 'crash-reports' / name
        path.write_text(body, encoding='utf-8')
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))

    def test_first_attach_reports_last_week_as_history_then_only_new_files(self):
        self.crash('crash-old.txt', 9 * 86400, 'Description: old\n')
        self.crash('crash-a.txt', 3600, '---- Minecraft Crash Report ----\nDescription: Ticking block entity\n'
                   'java.lang.NullPointerException: boom\n\tat x\n'
                   '-- Block entity being ticked --\nBlock: Block{create:schematicannon}\n'
                   'Block location: World: (12906,167,-8228), Section: (at 10,7,12)\n')
        jvm = self.root / 'hs_err_pid735.log'
        jvm.write_text('#\n# A fatal error has been detected by the Java Runtime Environment:\n#\n'
                       '#  SIGBUS (0xa) at pc=0x1, pid=735\n#\n# Problematic frame:\n'
                       '# J 599784 c2 java.util.AbstractCollection.removeAll\n', encoding='utf-8')
        reports, state, ok = crashes.read_remote_crashes(self.server, None, runner=run_local)
        self.assertTrue(ok)
        by_name = {r['name']: r for r in reports}
        self.assertEqual({'crash-a.txt', 'hs_err_pid735.log'}, set(by_name))  # 9-day-old one skipped
        self.assertTrue(all(r['historic'] for r in reports))
        a = by_name['crash-a.txt']
        self.assertEqual(('Ticking block entity', 'Block: Block{create:schematicannon}', '12906,167,-8228'),
                         (a['description'], a['subject'], a['location']))
        self.assertIn('NullPointerException', a['cause'])
        self.assertIn('SIGBUS', by_name['hs_err_pid735.log']['description'])
        self.assertIn('removeAll', by_name['hs_err_pid735.log']['cause'])
        self.assertIn('crash-old.txt', state['seen'])
        line = crashes.render('[怀旧]', a)
        self.assertIn('崩溃', line)
        self.assertIn('位置 12906,167,-8228', line)
        self.assertEqual([], crashes.read_remote_crashes(self.server, state, runner=run_local)[0])
        self.crash('crash-b.txt', 0, 'Description: Watching Server\njava.lang.Error: hang\n')
        new, state, ok = crashes.read_remote_crashes(self.server, state, runner=run_local)
        self.assertEqual(['crash-b.txt'], [r['name'] for r in new])
        self.assertFalse(new[0]['historic'])

    def test_failures_keep_the_cursor(self):
        state = {'source': crashes.source_id(self.server), 'since': 1, 'seen': {'x': 1}}
        for runner in (mock.Mock(return_value=mock.Mock(returncode=255, stdout=b'')),
                       mock.Mock(side_effect=subprocess.TimeoutExpired('ssh', 30)),
                       mock.Mock(return_value=mock.Mock(returncode=0, stdout=b'not json'))):
            self.assertEqual(([], state, False), crashes.read_remote_crashes(self.server, state, runner=runner))
        self.assertFalse(crashes.read_remote_crashes(dict(self.server, remote_os='windows'), state)[2])


class FakePopen:
    """Runs the generated tar script locally and exposes its stdout like Popen."""

    def __init__(self, argv, **kwargs):
        kwargs.pop('creationflags', None)
        self._process = subprocess.Popen(local_command(argv), stdout=subprocess.PIPE,
                                         stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.stdout = self._process.stdout

    def kill(self):
        self._process.kill()

    def wait(self, timeout=None):
        return self._process.wait(timeout)


class ModsMirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.remote = base / 'remote'
        (self.remote / 'mods').mkdir(parents=True)
        self.local = base / 'mirror'
        self.server = {'remote_ssh_host': 'remote-test', 'remote_os': 'posix',
                       'remote_server_dir': str(self.remote), 'remote_python': 'python3'}

    def jar(self, name, data):
        path = self.remote / 'mods' / name
        path.write_bytes(data)
        return path

    def sync(self):
        return mirror.sync(self.server, self.local, runner=run_local, popen=FakePopen)

    def test_copies_changed_jars_and_removes_vanished_ones(self):
        self.jar('[AE2多配方兼容] polyeng-0.4.1.jar', b'a' * 10)
        self.jar('thaumcraft-port.277.jar', b'b' * 20)
        (self.remote / 'mods' / 'notes.txt').write_text('ignored')
        result = self.sync()
        self.assertTrue(result['complete'])
        self.assertEqual(2, len(result['fetched']))
        mods = self.local / 'mods'
        self.assertEqual(b'a' * 10, (mods / '[AE2多配方兼容] polyeng-0.4.1.jar').read_bytes())
        self.assertFalse((mods / 'notes.txt').exists())
        remote_mtime = int((self.remote / 'mods' / 'thaumcraft-port.277.jar').stat().st_mtime)
        self.assertEqual(remote_mtime, int((mods / 'thaumcraft-port.277.jar').stat().st_mtime))
        self.assertFalse(self.sync()['changed'])                     # nothing to do second time
        (self.remote / 'mods' / 'thaumcraft-port.277.jar').unlink()
        self.jar('thaumcraft-port.278.jar', b'c' * 30)
        result = self.sync()
        self.assertEqual(['thaumcraft-port.278.jar'], result['fetched'])
        self.assertEqual(['thaumcraft-port.277.jar'], result['deleted'])
        self.assertEqual({'[AE2多配方兼容] polyeng-0.4.1.jar', 'thaumcraft-port.278.jar'},
                         {p.name for p in mods.glob('*.jar')})

    def test_server_properties_is_mirrored_for_rule_queries(self):
        (self.remote / 'server.properties').write_text('pvp=false\nwhite-list=true\n', encoding='utf-8')
        self.assertEqual('updated', self.sync()['properties'])
        self.assertEqual('pvp=false\nwhite-list=true\n',
                         (self.local / 'server.properties').read_text(encoding='utf-8'))
        self.assertEqual('same', self.sync()['properties'])
        (self.remote / 'server.properties').unlink()
        self.assertEqual('missing', self.sync()['properties'])

    def test_plan_skips_names_windows_cannot_store(self):
        fetch, delete, skipped = mirror.plan({'ok.jar': (1, 1), 'bad:name.jar': (1, 1)},
                                             {'old.jar': (1, 1), 'ok.jar': (1, 1)})
        self.assertEqual(([], ['old.jar'], ['bad:name.jar']), (fetch, delete, skipped))

    def test_listing_failure_raises_before_touching_the_mirror(self):
        runner = mock.Mock(return_value=mock.Mock(returncode=255, stdout=b''))
        with self.assertRaises(OSError):
            mirror.sync(self.server, self.local, runner=runner, popen=FakePopen)


if __name__ == '__main__':
    unittest.main()
