"""Uncertainty and negation must never authorize a pending server operation."""
import unittest
from unittest import mock

from tests import test_yachiyo_runtime as runtime_tests

s = runtime_tests.s


class ConfirmationNegationTests(unittest.TestCase):
    SERVER = {'id': 'fixture', 'prefix': '[Test]'}
    PENDING = {'code': '1234', 'server': SERVER}

    def confirm(self, text, privileged=True, user_id='2002'):
        return s.execute_tool(
            'confirm_console_command', {}, privileged,
            current_user_text=text, selected_server=self.SERVER,
            user_id=user_id, nickname='FixtureAdmin', group_id=1001,
        )

    def test_negative_and_uncertain_messages_never_reach_execution(self):
        with mock.patch.object(s, 'check_active'), \
                mock.patch.object(s._rcon_ops, 'pending_confirmation', return_value=self.PENDING) as pending, \
                mock.patch.object(s._rcon_ops, 'dispatch') as dispatch:
            for text in ('不确定', '我还不确定', '先别确定', '不要确认', '未确认', '没确认',
                         '还不能确定', '我没有确认', '别执行', '不要执行'):
                with self.subTest(text=text):
                    self.assertFalse(s._has_confirmation_intent(text))
                    self.assertIn('没有明确确认', self.confirm(text))
            pending.assert_not_called()
            dispatch.assert_not_called()

    def test_plain_confirmation_uses_original_pending_owner_and_target(self):
        with mock.patch.object(s, 'check_active'), \
                mock.patch.object(s._rcon_ops, 'pending_confirmation', return_value=self.PENDING) as pending, \
                mock.patch.object(s._rcon_ops, 'dispatch', return_value='fixture executed') as dispatch:
            self.assertTrue(s._has_confirmation_intent('确定'))
            self.assertEqual('fixture executed', self.confirm('确定'))
        pending.assert_called_once_with('2002', '')
        args, kwargs = dispatch.call_args
        self.assertEqual(('确认 1234', 'FixtureAdmin', '2002', True), args)
        self.assertEqual(self.SERVER, kwargs['server'])
        self.assertEqual('fixture', kwargs['query_fn'].server_id)

    def test_confirmation_still_requires_privilege(self):
        with mock.patch.object(s, 'check_active'), \
                mock.patch.object(s._rcon_ops, 'pending_confirmation') as pending, \
                mock.patch.object(s._rcon_ops, 'dispatch') as dispatch:
            self.assertIn('只有原管理员', self.confirm('确定', privileged=False))
            pending.assert_not_called()
            dispatch.assert_not_called()

    def test_confirmation_without_callers_pending_operation_does_not_execute(self):
        with mock.patch.object(s, 'check_active'), \
                mock.patch.object(s._rcon_ops, 'pending_confirmation', return_value=None) as pending, \
                mock.patch.object(s._rcon_ops, 'dispatch') as dispatch:
            self.assertIn('没有找到你本人唯一', self.confirm('确定', user_id='2003'))
            pending.assert_called_once_with('2003', '')
            dispatch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
