"""Offline account-switch regressions; never import the bridge or AI runtime."""
import ast
import contextlib
import importlib
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import types
import unittest
import urllib.request
from unittest import mock
from onebot_utf8 import onebot_headers


ROOT = Path(__file__).resolve().parents[1]
OLD_QQ = "1004"
PREVIOUS_QQ = "1005"
NEW_QQ = "1002"
OTHER_QQ = "2002"
HUMAN_QQ = "2001"
GROUP_ID = 1001
NOW = 1_800_000_000
ISOLATED_MODULES = (
    "sanae_identity", "social_lite", "conversation_memory",
    "expression_memory", "chat_recall", "shop_query", "reply_lessons", "silent_vision",
)


def source_nodes(filename, names=(), methods=()):
    """Select actual gate/constructor source without top-level runtime effects."""
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8-sig"))
    selected = []
    found = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "sanae_identity":
            selected.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            selected.append(node)
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            assigned = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if assigned & set(names):
                selected.append(node)
                found.update(assigned & set(names))
        elif isinstance(node, ast.ClassDef):
            for method in node.body:
                if isinstance(method, ast.FunctionDef) and (node.name, method.name) in methods:
                    selected.append(method)
                    found.add(method.name)
    expected = set(names) | {name for _, name in methods}
    if found != expected:
        raise AssertionError(f"Missing selected source in {filename}: {expected - found}")
    return ast.Module(body=selected, type_ignores=[])


def load_source(filename, names=(), methods=(), **namespace):
    namespace.setdefault("re", re)
    namespace.setdefault("onebot_headers", onebot_headers)
    exec(compile(source_nodes(filename, names, methods), str(ROOT / filename), "exec"), namespace)
    return namespace


def event(text="一起聊聊这件事", uid=HUMAN_QQ, gid=GROUP_ID, **extra):
    value = {
        "post_type": "message", "message_type": "group", "group_id": gid,
        "user_id": uid, "self_id": NEW_QQ, "message_id": "501",
        "time": NOW, "raw_message": text, "sender": {"nickname": "测试成员"},
    }
    value.update(extra)
    return value


class AccountIdentityTests(unittest.TestCase):
    def setUp(self):
        stack = self.stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.temp = Path(stack.enter_context(tempfile.TemporaryDirectory(
            prefix="account-identity-", dir=ROOT.parent)))
        stack.enter_context(mock.patch.dict(os.environ, {
            "SANAE_QQ": NEW_QQ,
            "SANAE_BOT_QQ": NEW_QQ,
            "SANAE_HISTORICAL_BOT_QQ": ",".join((OLD_QQ, PREVIOUS_QQ, NEW_QQ)),
            "SCE_BOT_TEST_MODE": "1",
            "SCE_SERVER_REGISTRY": str(self.temp / "empty-registry.json"),
            "CONVERSATION_MEMORY_PATH": str(self.temp / "conversation.json"),
            "EXPRESSION_MEMORY_PATH": str(self.temp / "expression.json"),
            "REPLY_LESSONS_PATH": str(self.temp / "lessons.json"),
        }))
        stack.enter_context(mock.patch.object(sys, "path", [str(ROOT), *sys.path]))
        stack.enter_context(mock.patch.dict(sys.modules))
        for name in ISOLATED_MODULES:
            sys.modules.pop(name, None)
        for owner, attribute in (
            (socket.socket, "connect"), (socket, "create_connection"),
            (urllib.request, "urlopen"),
        ):
            stack.enter_context(mock.patch.object(owner, attribute,
                side_effect=AssertionError("Account tests must stay offline")))
        self.identity = importlib.import_module("sanae_identity")

    def gates(self):
        bridge = load_source("llbot_bridge.py", names=(
            "WIKI_BOT_QQ", "_event_mentions_sanae", "_event_direct_name_call", "_ai_reply_trigger"),
            OWNERS=set(), ADMINS=set(), SANAE_NAME_CALL_ENABLED=False, SANAE_MENTION_ONLY=True)
        ai = load_source("sanae_ai.py", names=("AT_PATTERN", "is_at_sanae"))
        return bridge, ai

    def test_identity_current_default_and_configured_accounts(self):
        for configured in (None, OLD_QQ, PREVIOUS_QQ, NEW_QQ, OTHER_QQ):
            with self.subTest(configured=configured), mock.patch.dict(os.environ), mock.patch.dict(sys.modules):
                sys.modules.pop("sanae_identity", None)
                if configured is None:
                    os.environ.pop("SANAE_QQ", None)
                else:
                    os.environ["SANAE_QQ"] = configured
                identity = importlib.import_module("sanae_identity")
                current = configured or NEW_QQ
                self.assertEqual(identity.SANAE_QQ, current)
                self.assertEqual(set(identity.HISTORICAL_BOT_QQ), {OLD_QQ, PREVIOUS_QQ, NEW_QQ, current})
                bridge, ai = self.gates()
                self.assertEqual(bridge["WIKI_BOT_QQ"], current)
                raw = f"[CQ:at,qq={current}]一起聊聊这件事"
                incoming = event(raw)
                incoming.pop("self_id")
                self.assertEqual(bridge["_ai_reply_trigger"](incoming), "mention-self")
                self.assertTrue(ai["is_at_sanae"](raw))

    def test_new_mentions_pass_both_bridge_and_ai_old_other_all_do_not(self):
        bridge, ai = self.gates()
        for recipient in (NEW_QQ, OLD_QQ, PREVIOUS_QQ, OTHER_QQ, "all", NEW_QQ + "0"):
            for suffix in ("", ",name=测试账号"):
                with self.subTest(recipient=recipient, suffix=suffix):
                    raw = f"[CQ:at,qq={recipient}{suffix}]一起聊聊这件事"
                    expected = recipient == NEW_QQ
                    incoming = event(raw)
                    self.assertEqual(bridge["_event_mentions_sanae"](incoming), expected)
                    self.assertEqual(bool(bridge["_ai_reply_trigger"](incoming)), expected)
                    self.assertEqual(ai["is_at_sanae"](raw), expected)

    def test_bridge_segments_are_authoritative_and_self_id_remains_dynamic(self):
        bridge, _ = self.gates()
        for recipient in (NEW_QQ, int(NEW_QQ), OLD_QQ, PREVIOUS_QQ, OTHER_QQ, "all"):
            with self.subTest(recipient=recipient):
                incoming = event(f"[CQ:at,qq={NEW_QQ}]一起聊聊这件事", message=[
                    {"type": "at", "data": {"qq": recipient}},
                    {"type": "text", "data": {"text": "一起聊聊这件事"}},
                ])
                self.assertEqual(bool(bridge["_ai_reply_trigger"](incoming)), str(recipient) == NEW_QQ)
        quoted = event(f"[CQ:at,qq={NEW_QQ}]", message=[
            {"type": "text", "data": {"text": f"[CQ:at,qq={NEW_QQ}]"}}])
        self.assertFalse(bridge["_event_mentions_sanae"](quoted))
        dynamic = event(f"[CQ:at,qq={OTHER_QQ}]一起聊聊这件事", self_id=int(OTHER_QQ))
        self.assertEqual(bridge["_ai_reply_trigger"](dynamic), "mention-self")

    def test_bridge_drops_historical_and_current_bot_events(self):
        start_worker = mock.Mock()
        handler = types.SimpleNamespace(_forward_private=mock.Mock())
        bridge = load_source("llbot_bridge.py", methods=(("BridgeHandler", "_process_event"),),
            REPLY_GROUP=mock.Mock(), LLBOT_GROUP=GROUP_ID, SOCIAL_LITE_GROUP=GROUP_ID,
            _shop_query=types.SimpleNamespace(is_query=lambda _: False),
            _bridge_group_enabled=lambda _: True, _start_worker=start_worker, print=mock.Mock(),
            PRIVATE_FORWARD_ENABLED=True)
        for message_type in ("private", "group"):
            for uid, self_id in ((OLD_QQ, NEW_QQ), (PREVIOUS_QQ, NEW_QQ), (int(NEW_QQ), NEW_QQ),
                                 (NEW_QQ, int(NEW_QQ)), (OTHER_QQ, int(OTHER_QQ))):
                with self.subTest(message_type=message_type, uid=uid, self_id=self_id):
                    bridge["_process_event"](handler, event(uid=uid, self_id=self_id,
                                                            message_type=message_type))
                    start_worker.assert_not_called()
        incoming = event(message_type="private")
        bridge["_process_event"](handler, incoming)
        start_worker.assert_called_once_with(handler._forward_private, args=(incoming,))
        # Production switches forwarding off: private messages are then ignored.
        start_worker.reset_mock()
        bridge["PRIVATE_FORWARD_ENABLED"] = False
        bridge["_process_event"](handler, incoming)
        start_worker.assert_not_called()

    def test_social_mentions_replies_and_self_filter_follow_instance_identity(self):
        SocialLite = importlib.import_module("social_lite").SocialLite
        default = SocialLite(self.temp / "default.json", self.temp / "default.log")
        self.assertEqual(default.self_id, NEW_QQ)
        for current in (NEW_QQ, OTHER_QQ):
            with self.subTest(current=current):
                social = SocialLite(self.temp / f"{current}.json", self.temp / f"{current}.log",
                                    self_id=current, ambient_reply_weight=0)
                for recipient in (current, OLD_QQ, PREVIOUS_QQ, "12345", "all", current + "0"):
                    for quoted in (False, True):
                        with self.subTest(recipient=recipient, quoted=quoted):
                            incoming = (event(reply={"user_id": recipient}) if quoted else
                                        event(f"[CQ:at,qq={recipient}]一起聊聊这件事"))
                            self.assertEqual(social.is_explicit_interaction(incoming), recipient == current)
                            self.assertEqual(social.should_reply(incoming)[0], recipient == current)
                self.assertEqual(social.should_reply(event(
                    f"[CQ:at,qq={current}]一起聊聊这件事", uid=int(current))), (False, "self"))
                social.record_incoming(event("我来接这句话", uid=int(current)))
                social.record_outgoing(GROUP_ID, "已经记下这条消息")
                recent = social._state[str(GROUP_ID)]["recent"]
                self.assertTrue(all(row["isSelf"] for row in recent))
                self.assertTrue(all(row["userId"] == current for row in recent))

    def test_bridge_memory_wiring_rejects_both_bot_accounts_and_dynamic_self(self):
        ConversationMemory = importlib.import_module("conversation_memory").ConversationMemory
        ExpressionMemory = importlib.import_module("expression_memory").ExpressionMemory
        wired = load_source("llbot_bridge.py", names=("CONVERSATION_MEMORY", "EXPRESSION_MEMORY"),
            _os=os, _BRIDGE_DIR=str(self.temp), SOCIAL_LITE_GROUP=GROUP_ID,
            _default_path=lambda *parts: os.path.join(str(self.temp), *parts),
            ConversationMemory=ConversationMemory, ExpressionMemory=ExpressionMemory)
        for name in ("CONVERSATION_MEMORY", "EXPRESSION_MEMORY"):
            memory = wired[name]
            with self.subTest(memory=name):
                self.assertEqual(memory.bot_user_ids, {OLD_QQ, PREVIOUS_QQ, NEW_QQ})
                for uid in (OLD_QQ, int(OLD_QQ), PREVIOUS_QQ, int(PREVIOUS_QQ), NEW_QQ, int(NEW_QQ), OTHER_QQ):
                    self.assertFalse(memory.observe(event("我喜欢咖啡", uid=uid, self_id=OTHER_QQ)))
                self.assertFalse(memory.path.exists())
                self.assertTrue(memory.observe(event("我喜欢咖啡", self_id=OTHER_QQ)))
                self.assertFalse(memory.observe(event("我喜欢咖啡", gid=100, self_id=OTHER_QQ)))

    def test_chat_recall_excludes_old_new_and_explicit_self_from_evidence(self):
        recall = importlib.import_module("chat_recall")
        rows = [event(text, uid=uid, message_id=str(index), time=NOW - index)
                for index, (uid, text) in enumerate((
                    (OLD_QQ, "旧机器人独有证据"), (PREVIOUS_QQ, "上一机器人独有证据"),
                    (int(NEW_QQ), "新机器人独有证据"),
                    (OTHER_QQ, "传入身份独有证据"), (HUMAN_QQ, "群友真实发言咖啡"),
                ), start=1)]
        # LLBot history may supply sender.user_id instead of a top-level user_id.
        rows[0]["sender"]["user_id"] = rows[0].pop("user_id")
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                kwargs = {"self_id": int(OTHER_QQ)} if explicit else {}
                result = recall.evidence(rows, GROUP_ID, HUMAN_QQ, "总结群聊", now=NOW, **kwargs)
                self.assertEqual(result["stats"]["sample_messages"], 1 if explicit else 2)
                self.assertEqual(result["stats"]["own_messages"], 1)
                self.assertIn("群友真实发言咖啡", result["context"])
                self.assertNotIn("旧机器人独有证据", result["context"])
                self.assertNotIn("上一机器人独有证据", result["context"])
                self.assertNotIn("新机器人独有证据", result["context"])
                self.assertEqual("传入身份独有证据" in result["context"], not explicit)

    def test_reply_lessons_wiring_rejects_all_bot_identities_but_keeps_human_feedback(self):
        ReplyLessons = importlib.import_module("reply_lessons").ReplyLessons
        wired = load_source("llbot_bridge.py", names=("REPLY_LESSONS",),
            _os=os, _BRIDGE_DIR=str(self.temp), SOCIAL_LITE_GROUP=GROUP_ID,
            _default_path=lambda *parts: os.path.join(str(self.temp), *parts),
            ReplyLessons=lambda *args, **kwargs: ReplyLessons(*args, **kwargs, clock=lambda: NOW))
        lessons = wired["REPLY_LESSONS"]
        self.assertEqual(lessons.bot_user_ids, {OLD_QQ, PREVIOUS_QQ, NEW_QQ})
        for uid in (OLD_QQ, int(OLD_QQ), PREVIOUS_QQ, int(PREVIOUS_QQ),
                    NEW_QQ, int(NEW_QQ), OTHER_QQ):
            with self.subTest(uid=uid):
                incoming = event("你是不是又嘴硬了？", uid=uid, self_id=OTHER_QQ)
                self.assertIsNone(lessons._event(incoming))
                self.assertFalse(lessons.record_reply(incoming, "你看得倒挺准。"))
                self.assertFalse(lessons.observe(event("这句接得好", uid=uid, self_id=OTHER_QQ)))
        self.assertFalse(lessons.path.exists())
        self.assertTrue(lessons.record_reply(event("你是不是又嘴硬了？"),
                                            "你看得倒挺准。", reply_message_id=601))
        self.assertTrue(lessons.observe(event("这句接得好", message_id=502)))
        self.assertIsNotNone(lessons.prepare_batch(GROUP_ID))

    def test_silent_vision_wiring_rejects_all_bot_images_without_observer_calls(self):
        SilentVision = importlib.import_module("silent_vision").SilentVision
        observer = mock.Mock(return_value={"description": "案板上的肉。", "sha256": "a" * 64})
        wired = load_source("llbot_bridge.py", names=("SILENT_VISION",),
            SOCIAL_LITE_GROUP=GROUP_ID, WIKI_API="http://unused.invalid",
            _sanae_ai=types.SimpleNamespace(observe_silent_image=observer),
            SilentVision=lambda *args, **kwargs: SilentVision(*args, **kwargs, clock=lambda: NOW))
        vision = wired["SILENT_VISION"]
        self.addCleanup(vision.close)
        self.assertEqual(vision.bot_user_ids, {OLD_QQ, PREVIOUS_QQ, NEW_QQ})
        for uid in (OLD_QQ, int(OLD_QQ), PREVIOUS_QQ, int(PREVIOUS_QQ),
                    NEW_QQ, int(NEW_QQ), OTHER_QQ):
            with self.subTest(uid=uid):
                incoming = event(uid=uid, self_id=OTHER_QQ,
                    message=[{"type": "image", "data": {"file": "fixture-image.jpg"}}])
                self.assertFalse(vision.observe(incoming))
        observer.assert_not_called()
        self.assertEqual(vision.status()["groups"], {})
        # A real member still enters the same production constructor wiring.
        self.assertTrue(vision.observe(event(message=[
            {"type": "image", "data": {"file": "fixture-image.jpg"}}])))
        self.assertEqual(vision.status()["groups"][str(GROUP_ID)]["entries"], 1)

    def test_folded_answers_and_inventory_use_current_identity(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"status":"ok","retcode":0,"data":{}}'
        runtime = load_source("llbot_bridge.py", names=(
            "WIKI_BOT_QQ", "_send_folded_ai_text", "_send_inventory_reply"),
            urllib=types.SimpleNamespace(request=urllib.request),
            json=json, json_bytes=lambda value: json.dumps(value).encode("utf-8"),
            JSON_CONTENT_TYPE="application/json", WIKI_API="http://unused.invalid",
            utf8_text=lambda value: value.decode("utf-8"),
            onebot_success=lambda value: (True, {}), send_group_msg=mock.Mock())
        with mock.patch.object(urllib.request, "urlopen", return_value=response) as opener:
            self.assertTrue(runtime["_send_folded_ai_text"]("第一段。" * 1000, GROUP_ID))
            self.assertTrue(runtime["_send_inventory_reply"](
                types.SimpleNamespace(pages=["模组甲", "模组乙"], page_command="!模组"), GROUP_ID))
        self.assertEqual(opener.call_count, 2)
        for call in opener.call_args_list:
            payload = json.loads(call.args[0].data)
            self.assertEqual(payload["group_id"], GROUP_ID)
            self.assertTrue(payload["messages"])
            self.assertTrue(all(node["data"]["uin"] == NEW_QQ for node in payload["messages"]))

    def test_shop_mentions_and_group_permissions_remain_scoped(self):
        shop = importlib.import_module("shop_query")
        self.assertEqual(shop.BOT_QQ, NEW_QQ)
        config = {"all_alert_group_id": 2101, "group_id": 2102,
                  "priority_group_enabled": True, "query_group_ids": [2101, 2102, GROUP_ID]}
        for recipient in (NEW_QQ, OLD_QQ, PREVIOUS_QQ, OTHER_QQ, "all", NEW_QQ + "0"):
            for group in (2101, 2102, GROUP_ID):
                for structured in (False, True):
                    with self.subTest(recipient=recipient, group=group, structured=structured):
                        incoming = event(f"[CQ:at,qq={recipient}]看看商店", gid=group)
                        if structured:
                            incoming["message"] = [{"type": "at", "data": {"qq": recipient}}]
                        self.assertEqual(shop.is_query(incoming, config=config),
                                         recipient == NEW_QQ and group in (2101, 2102))
        mentioned = f"[CQ:at,qq={NEW_QQ}]看看商店"
        self.assertFalse(shop.is_query(event(mentioned, gid=2102),
                                       config={**config, "priority_group_enabled": False}))
        self.assertFalse(shop.is_query(event(mentioned, gid=2101),
                                       config={**config, "query_group_ids": [2102]}))
        for uid, self_id in ((OLD_QQ, NEW_QQ), (PREVIOUS_QQ, NEW_QQ), (int(NEW_QQ), NEW_QQ), (OTHER_QQ, int(OTHER_QQ))):
            with self.subTest(uid=uid, self_id=self_id):
                self.assertFalse(shop.is_query(event(mentioned, gid=2101, uid=uid,
                                                      self_id=self_id), config=config))
        self.assertFalse(shop.is_query(event(mentioned, gid=2101, message_type="private"), config=config))
        self.assertFalse(shop.is_query(event(mentioned, gid=2101, message=[
            {"type": "text", "data": {"text": mentioned}}]), config=config))

    def test_returned_account_mentions_and_history_filters(self):
        with mock.patch.dict(os.environ, {"SANAE_QQ": PREVIOUS_QQ}), mock.patch.dict(sys.modules):
            for name in ISOLATED_MODULES:
                sys.modules.pop(name, None)
            bridge, ai = self.gates()
            for recipient in (OLD_QQ, PREVIOUS_QQ, NEW_QQ, OTHER_QQ, "all"):
                raw = f"[CQ:at,qq={recipient}]一起聊聊这件事"
                incoming = event(raw, self_id=PREVIOUS_QQ)
                expected = recipient == PREVIOUS_QQ
                self.assertEqual(bool(bridge["_ai_reply_trigger"](incoming)), expected)
                self.assertEqual(ai["is_at_sanae"](raw), expected)
            wired = load_source("llbot_bridge.py", names=("CONVERSATION_MEMORY", "EXPRESSION_MEMORY"),
                _os=os, _BRIDGE_DIR=str(self.temp), SOCIAL_LITE_GROUP=GROUP_ID,
            _default_path=lambda *parts: os.path.join(str(self.temp), *parts),
                ConversationMemory=importlib.import_module("conversation_memory").ConversationMemory,
                ExpressionMemory=importlib.import_module("expression_memory").ExpressionMemory)
            for memory in (wired["CONVERSATION_MEMORY"], wired["EXPRESSION_MEMORY"]):
                self.assertEqual(memory.bot_user_ids, {OLD_QQ, PREVIOUS_QQ, NEW_QQ})
                for uid in (OLD_QQ, PREVIOUS_QQ, NEW_QQ):
                    self.assertFalse(memory.observe(event("我喜欢咖啡", uid=uid, self_id=PREVIOUS_QQ)))
                self.assertFalse(memory.path.exists())
                self.assertTrue(memory.observe(event("我喜欢咖啡", self_id=PREVIOUS_QQ)))

    def test_public_launcher_requires_local_account_configuration(self):
        launcher = (ROOT / "start.example.ps1").read_text(encoding="utf-8-sig")
        config = (ROOT / "examples/env.example.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("env.ps1", launcher)
        self.assertRegex(config, r"(?m)^\s*\$env:SANAE_BOT_QQ\s*=\s*'YOUR_BOT_QQ'\s*$")
        self.assertIn("SANAE_HISTORICAL_BOT_QQ", config)
        for account in (OLD_QQ, PREVIOUS_QQ, NEW_QQ):
            self.assertNotRegex(config, r"['\"]" + account + r"['\"]")



if __name__ == "__main__":
    unittest.main()
