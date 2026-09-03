#!/usr/bin/env python3
import importlib
import json
import os
import re
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ["SCE_BOT_TEST_MODE"] = "1"
os.environ.setdefault("LLBOT_GROUP", "1001")
os.environ.setdefault("SANAE_BOT_QQ", "1002")

if 'local_secrets' not in sys.modules:
    secrets_stub = types.ModuleType('local_secrets')
    secrets_stub.require_secret = lambda _name: 'test-only'
    sys.modules['local_secrets'] = secrets_stub
try:
    import PIL  # noqa: F401
except ModuleNotFoundError:
    pil = types.ModuleType('PIL')
    pil.Image = types.ModuleType('PIL.Image')
    pil.ImageDraw = types.ModuleType('PIL.ImageDraw')
    sys.modules['PIL'] = pil
    sys.modules['PIL.Image'] = pil.Image
    sys.modules['PIL.ImageDraw'] = pil.ImageDraw

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_SRC = os.path.join(HERE, "src")
if os.path.isdir(PACKAGE_SRC):
    HERE = PACKAGE_SRC
if HERE not in sys.path:
    sys.path.insert(0, HERE)
os.environ.setdefault("SCE_SERVER_REGISTRY", os.path.join(HERE, "examples", "servers.example.json"))

r = importlib.import_module("rcon_ops")
b = importlib.import_module("llbot_bridge")


class RiskConfirmationTests(unittest.TestCase):
    def setUp(self):
        r._PENDING_RISK.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = Path(self.tmp.name) / "audit.jsonl"
        self.audit_patch = mock.patch.object(r, "AUDIT_PATH", self.audit)
        self.audit_patch.start()

    def tearDown(self):
        self.audit_patch.stop()
        self.tmp.cleanup()

    def _code(self, text):
        return re.search(r"!确认\s+([A-Z0-9]{4})", text).group(1)

    def test_confirmation_bound_to_requester(self):
        called = []
        text = r._request_risk("owner", "测试", "stop", lambda: called.append(1) or "DONE")
        code = self._code(text)
        denied = r._confirm_risk("other", code)
        self.assertIn("原发起人", denied)
        self.assertEqual(called, [])
        self.assertEqual(r._confirm_risk("owner", code), "DONE")
        self.assertEqual(called, [1])

    def test_executor_failure_is_not_reported_as_success(self):
        def fail():
            raise RuntimeError("boom")
        code = self._code(r._request_risk("owner", "测试", "cmd", fail))
        result = r._confirm_risk("owner", code)
        self.assertIn("执行失败", result)
        entries = [json.loads(x) for x in self.audit.read_text(encoding="utf-8").splitlines()]
        self.assertIn("failed", entries[-1]["result"])

    def test_command_syntax_error_is_audited_as_rejected(self):
        code = self._code(r._request_risk(
            "owner", "测试", "cmd bad", lambda: "[控制台返回] bad\nIncorrect argument for command"))
        result = r._confirm_risk("owner", code)
        self.assertIn("Incorrect argument", result)
        entries = [json.loads(x) for x in self.audit.read_text(encoding="utf-8").splitlines()]
        self.assertIn("rejected", entries[-1]["result"])
        self.assertIn("Incorrect argument", entries[-1]["result"])

    def test_cmd_canonicalizes_leading_slash(self):
        text = r.cmd_cmd("/mek radiation removeAll", "owner")
        code = self._code(text)
        self.assertEqual(r._PENDING_RISK[code]["command"], "cmd mek radiation removeAll")

    def test_cmd_preserves_worldedit_double_slash(self):
        text = r.cmd_cmd('//set stone', 'owner')
        code = self._code(text)
        self.assertEqual(r._PENDING_RISK[code]['command'], 'cmd //set stone')

    def test_pending_confirmation_can_resolve_unique_code_for_natural_reply(self):
        code = self._code(r._request_risk('owner', '测试', 'cmd list', lambda: 'ok'))
        self.assertEqual(code, r.pending_confirmation('owner')['code'])
        self.assertIsNone(r.pending_confirmation('other'))

    def test_expired_code_cannot_execute(self):
        called = []
        code = self._code(r._request_risk("owner", "测试", "stop", lambda: called.append(1)))
        r._PENDING_RISK[code]["expires"] = time.time() - 1
        self.assertIn("已过期", r._confirm_risk("owner", code))
        self.assertEqual(called, [])

    def test_routed_query_and_confirmation_stay_on_original_server(self):
        calls = []

        def legacy(command):
            calls.append(('legacy', command))
            return 'ok'

        def nast(command):
            calls.append(('secondary', command))
            return 'ok'

        legacy.server_id = 'legacy'
        nast.server_id = 'secondary'
        text = r.dispatch('restart', 'n', 'owner', True, query_fn=legacy)
        code = self._code(text)
        denied = r.dispatch('确认 ' + code, 'n', 'owner', True, query_fn=nast)
        self.assertIn('不一致', denied)
        self.assertEqual([], calls)
        result = r.dispatch('确认 ' + code, 'n', 'owner', True, query_fn=legacy)
        self.assertIn('已安全发送', result)
        self.assertEqual([('legacy', 'stop')], calls)


class BackupSemanticsTests(unittest.TestCase):
    def test_backup_start_is_only_accepted_signal(self):
        fake_log = mock.mock_open()
        with mock.patch.object(r.subprocess, "Popen") as popen, \
             mock.patch.object(Path, "open", fake_log):
            text = r._cmd_backup()
        self.assertIn("已开始", text)
        self.assertIn("完成或失败后会自动回群通知", text)
        popen.assert_called_once()
        fake_log().close.assert_called_once()
        self.assertNotIn("备份完成", text)

    def test_backup_spawn_failure_is_reported(self):
        fake_log = mock.mock_open()
        with mock.patch.object(r.subprocess, "Popen", side_effect=OSError("nope")), \
             mock.patch.object(Path, "open", fake_log):
            text = r._cmd_backup()
        self.assertIn("无法启动", text)
        self.assertNotIn("备份完成", text)
        fake_log().close.assert_called_once()


class BridgeRoutingPureTests(unittest.TestCase):
    def setUp(self):
        b._dup_ring.clear()

    def test_normalize_and_routes(self):
        self.assertEqual(b._normalize("！wiki [CQ:at,qq=1] ae2"), "!wiki  ae2")
        self.assertEqual(b._match_query("！ＴＰＳ"), "tps")
        self.assertEqual(b._match_wiki("!百科 ae2"), "ae2")
        self.assertEqual(b._match_mod("!mod mekanism"), "mekanism")
        self.assertEqual(b._match_cf("!cf create"), "create")
        self.assertEqual(b._match_query("!在线 [怀旧]"), "list")

    def test_one_step_operation_target_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'server-selections.json')
            with mock.patch.object(sys.modules['server_registry'], 'SELECTION_PATH', path), \
                    mock.patch.object(sys.modules['server_registry'], 'SELECTION_TTL', 0):
                registry = sys.modules['server_registry']
                registry._SELECTIONS.clear()
                registry._SELECTIONS_LOADED_FROM = None
                reply = b._selection_reply('!怀旧', 'g', 'u')
                self.assertIn('[怀旧]', reply)
                selected, error = registry.get_selected('g', 'u')
                self.assertEqual('legacy', selected['id'])
                self.assertIsNone(error)
                self.assertIn('普通群消息仍同步两服', b._selection_reply('!服', 'g', 'u'))
                self.assertIn('已清除', b._selection_reply('!服 自动', 'g', 'u'))
                self.assertIsNone(b._selection_reply('!wiki ae2', 'g', 'u'))

    def test_explicit_server_selector_has_priority(self):
        target, command, explicit, error = b._command_target('[怀旧] !restart', 'g', 'u')
        self.assertEqual('legacy', target['id'])
        self.assertEqual('!restart', command)
        self.assertTrue(explicit)
        self.assertEqual('', error)

    def test_html_escaped_server_selector_routes_cmd(self):
        raw = ' &#91;怀旧&#93; !cmd sophisticatedbackpacks list ExamplePlayer'
        target, command, explicit, error = b._command_target(raw, 'g', 'u')
        self.assertEqual('legacy', target['id'])
        self.assertEqual('!cmd sophisticatedbackpacks list ExamplePlayer', command)
        self.assertTrue(explicit)
        self.assertEqual('', error)
        self.assertTrue(r.is_known_command(command.lstrip('!')))

    def test_natural_server_selector_only_accepts_enabled_server(self):
        registry = sys.modules['server_registry']
        target, cleaned = registry.extract_natural_server_selector(
            '[CQ:at,qq=1002] 早苗，怀旧服给 ExamplePlayer OP')
        self.assertEqual('legacy', target['id'])
        self.assertNotIn('怀旧服', cleaned)
        target, _ = registry.extract_natural_server_selector('早苗，副服查一下 tps')
        self.assertIsNone(target)

    def test_single_enabled_server_is_unambiguous_for_natural_ops(self):
        with mock.patch.object(b, 'get_selected', return_value=(None, '未设置')):
            target, cleaned, unambiguous = b._ai_operation_target(
                '[CQ:at,qq=1002] 给AnotherPlayer OP', 'g', 'u')
        self.assertEqual('legacy', target['id'])
        self.assertIn('AnotherPlayer', cleaned)
        self.assertTrue(unambiguous)

    def test_rcon_dispatch_uses_per_server_backend(self):
        calls = []

        def backend(command):
            calls.append(command)
            return 'There are 0 of a max of 20 players online:'

        reply = r.dispatch('list', 'n', 'u', False, query_fn=backend)
        self.assertIn('[在线]', reply)
        self.assertEqual(['list'], calls)

    def test_production_server_context_routes_and_formats_address(self):
        target = b.resolve_server('怀旧')
        with mock.patch.object(r, 'query_server', return_value=(
                'There are 0 of a max of 20 players online:')) as routed:
            reply = r.dispatch('list', 'n', 'u', False, server=target)
            address = r.dispatch('ip', 'n', 'u', False, server=target)
        routed.assert_called_once_with(target, 'list')
        self.assertIn('[在线]', reply)
        self.assertIn(target['name'], address)

    def test_wiki_result_uses_existing_mod_lookup_links(self):
        result = (
            "RF工具：建造机（RFTools Builder）",
            "MC百科简介",
            "https://www.mcmod.cn/class/4561.html",
        )
        hit = {
            "slug": "rftools-builder",
            "title": "RFTools Builder",
            "description": "RFTools addon mod adding the builder, shield system and much more",
            "link": "https://modrinth.com/mod/rftools-builder",
        }
        cf = {"link": "https://www.curseforge.com/minecraft/mc-mods/rftools-builder"}
        with mock.patch.object(b._mod_lookup, "search_modrinth", return_value=[hit]) as modrinth, \
             mock.patch.object(b._mod_lookup, "search_curseforge", return_value=cf), \
             mock.patch.object(b._mod_lookup, "translate", return_value="添加建造机、护盾系统等内容的 RFTools 附属模组。"):
            text = b._format_wiki_result("rftools builder", result)
        self.assertEqual(modrinth.call_count, 1)
        self.assertIn("【模组百科】RF工具：建造机（RFTools Builder）", text)
        self.assertIn("简介：添加建造机、护盾系统等内容的 RFTools 附属模组。", text)
        self.assertIn("MC百科：https://www.mcmod.cn/class/4561.html", text)
        self.assertIn("CurseForge：https://www.curseforge.com/minecraft/mc-mods/rftools-builder", text)
        self.assertIn("Modrinth：https://modrinth.com/mod/rftools-builder", text)

    def test_duplicate_window(self):
        self.assertFalse(b._is_duplicate("hello"))
        self.assertTrue(b._is_duplicate(" hello "))

    def test_admin_command_permission_gate(self):
        with mock.patch.object(r, "cmd_tps") as tps:
            self.assertIsNone(r.dispatch("tps", "n", "u", False))
            tps.assert_not_called()

    def test_fun_mute_classifier_catches_similar_behaviour_without_model(self):
        for text in (
                '来点片', '来点涩图', '来点人妻', '好久没发片了都',
                '来点淫秽色情', '来点淫秽涩情', '来点涩情',
                '给我色情内容', '来点黄色废料', '搞点少儿不宜的',
                '早苗，帮我生成群主黄文', '早苗，帮我骚扰群主'):
            self.assertTrue(b._fun_mute_intent(text), text)

    def test_fun_mute_classifier_avoids_benign_similar_phrases(self):
        for text in (
                '来点地狱笑话', '来张奶龙表情包', '来点碎片', '来点',
                '群主是不是萝莉', '讨论关键词“来点片”',
                '禁止色情内容', '为什么要过滤淫秽内容', '讨论色情过滤'):
            self.assertEqual('', b._fun_mute_intent(text), text)

    def test_fun_mute_is_scoped_to_group_and_ordinary_members(self):
        event = {'group_id': 1001, 'user_id': 1004,
                 'raw_message': '来点片'}
        self.assertTrue(b._fun_mute_reason(event))
        self.assertTrue(b._fun_mute_reason({**event, 'user_id': 20001}))
        self.assertEqual('', b._fun_mute_reason({**event, 'group_id': 1}))
        self.assertEqual('', b._fun_mute_reason({**event, 'user_id': b.WIKI_BOT_QQ}))
        with mock.patch.object(b, 'OWNERS', {'20002'}), \
                mock.patch.object(b, 'ADMINS', {'20003'}):
            self.assertEqual('', b._fun_mute_reason({**event, 'user_id': 20002}))
            self.assertEqual('', b._fun_mute_reason({**event, 'user_id': 20003}))


if __name__ == "__main__":
    unittest.main()
