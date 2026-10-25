"""Real cross-process locking of temporary files; no services or network."""
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from weekly_reports import locked


ROOT = Path(__file__).resolve().parents[1]
CHILD = r'''
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from weekly_reports import locked
lock_path, ready, acquired = map(Path, sys.argv[2:])
ready.write_text('ready', encoding='ascii')
with locked(lock_path):
    acquired.write_text('acquired', encoding='ascii')
'''


class WeeklyReportLockTests(unittest.TestCase):
    def exercise_lock(self, raise_in_body=False):
        with tempfile.TemporaryDirectory(prefix='weekly-lock-') as tmp:
            root = Path(tmp)
            lock_path, ready, acquired = (root / name for name in ('report.lock', 'ready', 'acquired'))
            child = None
            try:
                try:
                    with locked(lock_path):
                        child = subprocess.Popen(
                            [sys.executable, '-X', 'utf8', '-c', CHILD, str(ROOT),
                             str(lock_path), str(ready), str(acquired)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                        deadline = time.monotonic() + 5
                        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertTrue(ready.exists(), 'Child did not reach the lock attempt')
                        with self.assertRaises(subprocess.TimeoutExpired):
                            child.wait(timeout=0.2)
                        self.assertFalse(acquired.exists(), 'Another process entered the locked section')
                        if raise_in_body:
                            raise ValueError('synthetic report error')
                except ValueError:
                    if not raise_in_body:
                        raise
                _, stderr = child.communicate(timeout=5)
                self.assertEqual(0, child.returncode, stderr.decode('utf-8', errors='replace'))
                self.assertTrue(acquired.exists(), 'Lock was not released')
            finally:
                if child is not None:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)
                    child.stderr.close()

    def test_other_process_waits_until_context_exits(self):
        self.exercise_lock()

    def test_exception_releases_lock_for_waiting_process(self):
        self.exercise_lock(raise_in_body=True)


if __name__ == '__main__':
    unittest.main()
