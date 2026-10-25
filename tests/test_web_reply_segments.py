import re
import unittest

from social_lite import split_informative_reply, split_social_reply


class InformativeReplyTests(unittest.TestCase):
    def assert_preserved(self, original, parts, max_parts=8):
        self.assertLessEqual(len(parts), max_parts)
        self.assertEqual(re.sub(r"\s+", "", original),
                         re.sub(r"\s+", "", "".join(parts)))
        self.assertTrue(all(part and len(part) <= 3500 for part in parts))

    def test_empty_and_short_answer(self):
        self.assertEqual(split_informative_reply(None), [])
        self.assertEqual(split_informative_reply(" \n\t "), [])
        self.assertEqual(split_informative_reply("  查到了，确实发布了。  "),
                         ["查到了，确实发布了。"])

    def test_medium_answer_keeps_content_in_a_few_paragraph_bubbles(self):
        paragraphs = [f"第{i}段：" + "这里保留网页给出的具体依据与限制。" * 20
                      for i in range(4)]
        text = "\n\n".join(paragraphs)
        parts = split_informative_reply(text)
        self.assert_preserved(text, parts)
        self.assertTrue(2 <= len(parts) <= 6, parts)
        self.assertTrue(all(200 <= len(part) <= 500 for part in parts))
        self.assertEqual(parts, paragraphs)

    def test_long_single_paragraph_uses_complete_sentences_not_every_sentence(self):
        sentence = "这个条件仅适用于当前版本，升级时还需要核对依赖。"
        text = sentence * 55
        parts = split_informative_reply(text)
        self.assert_preserved(text, parts)
        self.assertTrue(2 <= len(parts) <= 6)
        self.assertTrue(all(part.endswith("。") for part in parts))
        self.assertTrue(all(len(part) >= len(sentence) * 5 for part in parts))

    def test_versions_numbers_urls_and_markdown_citations_remain_whole(self):
        prefix = "网页给出了这一项变化。" * 30
        citation = "[版本 1.21.1。构建 4.29.1 的说明](https://example.test/v1.21.1/notes_(stable)?n=12.50)"
        bare_url = "https://example.test/releases/4.29.1?q=12.50&next=v1.2.3"
        text = prefix + citation + "，吞吐为 12.50 token/s。\n\n" + bare_url + "\n" + prefix
        parts = split_informative_reply(text)
        self.assert_preserved(text, parts)
        self.assertTrue(any(citation in part for part in parts))
        self.assertTrue(any(bare_url in part for part in parts))
        self.assertTrue(any("12.50 token/s" in part for part in parts))

    def test_fenced_code_including_blank_lines_is_not_split(self):
        code = '```python\nprint("第一句。第二句。")\n\n' + '# 保留完整代码。\n' * 45 + 'print(1.25)\n```'
        text = "先看结果。" * 55 + "\n\n" + code + "\n\n" + "这段代码只用于复现。" * 35
        parts = split_informative_reply(text)
        self.assert_preserved(text, parts)
        self.assertTrue(any(code in part for part in parts))
        self.assertGreater(len(parts), 1)

    def test_tilde_and_unclosed_fences_stay_whole(self):
        for code in ["~~~sh\n" + "echo '结果。'\n" * 35 + "~~~",
                     "```text\n" + "这里尚未关闭代码块。\n" * 40]:
            text = "说明如下。" * 60 + "\n\n" + code
            parts = split_informative_reply(text)
            self.assert_preserved(text, parts)
            self.assertTrue(any(code.rstrip() in part for part in parts))

    def test_command_and_inline_code_stay_whole(self):
        command = "pwsh -Command \"Write-Output '版本。'; Write-Output '数值。'\" " + "x" * 450
        inline = "`echo '这是一个完整命令。不能从中间切。'`"
        text = "说明。" * 90 + "\n\n" + command + "\n\n" + inline + "\n" + "再看输出。" * 60
        parts = split_informative_reply(text)
        self.assert_preserved(text, parts)
        self.assertTrue(any(command in part for part in parts))
        self.assertTrue(any(inline in part for part in parts))

    def test_reference_link_is_kept_whole(self):
        link = "[原始说明。包括详细条件][source-1]"
        text = "前面的事实。" * 50 + link + "\n\n" + "后面的限制。" * 50
        parts = split_informative_reply(text)
        self.assert_preserved(text, parts)
        self.assertTrue(any(link in part for part in parts))

    def test_multibacktick_inline_code_and_shell_continuation_are_not_split(self):
        inline = "``echo '保留。`单反引号`也属于代码。'``"
        command = "pwsh -Command `\n  Write-Output '" + "保持整段。" * 90 + "'"
        text = "说明。" * 90 + inline + "\n\n" + command + "\n\n" + "结果。" * 90
        parts = split_informative_reply(text)
        self.assert_preserved(text, parts)
        self.assertTrue(any(inline in part for part in parts))
        self.assertTrue(any(command in part for part in parts))

    def test_many_paragraphs_are_grouped_with_eight_bubble_ceiling(self):
        text = "\n\n".join(f"第{i}项。" + "内容必须完整保留。" * 10 for i in range(60))
        parts = split_informative_reply(text)
        self.assert_preserved(text, parts)
        self.assertTrue(2 <= len(parts) <= 8)

    def test_long_atomic_span_and_overall_capacity_fall_back_without_truncation(self):
        for text in ["```text\n" + "内容。\n" * 1000 + "```",
                     "没有可以安全断开的长句" * 400,
                     "有完整句子。" * 5000]:
            self.assertEqual(split_informative_reply(text), [text])

    def test_caller_can_reserve_bubbles_for_attachments(self):
        text = "完整说明。" * 700
        parts = split_informative_reply(text, max_parts=6)
        self.assert_preserved(text, parts, max_parts=6)
        self.assertEqual(split_informative_reply(text, max_parts=1), [text])

    def test_existing_short_social_cadence_is_unchanged(self):
        self.assertEqual(split_social_reply("行啊\n你还真会挑"), ["行啊", "你还真会挑"])
        text = "这是原有闲聊路径不应自动拆开的长回答。" * 30
        self.assertEqual(split_social_reply(text), [text])


if __name__ == "__main__":
    unittest.main()
