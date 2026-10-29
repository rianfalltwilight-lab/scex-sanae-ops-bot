"""Round 25: tool schemas the 10-27 upstream (sub.kai.lc) accepts. Offline.

It rejected every request that offered notify_admins with HTTP 400 INVALID_ARGUMENT, because the
"attach" enum contained an empty string. Dropping that one declaration made the request pass.
"""
import unittest

from tests import test_yachiyo_runtime as runtime_tests

s = runtime_tests.s


def _empty_enums(node, path=()):
    if isinstance(node, dict):
        if '' in (node.get('enum') or []):
            yield path
        for key, value in node.items():
            yield from _empty_enums(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _empty_enums(value, path + (index,))


class SchemaTests(unittest.TestCase):
    def test_no_empty_enum_values(self):
        for tools in (s.TOOLS_ADMIN, s.TOOLS_MEMBER, s.CONSOLE_TOOLS):
            for tool in tools:
                with self.subTest(tool=tool['function']['name']):
                    self.assertEqual([], list(_empty_enums(tool['function'].get('parameters') or {})))
        declarations = s._gemini_function_declarations(s.TOOLS_ADMIN)
        self.assertEqual([], list(_empty_enums(declarations)))

    def test_notify_attach_values(self):
        attach = s.NOTIFY_ADMINS['function']['parameters']['properties']['attach']
        self.assertEqual(['none', 'entities'], attach['enum'])


if __name__ == '__main__':
    unittest.main()
