"""Server-question routing: which messages must be answered from tools, not from memory.

Cases come from the 10-26 06:47-06:59 exchange, where four answerable questions went
to tool-free chat and were answered "查不了". Offline: the model is a fake.
"""
import copy
import json
import unittest
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests

s = runtime_tests.s


class QueryIntentTests(unittest.TestCase):
    def test_login_mod_and_server_content_questions_need_tools(self):
        for text in ('早苗,帮忙查我同id账号的最近一次登录现实时间',
                     '早苗,帮忙查询和我qq群昵称同名的服务器内账号上次登录时间。',
                     'Alice 最近什么时候上线的', 'Bob 多久没上线了', '查一下我的进服记录',
                     '早苗,列一下服务器主要模组清单,即将各种运行库类、联动模组省去,可以简要统计下么?不需要完全准确。',
                     '早苗,有当前服的详细模组清单么', '服务器装了哪些模组', '本服有没有装机械动力模组',
                     '早苗早苗,服务器里有什么存储大量物品的方案?', '服里有哪些魔法类的东西',
                     # Already covered before; must stay covered.
                     '早苗,半夜有tps或者网络波动吗', 'Alice 玩了多久', '今晚卡不卡'):
            with self.subTest(text=text):
                self.assertTrue(s._has_server_query_intent(text))

    def test_ordinary_chat_stays_chat(self):
        # The chunk question moved to the tool side on 10-26, once server_world gave it a source.
        self.assertTrue(s._has_server_query_intent('早苗,帮忙统计下当前有多少个区块在加载'))
        for text in ('早苗你有什么爱好', '你喜欢什么mod', '这个模组好玩吗', '我刚登录游戏',
                     '晚安，明天再上线', '模组作者又鸽了', '服务器的事我不懂',
                     '模组清单是什么意思'):
            with self.subTest(text=text):
                self.assertFalse(s._has_server_query_intent(text))


class ForcedQueryPayloadTests(unittest.TestCase):
    setUp = runtime_tests.YachiyoPayloadTests.setUp

    def test_mod_list_tool_and_asker_name_are_offered(self):
        sent = []
        replies = iter([
            {'choices': [{'message': {'content': '', 'tool_calls': [{
                'id': 'c1', 'type': 'function',
                'function': {'name': 'list_mods', 'arguments': '{}'}}]}}]},
            {'choices': [{'message': {'content': '装了 AE2 和精妙存储，可以用来存大量物品。'}}]}])

        def model(payload, **kwargs):
            sent.append(copy.deepcopy(payload))
            return next(replies)
        with mock.patch.object(s, '_text_post', side_effect=model), \
                mock.patch.object(s, 'execute_tool', return_value='已安装模组：2 个') as execute:
            answer, _, used = s.run_agent('服务器里有什么存储大量物品的方案', 'fixture-member', False,
                                          'fixture-group', short_reply=True, selected_server={'id': 'legacy'},
                                          nickname='Char[CQ:at,qq=1]lie\n')
        self.assertTrue(used)
        self.assertIn('AE2', answer)
        execute.assert_called_once()
        self.assertEqual('list_mods', execute.call_args.args[0])
        self.assertIn(s.LIST_MODS, sent[0]['tools'])
        self.assertEqual('required', sent[0]['tool_choice'])
        self.assertNotIn('tool_choice', sent[1])           # list_mods satisfied the forced query
        prompt = sent[0]['messages'][0]['content']
        self.assertIn('当前提问者的群名片是「Charlie」', prompt)
        self.assertIn('先用 list_mods 看真实清单', prompt)
        self.assertIn('kind=join', prompt)


if __name__ == '__main__':
    unittest.main()
