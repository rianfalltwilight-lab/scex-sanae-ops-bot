"""Round 34 (10-28 evening review): a hung server is not "fine" because its process is up, and a bare
call of her name followed by the real question gets one answer. Offline."""
import os
import tempfile
import unittest
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests
import sanae_local_ops

s, bridge = runtime_tests.s, runtime_tests.bridge
HOST = {'process': {'rss_kb': 8 * 1024 * 1024, 'cpu': 412, 'etime': '03:07:00'}, 'uptime': '11220 s', 'cores': 12,
        'collected_at': 1793180000}


class HostLivenessTests(unittest.TestCase):
    def host(self, query):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        server = {'id': 'legacy', 'prefix': '[怀旧]', 'remote_ssh_host': 'remote-test', 'server_dir': temp.name}
        with mock.patch.dict(os.environ, {'OPS_STATE_BASE': temp.name}), \
                mock.patch.object(sanae_local_ops, '_resolved_server', return_value=server), \
                mock.patch.object(sanae_local_ops.server_traffic, 'recover'), \
                mock.patch.object(sanae_local_ops, '_host', return_value=HOST), \
                mock.patch.object(sanae_local_ops.server_registry, 'query_server', side_effect=query):
            return sanae_local_ops.execute_insight('server_world', {'topic': 'host'}, server, False)

    def test_a_hung_main_thread_is_named(self):
        # 10-28 17:52 "早苗也崩了" -> "服务器那边也好好开着，已经连续运行 3 小时 7 分钟了"
        text = self.host(mock.Mock(side_effect=TimeoutError()))
        self.assertIn('已连续运行 3 小时 7 分钟', text)
        self.assertIn('游戏控制台（RCON）：没有响应（TimeoutError）', text)
        self.assertIn('不能说服务器好好开着', text)
        self.assertIn('游戏控制台（RCON）：响应正常', self.host(lambda server, command: 'There are 0 of a max of 20 players online:'))

    def test_policy(self):
        self.assertIn('RCON 没响应就是卡住或崩了', s.SANAE_OPERATIONAL_POLICY)


class BareCallTests(unittest.TestCase):
    def event(self, message_id, raw):
        return {'group_id': 1001, 'user_id': 2003, 'message_id': message_id, 'raw_message': raw,
                'sender': {'card': 'FixtureMember'}}

    def test_a_bare_call_gives_way_to_the_question_after_it(self):
        bare = self.event(1, f'[CQ:at,qq={s.SANAE_QQ}] 早苗')
        question = self.event(2, f'[CQ:at,qq={s.SANAE_QQ}] 早苗，服务器崩了，汇报下是什么情况')
        with mock.patch.object(bridge, '_AI_PENDING', {}), mock.patch.object(bridge, '_AI_LATEST', {}):
            bridge._ai_pending_start(bare)
            self.assertFalse(bridge._superseded_bare_call(bare))
            bridge._ai_pending_start(question)
            self.assertTrue(bridge._superseded_bare_call(bare))
            self.assertFalse(bridge._superseded_bare_call(question))
            other = dict(bare, user_id=2012, message_id=3)
            bridge._ai_pending_start(other)
            self.assertFalse(bridge._superseded_bare_call(other))
        self.assertTrue(bridge._bare_call('[CQ:at,qq=1002] 早苗！'))
        self.assertTrue(bridge._bare_call('八千代？'))
        self.assertFalse(bridge._bare_call('早苗 tps'))

    def test_the_reply_thread_checks_it(self):
        source = open(bridge.__file__, encoding='utf-8').read()
        self.assertIn("if reply and _superseded_bare_call(event):", source)


class NaturalRestartTests(unittest.TestCase):
    """Owner decision 10-28: an admin saying "重启服务器" gets the same confirmation as `!restart`."""
    SERVER = {'id': 'legacy', 'prefix': '[怀旧]'}

    def request(self, command, privileged=True, text='早苗，重启服务器'):
        return s.execute_tool('request_console_command', {'command': command}, privileged, current_user_text=text,
                              selected_server=self.SERVER, user_id='2002', nickname='FixtureAdmin',
                              explicit_server=True, group_id=1001)

    def test_restart_goes_through_the_same_confirmation(self):
        import rcon_ops
        import legacy_mslx_control
        self.addCleanup(rcon_ops._PENDING_RISK.clear)
        with mock.patch.object(legacy_mslx_control, 'start', return_value='[控制台] 已提交怀旧服重启到 MSLX') as start, \
                mock.patch.object(rcon_ops, '_audit'), mock.patch.object(s, 'check_active'):
            reply = self.request('restart')
            self.assertIn('[高危确认] 即将重启服务器', reply)
            self.assertIn('回复八千代：确认', reply)
            self.assertIn('不用带确认码', reply)
            start.assert_not_called()
            pending = rcon_ops.pending_confirmation('2002')
            self.assertEqual('legacy', pending['server']['id'])

            def backend(command):
                raise AssertionError('restart must not go through RCON')
            backend.server_id = 'legacy'
            done = rcon_ops.dispatch('确认 ' + pending['code'], 'FixtureAdmin', '2002', True,
                                     query_fn=backend, server=self.SERVER)
        self.assertIn('已提交怀旧服重启', done)
        start.assert_called_once_with('restart', '2002')

    def test_guards(self):
        with mock.patch.object(s, 'check_active'):
            self.assertIn('不带参数', self.request('restart now'))
            self.assertIn('只有群主或管理员', self.request('restart', privileged=False))
            self.assertIn('没有明确要求执行', self.request('restart', text='重启是什么意思'))
        self.assertIn('command 写 restart 或 stop', s.NATURAL_OPERATION_POLICY)


if __name__ == '__main__':
    unittest.main()
