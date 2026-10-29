"""Round 22: which world each online player is in, and an unavailable tool no longer ends the turn.
Offline.

10-27 11:49-11:50 (machine time) "查询当前在线玩家的世界分布（即位于哪个世界）":
- returnling (member): six refused run_rcon calls, then "因为权限限制……无法查看具体玩家所处的维度".
- the owner: the read-only turn did not offer request_console_command; she called it anyway and
  "拒绝：此工具不在当前请求可用列表" was sent to the group as the whole answer.
"""
import copy
import unittest
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests
from tests.test_server_round8 import text_reply, tool_call
import sanae_local_ops
import world_status

s = runtime_tests.s
LEGACY = {'id': 'legacy', 'name': 'Legacy', 'prefix': '[怀旧]'}
RCON = {
    'list': 'There are 3 of a max of 20 players online: Alice, Bob, Charlie',
    'data get entity Alice Dimension': 'Alice has the following entity data: "minecraft:overworld"',
    'data get entity Alice Pos': 'Alice has the following entity data: [-1520.4d, 64.0d, 2210.9d]',
    'data get entity Bob Dimension': 'Bob has the following entity data: "minecraft:the_nether"',
    'data get entity Bob Pos': 'Bob has the following entity data: [10.5d, 70.0d, -3.2d]',
}


def query(command):
    if command in RCON:
        return RCON[command]
    raise RuntimeError('no data')


class PlayerWorldTests(unittest.TestCase):
    def test_members_see_worlds_admins_see_coordinates(self):
        located = world_status.player_locations(query, query('list'))
        member = world_status.format_players('[怀旧]', located)
        self.assertIn('在线 3 人，按世界：', member)
        self.assertIn('主世界 1 人（Alice）', member)
        self.assertIn('下界 1 人（Bob）', member)
        self.assertIn('位置没取到：Charlie', member)
        self.assertIn('具体坐标没有列出', member)
        self.assertNotIn('-1521', member)
        admin = world_status.format_players('[怀旧]', located, detailed=True)
        self.assertIn('Alice 主世界 x=-1521 y=64 z=2210', admin)
        self.assertIn('Bob 下界 x=10 y=70 z=-4', admin)
        self.assertIn('现在没有在线玩家', world_status.format_players('[怀旧]', ([], [])))

    def test_tool_topic(self):
        server = {'id': 'legacy', 'prefix': '[怀旧]'}
        with mock.patch.object(sanae_local_ops.server_registry, 'query_server', side_effect=lambda srv, c: query(c)):
            text = sanae_local_ops._world(server, {'topic': 'players'}, None, '', False)
        self.assertIn('主世界 1 人（Alice）', text)
        with mock.patch.object(sanae_local_ops.server_registry, 'query_server', side_effect=lambda srv, c: query(c)):
            asked = sanae_local_ops._world(server, {'topic': 'players', 'with_coordinates': True}, None, '', True)
            not_asked = sanae_local_ops._world(server, {'topic': 'players'}, None, '', True)
            member = sanae_local_ops._world(server, {'topic': 'players', 'with_coordinates': True}, None, '', False)
        self.assertIn('x=-1521', asked)
        self.assertNotIn('x=-1521', not_asked)
        self.assertNotIn('x=-1521', member)
        self.assertIn('players', [t for t in s.SERVER_WORLD['function']['parameters']['properties']['topic']['enum']])

    def test_the_question_is_a_live_query(self):
        for text in ('早苗，查询当前在线玩家的世界分布（即位于哪个世界）。', '大家都在哪个世界', '在线的人都在哪'):
            with self.subTest(text=text):
                self.assertTrue(s._has_server_query_intent(text))


class UnavailableToolTests(unittest.TestCase):
    setUp = runtime_tests.YachiyoPayloadTests.setUp

    def test_an_unoffered_console_tool_does_not_end_the_turn(self):
        sent = []
        replies = iter([tool_call('request_console_command', {'command': 'data get entity Alice Dimension'}),
                        tool_call('server_world', {'topic': 'players'}),
                        text_reply('Alice 在主世界，Bob 在下界')])

        def model(payload, **kwargs):
            sent.append(copy.deepcopy(payload))
            return next(replies)

        def execute(name, args, privileged, **kwargs):
            return '[怀旧] 在线 3 人，按世界：主世界 1 人（Alice）；下界 1 人（Bob）'
        with mock.patch.object(s, '_text_post', side_effect=model), \
                mock.patch.object(s, 'execute_tool', side_effect=execute) as tool:
            answer, _, _ = s.run_agent('早苗，查询当前在线玩家的世界分布（即位于哪个世界）。', 'owner', True,
                                       'fixture-group', selected_server=LEGACY, explicit_server=True)
        offered = [t['function']['name'] for t in sent[0]['tools']]
        self.assertNotIn('request_console_command', offered)
        self.assertEqual('Alice 在主世界，Bob 在下界', answer)
        refusal = [m for m in sent[1]['messages'] if m.get('role') == 'tool'][0]['content']
        self.assertIn('此工具不在当前请求可用列表', refusal)
        self.assertEqual(['server_world'], [c.args[0] for c in tool.call_args_list])


if __name__ == '__main__':
    unittest.main()
