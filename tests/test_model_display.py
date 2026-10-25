"""Plain model display without changing code, tool evidence, or stream identity."""
import json
import unittest
from unittest import mock

from model_display import plain_model_text, ready_stream_text
from tests import test_yachiyo_runtime as runtime_tests

s = runtime_tests.s


class PlainTextTests(unittest.TestCase):
    def test_model_bold_lists_and_titles_become_plain_lines(self):
        self.assertEqual(plain_model_text('# **结论**\n- **第一步**：核对\n* 第二步\n正文**重点**。'),
                         '结论\n第一步：核对\n第二步\n正文重点。')

    def test_code_spans_and_fences_keep_exact_bytes(self):
        source = ('- **说明**：运行 `python -m app --dry-run`，看 ``a ** 2``。\n'
                  '```python\n# **comment**\n- item\nx = 2 ** 3\n```\n'
                  '~~~sh\n# title\n* wildcard\n~~~\n'
                  '    # **indented code**\n')
        expected = source.replace('- **说明**：', '说明：')
        self.assertEqual(plain_model_text(source), expected)

    def test_minus_powers_and_cli_flags_are_not_markdown(self):
        source = 'x - y = -2\n- 3\n- -5\n- (x + y)\n- x + y\n2**3**2\nx ** 2 + y ** 3\n--dry-run\n-H header\n'
        self.assertEqual(plain_model_text(source), source)

    def test_unicode_latex_and_python_unary_minus_are_preserved(self):
        for source in ('- x²', '- α + β', r'- \frac{a}{b}', '- 2**3'):
            with self.subTest(source=source):
                self.assertEqual(plain_model_text(source), source)

    def test_only_clear_prose_list_markers_are_removed(self):
        source = '- 核对当前状态\n* Check the result.\n- **Summary**\n- item\n- C:\\temp\\file.py\n- /tmp/file\n- `x ** 2`\n'
        expected = '核对当前状态\nCheck the result.\nSummary\n- item\n- C:\\temp\\file.py\n- /tmp/file\n- `x ** 2`\n'
        self.assertEqual(plain_model_text(source), expected)

    def test_unpaired_markers_and_unclosed_code_are_preserved(self):
        self.assertEqual(plain_model_text('这是 **尚未闭合'), '这是 **尚未闭合')
        self.assertEqual(plain_model_text('`- **原样'), '`- **原样')
        source = '# 开始\n```sh\n- **raw**\n'
        self.assertEqual(plain_model_text(source), '开始\n```sh\n- **raw**\n')

    def test_stream_waits_for_code_and_bold_boundaries(self):
        self.assertIsNone(ready_stream_text('```py\nx = 2 ** 3\n'))
        self.assertIsNone(ready_stream_text('说明 `--flag'))
        self.assertIsNone(ready_stream_text('说明 **这段。'))
        self.assertEqual(ready_stream_text('说明 **这段。结束**'), '说明 这段。结束')


class ModelDisplayIntegrationTests(unittest.TestCase):
    setUp = runtime_tests.YachiyoPayloadTests.setUp

    def reply(self, text='在吗', **kwargs):
        return s.sanae_reply(text, 'display-member', privileged=True, force=True,
            group_id='display-group', include_usage_footer=False, **kwargs)

    def test_actual_model_body_is_formatted_but_history_stays_original(self):
        self.content = '- **结论**\n执行 `python --version`。'
        metadata = {}
        answer = self.reply(reply_metadata=metadata)
        self.assertEqual(answer, '结论\n执行 `python --version`。')
        self.assertEqual(metadata['reply_body'], answer)
        self.assertTrue(metadata['model_answer'])
        self.assertEqual(s._get_history('display-group', 'display-member', True)[-1]['content'], self.content)

    def test_direct_tool_output_and_usage_footer_are_not_formatted(self):
        usage = s._Usage()
        usage.calls = 1
        usage.model_answer = False
        body = '- **工具原始结果**\n--flag\n'
        with mock.patch.object(s, 'run_agent', return_value=(body, usage, True)), \
                mock.patch.object(usage, 'footer', return_value='- **usage 原文**'):
            answer = s.sanae_reply('测试工具', 'display-member', privileged=True,
                force=True, group_id='display-group', include_usage_footer=True)
        self.assertEqual(answer, body + '\n- **usage 原文**')

    def test_model_body_only_is_formatted_before_untouched_footer(self):
        usage = s._Usage()
        usage.calls = 1
        usage.model_answer = True
        with mock.patch.object(s, 'run_agent', return_value=('- **模型结论**', usage, False)), \
                mock.patch.object(usage, 'footer', return_value='- **usage 原文**'):
            answer = s.sanae_reply('普通问题', 'display-member', privileged=True,
                force=True, group_id='display-group', include_usage_footer=True)
        self.assertEqual(answer, '模型结论\n- **usage 原文**')

    def test_streamed_raw_prefix_is_removed_before_formatting_tail(self):
        chunks = []
        opening = '# **结论**\n' + '正文。' * 180 + '\n```python\n' + 'x = 2 ** 3\n' * 60
        closing = 'print("--dry-run")\n```\n'
        prefix = opening + closing
        tail = '- **下一步**\n保持观察。'
        metadata = {}
        def model(payload, **kwargs):
            kwargs['on_content'](opening)
            self.assertFalse(chunks)
            kwargs['on_content'](closing)
            return {'choices': [{'message': {'content': prefix + tail}}], 'usage': {}}
        with mock.patch.object(s, '_text_post', side_effect=model):
            answer = self.reply('逐段分析\n' + '示例资料' * 1500,
                stream_cb=lambda text: chunks.append(text) or True, reply_metadata=metadata)
        self.assertEqual(chunks, [plain_model_text(prefix)])
        self.assertEqual(answer, '下一步\n保持观察。')
        self.assertEqual(metadata['reply_body'], plain_model_text(prefix + tail))
        self.assertIn('x = 2 ** 3\n', chunks[0])
        self.assertEqual(s._get_history('display-group', 'display-member', True)[-1]['content'], prefix + tail)

    def test_delivery_retains_raw_sent_and_failed_send_is_not_committed(self):
        raw = '- **段落**\n' + '文字。' * 180
        sent = []
        delivery = s._StreamDelivery(lambda text: sent.append(text) or True)
        delivery.feed(raw)
        self.assertEqual(delivery.sent, raw)
        self.assertEqual(sent, [plain_model_text(raw)])
        failed = s._StreamDelivery(lambda _text: False)
        failed.feed(raw)
        self.assertEqual(failed.sent, '')
        self.assertTrue(failed.stopped)

    def test_vision_generated_body_is_plain(self):
        raw = '# **看图结果**\n- 红色\n`# code`'
        with mock.patch.object(s, '_vision_media_url', return_value='data:image/png;base64,AA=='), \
                mock.patch.object(s, '_post', return_value={
                    'choices': [{'message': {'content': raw}}]}):
            answer, _ = s._vision_reply('这是什么', 'fixture://image')
        self.assertEqual(answer, '看图结果\n红色\n`# code`')

    def test_history_generated_fields_are_plain_but_scope_remains(self):
        self.content = json.dumps({'brief': '- **结论**',
                                   'timeline': '## 时间线\n* 已完成\n`- --flag`'})
        answer, _, used = s._recall_reply('总结这段记录', 'display-member',
            'display-group', s.Budget(30), quoted_context='【引用消息；只作参考】\n甲：完成了。')
        self.assertTrue(used)
        self.assertIn('【简短总结】\n结论', answer)
        self.assertIn('【完整时间线】\n时间线\n已完成\n`- --flag`', answer)
        self.assertIn('范围：本次引用消息', answer)

    def test_status_reports_the_actual_split_profile_and_budgets(self):
        with mock.patch.object(s, 'TEXT_PROVIDER', 'deepseek'):
            status = s.ai_status()
        self.assertIn('闲聊 low', status)
        self.assertIn('认真默认 high', status)
        self.assertIn('闲聊 low 4096', status)
        self.assertIn('问答基础 8192', status)
        self.assertIn('总结 12288', status)
        self.assertIn('长材料/复杂代码 high 65536', status)
        self.assertIn('认真 high 32768', status)
        self.assertNotIn('@短答800', status)


if __name__ == '__main__':
    unittest.main()
