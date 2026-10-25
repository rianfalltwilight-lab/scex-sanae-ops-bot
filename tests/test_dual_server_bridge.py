"""Temporary registries and queued callbacks; no external services."""
from contextvars import ContextVar
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import bridge_runtime
import minecraft_status
import server_registry as registry


class RegistryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.base = self.root / 'servers.json'
        self.extra = self.root / 'extra.json'
        self.base.write_text(json.dumps({'servers': [{
            'id': 'legacy', 'name': 'Legacy', 'prefix': '[Legacy]',
            'aliases': ['main'], 'enabled': True, 'host': 'localhost',
            'port': 25575, 'server_dir': str(self.root / 'server'),
        }]}), encoding='utf-8')
        self.extra.write_text(json.dumps({'servers': [{
            'id': 'secondary', 'name': 'Secondary', 'prefix': '[Secondary]',
            'kind': 'status', 'host': 'localhost', 'port': 25565,
            'chat_enabled': False,
        }]}), encoding='utf-8')
        patches = [mock.patch.object(registry, 'REGISTRY_PATH', str(self.base)),
                   mock.patch.object(registry, 'SELECTION_PATH', str(self.root / 'selection.json')),
                   mock.patch.dict(os.environ, {'SCE_SERVER_EXTRA_REGISTRY': str(self.extra),
                                                'SCE_SERVER_GROUP_DEFAULTS': '1001:legacy,1003:secondary'})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        registry._SELECTIONS.clear()
        registry._SELECTIONS_LOADED_FROM = None

    def test_group_defaults_are_separate_from_persisted_user_selection(self):
        self.assertEqual('legacy', registry.get_group_default('1001')[0]['id'])
        self.assertEqual('secondary', registry.get_group_default('1003')[0]['id'])
        self.assertIsNone(registry.get_group_default('unknown')[0])
        self.assertFalse((self.root / 'selection.json').exists())
        registry.select_server('1001', '2001', 'secondary')
        self.assertEqual('secondary', registry.get_selected('1001', '2001')[0]['id'])
        self.assertEqual('legacy', registry.get_group_default('1001')[0]['id'])

    def test_duplicate_overlay_server_is_rejected(self):
        self.extra.write_text(self.base.read_text('utf-8'), encoding='utf-8')
        with self.assertRaises(ValueError):
            registry._validated_rows()

    def test_status_adapter_never_reads_rcon_password_or_executes_commands(self):
        server = registry.resolve_server('secondary')
        response = {'version': {'name': 'Test Minecraft'},
                    'players': {'online': 1, 'max': 20, 'sample': [{'name': 'ExamplePlayer'}]}}
        with mock.patch.object(registry, '_read_password', side_effect=AssertionError('RCON forbidden')), \
                mock.patch.object(minecraft_status, 'query', return_value=response) as query:
            self.assertIn('ExamplePlayer', registry.query_server(server, 'list'))
            self.assertIn('Test Minecraft', registry.query_server(server, 'version'))
            for command in ('stop', 'neoforge tps', 'op ExamplePlayer', 'say hello'):
                with self.assertRaises(registry.StatusOnlyError):
                    registry.query_server(server, command)
            self.assertEqual(query.call_count, 2)

    def test_chat_disabled_server_is_not_called(self):
        calls = []
        results = registry.fanout_group_message('Example', 'hello',
            servers=registry.list_servers(), query_fn=lambda server, command: calls.append((server['id'], command)))
        self.assertEqual(['legacy'], [item[0] for item in calls])
        self.assertEqual(1, len(results))


class QueuedContextTests(unittest.TestCase):
    def test_each_callback_keeps_the_group_context_at_submission(self):
        group = ContextVar('synthetic_group', default='unset')
        work = bridge_runtime.WorkQueue(workers=0, capacity=2)
        seen = []
        group.set('1001')
        self.assertTrue(work.submit(lambda: seen.append(group.get())))
        group.set('1003')
        self.assertTrue(work.submit(lambda: seen.append(group.get())))
        group.set('other')
        work.queue.get_nowait()()
        work.queue.get_nowait()()
        self.assertEqual(['1001', '1003'], seen)
        self.assertEqual('other', group.get())

    def test_full_command_queue_returns_without_blocking_event_router(self):
        work = bridge_runtime.WorkQueue(workers=0, capacity=1)
        self.assertTrue(work.submit(lambda: None))
        with mock.patch.object(bridge_runtime, 'JOBS', work):
            self.assertFalse(bridge_runtime.start_worker(lambda: None))
        with mock.patch.object(bridge_runtime, 'AI_JOBS', work):
            self.assertFalse(bridge_runtime.start_ai_worker(lambda: None))


if __name__ == '__main__':
    unittest.main()
