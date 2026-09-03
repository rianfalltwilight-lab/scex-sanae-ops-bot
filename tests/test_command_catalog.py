#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path

import command_catalog as catalog


HELP = """/give <targets> <item> [<count>]
/buildinggadgets2 redprints (list|remove|give)
/bluemap [reload|maps|version]
/sbp (list|give|removeNonPlayer|template)
/sophisticatedbackpacks -> sbp
//set [<args>]
"""


class CommandCatalogTests(unittest.TestCase):
    def test_scan_search_validate_and_worldedit_slashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'catalog.json'
            server = {'id': 'legacy', 'name': 'Legacy', 'prefix': '[怀旧]'}
            row = catalog.refresh_server(server, lambda _server, _cmd: HELP, path)
            self.assertEqual(6, row['count'])
            self.assertGreaterEqual(row['extended_count'], 5)
            self.assertIn('/buildinggadgets2 redprints',
                          catalog.search('legacy', '蓝图', path=path))
            self.assertIn('需指定在线玩家上下文',
                          catalog.search('legacy', '蓝图', path=path))
            self.assertTrue(catalog.requires_player_context(
                'buildinggadgets2 redprints list'))
            ok, usage, error = catalog.validate(
                'legacy', 'buildinggadgets2 redprints list', path)
            self.assertTrue(ok, error)
            self.assertIn('redprints', usage)
            self.assertEqual('//set stone', catalog.normalize_console_command('//set stone'))
            self.assertTrue(catalog.validate('legacy', '//set stone', path)[0])
            self.assertFalse(catalog.validate('legacy', 'not_registered foo', path)[0])

    def test_multiline_command_is_rejected(self):
        with self.assertRaises(ValueError):
            catalog.normalize_console_command('say ok\nstop')


if __name__ == '__main__':
    unittest.main()
