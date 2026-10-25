"""Behavioural checks for optional current-message expression guidance."""

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sanae_persona import situational_expression_hint


class PublicIdentityTests(unittest.TestCase):
    def load(self, **settings):
        spec = importlib.util.spec_from_file_location(
            'identity_fixture', Path(__file__).resolve().parents[1] / 'sanae_identity.py')
        identity = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, settings, clear=True):
            spec.loader.exec_module(identity)
        return identity

    def test_unconfigured_import_has_no_real_account(self):
        identity = self.load()
        self.assertEqual(identity.SANAE_QQ, '0')
        self.assertEqual(identity.HISTORICAL_BOT_QQ, frozenset())

    def test_legacy_environment_and_explicit_current_account(self):
        self.assertEqual(self.load(SANAE_BOT_QQ='1002').SANAE_QQ, '1002')
        identity = self.load(SANAE_QQ='1004', SANAE_BOT_QQ='1002',
                             SANAE_HISTORICAL_BOT_QQ='1002,1003')
        self.assertEqual(identity.SANAE_QQ, '1004')
        self.assertEqual(identity.HISTORICAL_BOT_QQ, frozenset({'1002', '1003', '1004'}))

    def test_non_numeric_identity_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            self.load(SANAE_QQ='not-an-account')
        with self.assertRaises(ValueError):
            self.load(SANAE_HISTORICAL_BOT_QQ='1002,bad')


class SituationalExpressionHintTests(unittest.TestCase):
    def test_plain_chat_and_questions_do_not_force_roasting(self):
        for text in (
            "晚上好", "今天吃了面", "早苗在吗？", "3乘128除64是多少？",
            "你是不是AI", "你怎么这么毒舌？", "早苗，这是什么模组？",
            "这个对话选项为什么会导致失败？",
        ):
            with self.subTest(text=text):
                self.assertEqual(situational_expression_hint(text), "")

    def test_serious_need_wins_over_praise_and_jokes(self):
        for text in (
            "你真靠谱，帮我排查日志", "早苗好可爱，但我今天真的好累",
            "你真会损，认真问一下怎么修", "这段对话读档一下，删除刚才的记录",
            "读档哈哈，恢复服务器存档", "别吐槽，我说没改只是改了十个配置",
            "我只改了一个小地方，整个工厂都停了，怎么办",
            "这句台词走错分支了，难受", "你真厉害，查一下 TPS？",
        ):
            with self.subTest(text=text):
                self.assertEqual(situational_expression_hint(text), "")

    def test_clear_praise_can_be_received_without_deflecting(self):
        for text in ("早苗真靠谱", "你也太会接梗了", "谢谢早苗"):
            with self.subTest(text=text):
                result = situational_expression_hint(text)
                self.assertIn("称赞", result)
                self.assertNotIn("\n", result)

    def test_negated_praise_is_not_treated_as_compliment(self):
        for text in ("你不太靠谱", "你真可靠才怪", "你真厉害，我在说反话"):
            with self.subTest(text=text):
                self.assertEqual(situational_expression_hint(text), "")

    def test_explicit_banter_is_current_message_only(self):
        result = situational_expression_hint("早苗你真会损")
        self.assertIn("轻松调侃", result)
        self.assertIn("不延伸", result)
        self.assertNotIn("早苗你真会损", result)

    def test_self_supplied_contradiction_gets_one_principle(self):
        for text in (
            "我没改配置，只是改了十几个选项",
            "我没改配置，就调了十几个选项。",
            "我只改了一个小地方，整个工厂都停了哈哈",
        ):
            with self.subTest(text=text):
                result = situational_expression_hint(text)
                self.assertIn("当前消息自己摆出了反差", result)
                self.assertNotIn("\n", result)

    def test_contradiction_with_a_real_followup_request_gets_no_roast_hint(self):
        text = "我没改配置，就调了十几个选项。现在报错，帮我看看怎么修。"
        self.assertEqual(situational_expression_hint(text), "")

    def test_explicit_meta_joke_keeps_real_action_boundary(self):
        result = situational_expression_hint("这段对话能读档吗？")
        self.assertIn("比喻", result)
        self.assertIn("不声称", result)

    def test_cq_metadata_does_not_trigger_a_style(self):
        self.assertEqual(situational_expression_hint("[CQ:image,file=你真厉害.png]"), "")
        self.assertIn("称赞", situational_expression_hint("[CQ:at,qq=123]早苗真靠谱"))

    def test_oversized_or_invalid_payloads_have_no_nudge(self):
        for text in (None, 3, "", "   ", "早苗真靠谱" * 130):
            with self.subTest(text=type(text).__name__):
                self.assertEqual(situational_expression_hint(text), "")


if __name__ == "__main__":
    unittest.main()
