"""Round 20: when the asker is an admin she answers and asks what to do, instead of offering to
pass it to the admins. Offline.

10-27 00:46 (machine time) the owner asked where items and XP orbs piled up. She gave chunk
coordinates, then: "如果你是想清理这些实体或者需要更精确的坐标位置，我可以帮你把这个情况转达给管理组，
请管理员进行进一步处理".
"""
import copy
import unittest
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests
from tests.test_server_round8 import text_reply, tool_call
import world_status

s = runtime_tests.s
LEGACY = {'id': 'legacy', 'name': 'Legacy', 'prefix': '[怀旧]'}
REPLY_0046 = ('目前全服共有掉落物 736 个，经验球 985 个。\n\n掉落物最集中的区域是区块 (-678, 1459)；'
              '经验球最集中的区域是区块 (-938, 800)。\n\n这里的坐标是区块坐标。如果你是想清理这些实体或者需要更精确的坐标位置，'
              '我可以帮你把这个情况转达给管理组，请管理员进行进一步处理')
TOTAL = 'Total: 1721\n  985: minecraft:experience_orb\n  736: minecraft:item\n'
FILTERED = 'Entity: minecraft:experience_orb Total: 985\n  350: -938, 800\n  228: -938, 802\n'


class EntityFormatTests(unittest.TestCase):
    def test_admins_get_block_coordinates_too(self):
        admin = world_status.format_entities('[怀旧]', TOTAL, FILTERED, 'minecraft:experience_orb', detailed=True)
        self.assertIn('区块 (-938, 800)（方块约 x=-15000 z=12808） 350 个', admin)
        member = world_status.format_entities('[怀旧]', TOTAL, FILTERED, 'minecraft:experience_orb')
        self.assertIn('350、228（坐标只对管理员显示）', member)
        self.assertNotIn('x=', member)


class HandoffTests(unittest.TestCase):
    def test_the_offer_is_dropped_and_the_data_kept(self):
        self.assertTrue(s._offers_to_ask_admins(REPLY_0046))
        trimmed = s._drop_admin_handoff(REPLY_0046)
        self.assertIn('经验球最集中的区域是区块 (-938, 800)', trimmed)
        self.assertNotIn('管理组', trimmed)
        self.assertNotIn('请管理员', trimmed)
        # Nothing but the offer: keep it rather than send nothing.
        self.assertEqual('', s._drop_admin_handoff('我可以帮你转给管理组。'))
        self.assertFalse(s._offers_to_ask_admins('经验球最多的在 (-938, 800)，要我清理掉落物吗？会先发确认'))


class AgentTests(unittest.TestCase):
    setUp = runtime_tests.YachiyoPayloadTests.setUp

    def run_turn(self, privileged, content=REPLY_0046):
        sent = []
        replies = iter([tool_call('server_world', {'topic': 'entities', 'entity': 'minecraft:experience_orb'}),
                        text_reply(content)])

        def model(payload, **kwargs):
            sent.append(copy.deepcopy(payload))
            return next(replies)
        with mock.patch.object(s, '_text_post', side_effect=model), \
                mock.patch.object(s, 'execute_tool', return_value='[怀旧] 经验球 共 985 个'):
            answer, _, _ = s.run_agent('早苗，掉落物和经验球最多的坐标是哪里？', 'owner' if privileged else 'member',
                                       privileged, 'fixture-group', selected_server=LEGACY, explicit_server=True)
        return answer, sent

    def test_an_admin_gets_the_answer_without_the_handoff(self):
        answer, sent = self.run_turn(True)
        self.assertIn('【当前提问的人是管理员】', sent[0]['messages'][0]['content'])
        self.assertIn('经验球最集中的区域是区块 (-938, 800)', answer)
        self.assertNotIn('转达给管理组', answer)

    def test_a_member_keeps_the_offer(self):
        answer, sent = self.run_turn(False)
        self.assertNotIn('【当前提问的人是管理员】', sent[0]['messages'][0]['content'])
        self.assertIn('转达给管理组', answer)

    def test_policy_scopes_the_offer_to_members(self):
        self.assertIn('提问的人自己就是管理员时，坐标直接给，不提“转给管理组”', s.SANAE_OPERATIONAL_POLICY)


if __name__ == '__main__':
    unittest.main()
