"""Offline behavior tests; no ping, upload, SSH or real socket is opened."""
import io
import json
import unittest
from unittest import mock

import minecraft_status
import network_commands
import network_probe


class ImmediateReplies:
    def submit(self, callback, **kwargs):
        callback()
        return True


class NetworkCommandsTests(unittest.TestCase):
    def test_busy_and_cooldown_do_not_start_another_probe(self):
        now = [100.0]
        jobs, replies, probes = [], [], []
        handler = network_commands.PingCommands(
            collector=lambda **kwargs: probes.append(kwargs) or {'fixture': True},
            renderer=lambda _data: 'measured result', clock=lambda: now[0],
            starter=jobs.append, replies=ImmediateReplies())
        self.assertTrue(handler.handle('!ping full', replies.append))
        self.assertTrue(handler.handle('!ping', replies.append))
        self.assertEqual(len(jobs), 1)
        self.assertIn('正在检测', replies[-1])
        jobs.pop()()
        self.assertEqual([{'mode': 'full'}], probes)
        self.assertIn('measured result', replies)
        handler.handle('!ping', replies.append)
        self.assertIn('冷却', replies[-1])
        self.assertEqual([], jobs)
        now[0] += 91
        handler.handle('!ping', replies.append)
        self.assertEqual(1, len(jobs))

    def test_custom_targets_are_rejected_and_unrelated_chat_is_ignored(self):
        replies, jobs = [], []
        handler = network_commands.PingCommands(starter=jobs.append, replies=ImmediateReplies())
        self.assertTrue(handler.handle('!ping example.invalid', replies.append))
        self.assertIn('不接受自定义', replies[-1])
        self.assertFalse(handler.handle('hello', replies.append))
        self.assertEqual([], jobs)

    def test_worker_failure_releases_busy_state(self):
        jobs, replies = [], []
        handler = network_commands.PingCommands(
            collector=mock.Mock(side_effect=RuntimeError('fixture failure')),
            starter=jobs.append, replies=ImmediateReplies())
        handler.handle('!ping', replies.append)
        jobs.pop()()
        self.assertFalse(handler.busy)
        self.assertIn('检测失败', replies[-1])

    def test_ping_statistics_preserve_submillisecond_bound_and_unavailable_state(self):
        result = network_probe._parse_ping(
            b'Reply: time<1ms\nReply: time<1ms\nAverage = 0ms\n(0% loss)')
        self.assertEqual('ok', result['status'])
        self.assertTrue(result['less_than_1ms'])
        self.assertEqual(0, result['loss_percent'])
        self.assertEqual('unknown', network_probe._parse_ping(b'Request timed out')['status'])


class StatusPacketTests(unittest.TestCase):
    def test_partial_reads_decode_player_status_and_do_not_send_commands(self):
        expected = {'version': {'name': 'Fixture'}, 'players': {'online': 2, 'max': 10}}
        raw = json.dumps(expected).encode()
        frame = b'\x00' + minecraft_status._encode_varint(len(raw)) + raw
        wire = io.BytesIO(minecraft_status._encode_varint(len(frame)) + frame)
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        sock.recv.side_effect = lambda size: wire.read(min(size, 2))
        with mock.patch.object(minecraft_status.socket, 'create_connection', return_value=sock) as connect:
            self.assertEqual(expected, minecraft_status.query('fixture.invalid', 25565))
        connect.assert_called_once_with(('fixture.invalid', 25565), timeout=5.0)
        self.assertEqual(2, sock.sendall.call_count)
        self.assertEqual(b'\x01\x00', sock.sendall.call_args.args[0])

    def test_invalid_configuration_never_connects(self):
        with mock.patch.object(minecraft_status.socket, 'create_connection') as connect:
            for host, port in [('', 25565), ('fixture.invalid', 0), ('fixture.invalid', 'bad')]:
                with self.assertRaises(minecraft_status.StatusError):
                    minecraft_status.query(host, port)
        connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
