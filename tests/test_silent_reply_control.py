import os
import unittest
from unittest import mock
os.environ['SCE_BOT_TEST_MODE'] = '1'
import sanae_ai as s


class SilentReplyControlTests(unittest.TestCase):
    def setUp(self):
        self.usage = s._Usage()
        self.usage.model_answer = True
        self.usage.calls = 1

    def reply(self, body, *, tools=False, chat=True):
        metadata = {}
        with mock.patch.object(s, '_with_reply_context', side_effect=lambda text, **kw: text), \
             mock.patch.object(s, 'run_agent', return_value=(body, self.usage, tools)):
            result = s.sanae_reply('先别回我了' if chat else '请详细解释控制标记',
                'fixture', group_id='fixture', force=True, privileged=True,
                short_reply=chat, reply_metadata=metadata)
        return result, metadata

    def test_at_chat_silence_never_leaks_marker_or_cost(self):
        for body in ('[SILENT]', '  [silent]\n'):
            with self.subTest(body=body):
                result, metadata = self.reply(body)
                self.assertIsNone(result)
                self.assertFalse(metadata['model_answer'])
                self.assertFalse(metadata['social_reply'])
                self.assertEqual(metadata['reply_body'], '')

    def test_text_mentioning_marker_still_replies(self):
        result, metadata = self.reply('这个 [SILENT] 是内部标记。')
        self.assertIn('内部标记', result)
        self.assertTrue(metadata['model_answer'])

    def test_tool_receipt_is_not_hidden(self):
        result, _ = self.reply('[SILENT]', tools=True)
        self.assertIn('[SILENT]', result)

    def test_serious_literal_answer_is_not_hidden(self):
        result, _ = self.reply('[SILENT]', chat=False)
        self.assertIn('[SILENT]', result)
