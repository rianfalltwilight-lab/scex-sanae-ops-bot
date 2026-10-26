"""Topic profile hints and the probe hint (2026-10-25 persona round)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sanae_persona as persona  # noqa: E402

T0 = 1_000_000.0


class PersonaDetailHintTests(unittest.TestCase):
    def setUp(self):
        persona.reset_detail_cooldowns()
        self.addCleanup(persona.reset_detail_cooldowns)

    def hint(self, text, now=T0, scope="g"):
        return persona.persona_detail_hint(text, now=now, scope=scope)

    def test_profile_topics_offer_one_matching_detail(self):
        for text, expected in (("今晚好像要打雷", "打雷"), ("热死了这天", "怕热"),
                               ("家里蟑螂又出来了", "蜘蛛"), ("晚上去庙会", "苹果糖"),
                               ("咖啡还是得加糖", "伯爵茶"), ("背包满了得整理一下", "整理")):
            with self.subTest(text=text):
                persona.reset_detail_cooldowns()
                result = self.hint(text)
                self.assertIn(expected, result)
                self.assertIn("不合适就不提", result)

    def test_minecraft_words_and_slang_do_not_trigger(self):
        for text in ("叫爷爷", "你爷爷我来了", "高温炉炸了", "烟花火箭不够了",
                     "开导航去下界", "分类讨论一下", "钓鱼执法", "主机挂了"):
            with self.subTest(text=text):
                persona.reset_detail_cooldowns()
                self.assertEqual(self.hint(text), "")

    def test_serious_emotional_or_invalid_text_gets_nothing(self):
        for text in ("我今天好累，热死了", "TPS 好卡怎么办", "帮我看看打雷的日志", None, "", "雷" * 700):
            with self.subTest(text=str(text)[:10]):
                self.assertEqual(self.hint(text), "")

    def test_cooldowns_per_topic_global_and_per_group(self):
        self.assertTrue(self.hint("今晚打雷", T0))
        self.assertEqual(self.hint("热死了", T0 + 60), "")            # any-topic rest
        self.assertTrue(self.hint("热死了", T0 + 601))
        self.assertEqual(self.hint("又打雷了", T0 + 700), "")         # same topic rest
        self.assertTrue(self.hint("又打雷了", T0 + 3601))
        self.assertTrue(self.hint("今晚打雷", T0 + 3602, scope="other-group"))

    def test_probe_hint_is_narrow_and_has_no_fixed_opening(self):
        for text in ("随便表现一下", "八千代，来展示一下", "测试一下你", "考考早苗", "给你打个分",
                     "这回答能打几分", "随便说两句", "表现一下吧"):
            with self.subTest(text=text):
                self.assertEqual(persona.situational_expression_hint(text), persona._HINTS["probe"])
        for text in ("你是不是AI", "展示一下你的基地", "我试试你说的办法", "这个测试服什么时候开"):
            with self.subTest(text=text):
                self.assertNotEqual(persona.situational_expression_hint(text), persona._HINTS["probe"])
        examples = persona.SOCIAL_STYLE_GUIDANCE.split("下面的场景", 1)[1]
        self.assertNotIn("坦白一件", examples)

    def test_ai_identity_question_is_answered_honestly(self):
        self.assertIn("直接承认你是群里的 AI 角色", persona.SANAE_PERSONALITY)


if __name__ == "__main__":
    unittest.main()
