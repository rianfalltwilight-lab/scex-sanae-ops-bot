"""A small direct-banter exception must not turn normal questions into roasts."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sanae_persona import playful_tease_question, situational_expression_hint


class PlayfulQuestionTests(unittest.TestCase):
    def test_direct_mild_confirmations_can_receive_tease_hint(self):
        for text in (
            "你是不是又得意上了？", "早苗你又嘴硬了是不是？",
            "早苗，你是不是又偷偷得意上了？", "你又嘴硬了是吧？",
            "早苗你是不是小得意了？", "你还在嘴硬吗？",
            "[CQ:at,qq=123] 早苗，你是不是又得意上了？",
        ):
            with self.subTest(text=text):
                self.assertTrue(playful_tease_question(text))
                self.assertIn("轻松调侃", situational_expression_hint(text))

    def test_question_grammar_does_not_require_question_mark(self):
        for text in ("你是不是又嘴硬了", "早苗你又嘴硬了是不是"):
            with self.subTest(text=text):
                self.assertTrue(playful_tease_question(text))

    def test_ordinary_questions_and_praise_do_not_receive_tease_hint(self):
        for text in (
            "你是不是AI？", "早苗在吗？", "你真可靠吗？", "你是不是很厉害？",
            "你怎么这么毒舌？", "早苗又嘴硬了为什么？", "得意是什么意思？",
            "你是不是又答错了？", "你是不是在骗我？", "你是不是不懂？",
            "你是不是又得意上了，昨天那题你都没答对？",
        ):
            with self.subTest(text=text):
                self.assertFalse(playful_tease_question(text))
                self.assertEqual(situational_expression_hint(text), "")

    def test_serious_or_emotional_need_wins(self):
        for text in (
            "认真点，你是不是又得意上了？", "你是不是又得意上了？帮我看看",
            "早苗你又嘴硬了是不是？我今天很难受", "别损我，你是不是又嘴硬了？",
            "你是不是又得意上了？我现在好累", "你是不是又嘴硬了，回答我的问题？",
        ):
            with self.subTest(text=text):
                self.assertFalse(playful_tease_question(text))
                self.assertEqual(situational_expression_hint(text), "")

    def test_quotes_reported_speech_and_third_person_are_not_direct_banter(self):
        for text in (
            "他说你是不是又得意上了？", "小明问早苗你又嘴硬了是不是？",
            "“你是不是又得意上了？”", '"早苗你又嘴硬了是不是？"',
            "你是不是又得意上了？是朋友刚才说的。", "她是不是又嘴硬了？",
            "如果有人问你是不是又得意上了？", "引用：你是不是又得意上了？",
            "[CQ:forward,id=123]", "[CQ:image,file=你是不是又嘴硬了？.png]",
        ):
            with self.subTest(text=text):
                self.assertFalse(playful_tease_question(text))
                self.assertEqual(situational_expression_hint(text), "")

    def test_declarative_banter_and_existing_hints_are_unchanged(self):
        self.assertFalse(playful_tease_question("早苗你真会损"))
        self.assertIn("轻松调侃", situational_expression_hint("早苗你真会损"))
        self.assertIn("称赞", situational_expression_hint("你也太会接梗了"))
        self.assertIn("当前消息自己摆出了反差", situational_expression_hint("我没改配置，只是改了十几个选项"))
        self.assertIn("比喻", situational_expression_hint("这段对话能读档吗？"))

    def test_invalid_or_long_inputs_have_no_exception(self):
        for text in (None, 3, "", "   ", "你" + "又" * 100 + "嘴硬了吗？"):
            with self.subTest(text=type(text).__name__):
                self.assertFalse(playful_tease_question(text))


if __name__ == "__main__":
    unittest.main()
