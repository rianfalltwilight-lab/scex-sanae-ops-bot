"""Synthetic local archives and receipts; never uses a configured backup share."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import backup_local


class BackupLocalTests(unittest.TestCase):
    def test_retention_is_disabled_by_default_and_requires_owned_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = [f'world_2026-01-0{index}_00-00-00.zip' for index in (1, 2, 3)]
            for name in names:
                archive = root / name
                archive.write_bytes(b'synthetic archive')
                archive.with_suffix('.zip.sha256.json').write_text(json.dumps({
                    'state': 'success', 'file': name, 'remoteSize': archive.stat().st_size,
                    'sha256': backup_local.digest(archive),
                }), encoding='utf-8')
            unowned = root / 'world_2025-01-01_00-00-00.zip'
            unowned.write_bytes(b'not created by this pipeline')
            with mock.patch.dict(os.environ, {'SANAE_BACKUP_KEEP_COMPLETE': '0'}):
                backup_local.prune(root)
            self.assertTrue(all((root / name).exists() for name in names))
            with mock.patch.dict(os.environ, {'SANAE_BACKUP_KEEP_COMPLETE': '2'}):
                backup_local.prune(root)
            self.assertFalse((root / names[0]).exists())
            self.assertTrue(all((root / name).exists() for name in names[1:]))
            self.assertTrue(unowned.exists())

    def test_sync_preserves_different_existing_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = root / 'source', root / 'target'
            source.mkdir()
            target.mkdir()
            (source / 'world_fixture.zip').write_bytes(b'new snapshot')
            (target / 'world_fixture.zip').write_bytes(b'old snapshot')
            capacity = mock.Mock(free=100 * 1024**3)
            with mock.patch.dict(os.environ, {
                    'SANAE_BACKUP_LOCAL_DIR': str(source), 'SANAE_BACKUP_NAS_DIR': str(target)}), \
                    mock.patch.object(backup_local.shutil, 'disk_usage', return_value=capacity):
                with self.assertRaises(FileExistsError):
                    backup_local.sync('world_fixture.zip')
            self.assertEqual(b'old snapshot', (target / 'world_fixture.zip').read_bytes())


if __name__ == '__main__':
    unittest.main()
