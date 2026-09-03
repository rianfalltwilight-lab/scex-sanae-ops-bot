#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('SCE_BOT_TEST_MODE', '1')

import onebot_utf8
import server_registry


def load_path(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_contract_stub():
    contract = types.ModuleType('ops_contract')
    contract.append_jsonl = lambda *_args, **_kwargs: None
    contract.event = lambda kind, data, raw='': {'kind': kind, 'data': data, 'raw': raw}
    contract.normalize_log_fingerprint = lambda raw: str(raw)
    contract.onebot_success = lambda raw: onebot_utf8.onebot_success_utf8(raw)
    contract.read_incremental_lines = lambda _path, state: ([], state, True)
    sys.modules['ops_contract'] = contract


class RegistryAndFanoutTests(unittest.TestCase):
    def test_public_example_has_only_scex_legacy(self):
        source = ROOT / 'examples' / 'servers.example.json'
        data = json.loads(source.read_text(encoding='utf-8'))
        self.assertEqual(['legacy'], [row['id'] for row in data['servers']])
        self.assertEqual(['[怀旧]'], [row['prefix'] for row in data['servers']])

    def test_fanout_is_best_effort_and_keeps_group_source(self):
        servers = [{'id': 'alpha'}, {'id': 'legacy'}]
        calls = []

        def fake_query(server, command):
            calls.append((server['id'], command))
            if server['id'] == 'alpha':
                raise ConnectionError('offline')
            return 'ok'

        results = server_registry.fanout_group_message(
            '测试昵称', '你好\n服务器', servers=servers, query_fn=fake_query)
        self.assertEqual(['alpha', 'legacy'], [row[0] for row in calls])
        self.assertEqual('say [QQ群] 测试昵称: 你好 服务器', calls[1][1])
        self.assertEqual([False, True], [row['ok'] for row in results])


class Utf8Tests(unittest.TestCase):
    def test_utf8_json_and_bom(self):
        payload = onebot_utf8.json_bytes({'message': '维护公告：中文正常'})
        self.assertIn('维护公告'.encode('utf-8'), payload)
        self.assertNotIn(b'\\u7ef4', payload)
        self.assertEqual('公告', onebot_utf8.utf8_text('\ufeff公告'))
        self.assertEqual('application/json; charset=utf-8', onebot_utf8.JSON_CONTENT_TYPE)


class LegacyEventsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_contract_stub()
        cls.temp = tempfile.TemporaryDirectory()
        cls.server = Path(cls.temp.name) / 'server'
        cls.server.mkdir()
        registry = Path(cls.temp.name) / 'servers.json'
        registry.write_text(json.dumps({'servers': [{
            'id': 'legacy', 'name': 'SCEX Legacy Genesis', 'prefix': '[怀旧]',
            'aliases': ['怀旧', 'legacy'], 'enabled': True,
            'host': '127.0.0.1', 'port': 1,
            'password_file': str(Path(cls.temp.name) / 'rcon.txt'),
            'server_dir': str(cls.server),
        }]}, ensure_ascii=False), encoding='utf-8')
        cls.old_registry = server_registry.REGISTRY_PATH
        server_registry.REGISTRY_PATH = str(registry)
        cls.monitor = load_path('public_legacy_monitor_test', 'legacy_monitor.py')

    @classmethod
    def tearDownClass(cls):
        server_registry.REGISTRY_PATH = cls.old_registry
        cls.temp.cleanup()

    def test_join_count_and_unavailable_fallback(self):
        message = self.monitor._join_message(
            'Foo', lambda _server, _command: 'There are 3 of a max of 20 players online: A, B, Foo')
        self.assertEqual('[怀旧] 玩家 Foo 加入服务器（3/20）', message)
        self.assertEqual('[怀旧] 玩家 Foo 加入服务器（人数暂不可查）',
                         self.monitor._join_message('Foo', mock.Mock(side_effect=TimeoutError())))

    def test_player_chat_is_sanitized_and_bridge_echo_is_dropped(self):
        normal = ('[12:00:00] [Server thread/INFO] '
                  '[net.minecraft.server.MinecraftServer/]: <Foo> 你好')
        self.assertEqual('[怀旧] <Foo> 你好', self.monitor._player_chat_message(normal))
        self.assertIsNone(self.monitor._player_chat_message(normal.replace('你好', '[QQ群] echo')))
        injected = self.monitor._player_chat_message(normal.replace('你好', '[CQ:at,qq=all]'))
        self.assertNotIn('[CQ:', injected)


if __name__ == '__main__':
    unittest.main()
