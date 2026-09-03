#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Thin deterministic QQ command adapter for dual-server ops artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from backup_verify import format_backup_verification
from mod_inventory import format_inventory
from ops_contract import append_jsonl
from ops_telemetry import OpsTelemetry
from recipe_index import format_recipe, query_recipes, render_recipe_png
from server_registry import extract_server_selector, get_selected, server_hint, server_prefix


STATE_BASE = Path(os.environ.get(
    "OPS_STATE_BASE", str(Path(__file__).with_name("state") / "ops")))
IMAGE_WSL_DIR = Path(os.environ.get(
    "OPS_RECIPE_IMAGE_DIR", str(Path(__file__).with_name("state") / "ops-images")))
IMAGE_WINDOWS_DIR = os.environ.get(
    "OPS_RECIPE_IMAGE_WINDOWS_DIR", str(Path(__file__).with_name("state") / "ops-images"))


@dataclass
class OpsReply:
    text: str
    image_path: str = ""


_COMMANDS = (
    ("mods", re.compile(r"^[!！](?:mods|modlist|模组清单|模组列表)\s*$", re.I)),
    ("errors", re.compile(r"^[!！](?:错误摘要|错误指纹|errors?)(?:\s+(1h|6h|24h|7d))?\s*$", re.I)),
    ("lag", re.compile(r"^[!！](?:卡顿取证|lag)(?:\s+(.+))?\s*$", re.I)),
    ("backup", re.compile(r"^[!！](?:验备份|验证备份)(?:\s+(deep|deep\d+|\d+))?\s*$", re.I)),
    ("timeline", re.compile(r"^[!！](?:时间线|timeline)(?:\s+(1h|6h|24h|7d))?\s*$", re.I)),
    ("postmortem", re.compile(r"^[!！](?:复盘|事故复盘|postmortem)(?:\s+(1h|6h|24h|7d))?\s*$", re.I)),
    ("weekly", re.compile(r"^[!！](?:周报|运行报告|weekly)\s*$", re.I)),
    ("recipe", re.compile(r"^[!！](?:配方|recipe)\s+(.+?)\s*$", re.I | re.S)),
)


def match_ops_command(raw):
    server, cleaned = extract_server_selector(raw)
    for name, pattern in _COMMANDS:
        match = pattern.match(cleaned)
        if match:
            return {"name": name, "server": server, "match": match, "cleaned": cleaned}
    return None


def _read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def _selected(command, group_id, user_id):
    if command.get("server"):
        return command["server"], ""
    server, error = get_selected(group_id, user_id)
    return server, error or ""


def _telemetry(server, query_fn=None):
    server_dir = server.get("server_dir") or "/nonexistent/remote/" + server["id"]
    wrapped = (lambda command: query_fn(server, command)) if query_fn else None
    return OpsTelemetry(server["id"], server_prefix(server), server_dir,
                        state_base=STATE_BASE, query_fn=wrapped)


def _audit(name, server, user_id, state="success", detail=""):
    actor = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:16]
    append_jsonl(STATE_BASE / "ops-command-audit.jsonl", {
        "timestamp": time.time(), "command": name, "server": server.get("id"),
        "actor_hash": actor, "state": state, "detail": str(detail)[:160],
    })


def _error_summary(telemetry, window):
    data = telemetry.timeline(window)
    errors = [row for row in data.get("events") or [] if row.get("kind") == "error"]
    lines = ["%s 错误摘要（%s）：%d 个告警" % (telemetry.prefix, window, len(errors))]
    for row in errors[-8:]:
        stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(float(row.get("timestamp") or 0)))
        lines.append("- %s｜%s" % (stamp, str(row.get("text") or "")[:180]))
    if not errors:
        lines.append("窗口内没有新错误指纹告警。")
    return "\n".join(lines)


def _image_paths(server_id, recipe_id):
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", "%s-%s" % (server_id, recipe_id))[:120]
    name = "recipe-%s-%d.png" % (safe, int(time.time()))
    return IMAGE_WSL_DIR / name, IMAGE_WINDOWS_DIR.rstrip("\\/") + "\\" + name


def dispatch_ops_command(raw, group_id, user_id, privileged, query_fn=None):
    command = match_ops_command(raw)
    if not command:
        return None
    server, selection_error = _selected(command, group_id, user_id)
    if not server:
        return OpsReply("%s 拒绝：%s。请在命令中指定已注册的服务器前缀。" %
                        (server_hint(), selection_error or "未指定服务器"))
    name = command["name"]
    public = name in ("mods", "recipe", "weekly")
    if not public and not privileged:
        _audit(name, server, user_id, "denied", "admin required")
        return OpsReply("%s 拒绝：该运维查询仅群主/管理员可用。" % server_prefix(server))
    telemetry = _telemetry(server, query_fn=query_fn)
    try:
        if name == "mods":
            data = telemetry.inventory()
            if not data:
                return OpsReply("%s 模组索引尚未生成，请稍后再试。" % telemetry.prefix)
            reply = OpsReply(format_inventory(data))
        elif name == "errors":
            window = command["match"].group(1) or "6h"
            reply = OpsReply(_error_summary(telemetry, window))
        elif name == "lag":
            if server.get("server_dir") and Path(server["server_dir"]).is_dir():
                data = telemetry.collect_lag_evidence("qq-readonly")
            else:
                data = _read(telemetry.root / "lag-latest.json")
            reply = OpsReply(telemetry.format_lag(data) if data else
                             "%s 尚无卡顿取证快照；等待本机采集器同步。" % telemetry.prefix)
        elif name == "backup":
            arg = (command["match"].group(1) or "1").casefold()
            count_match = re.search(r"\d+", arg)
            count = min(5, int(count_match.group(0))) if count_match else 1
            deep = arg.startswith("deep")
            if server.get("server_dir") and Path(server["server_dir"]).is_dir():
                data = telemetry.verify_backups(count=count, deep=deep)
            else:
                data = _read(telemetry.root / "backup-verify.json")
            reply = OpsReply(format_backup_verification(data, telemetry.prefix) if data else
                             "%s 尚无备份验证快照；等待本机采集器同步。" % telemetry.prefix)
        elif name == "timeline":
            window = command["match"].group(1) or "6h"
            reply = OpsReply(telemetry.format_timeline(telemetry.timeline(window)))
        elif name == "postmortem":
            window = command["match"].group(1) or "24h"
            reply = OpsReply(telemetry.format_postmortem(telemetry.incident_postmortem(window)))
        elif name == "weekly":
            reply = OpsReply(telemetry.format_weekly(telemetry.weekly_report()))
        elif name == "recipe":
            query = command["match"].group(1).strip()
            index = telemetry.recipes()
            rows = query_recipes(index, query, limit=4) if index else []
            if not rows:
                reply = OpsReply("%s 配方索引里没找到「%s」。可尝试中文名或物品 ID。" %
                                 (telemetry.prefix, query[:80]))
            else:
                local_path, windows_path = _image_paths(server["id"], rows[0].get("id", "recipe"))
                render_recipe_png(index, rows[0], local_path)
                text = format_recipe(index, rows[0], telemetry.prefix)
                if len(rows) > 1:
                    text += "\n另有 %d 个匹配配方，当前展示首项。" % (len(rows) - 1)
                reply = OpsReply(text, windows_path)
        else:
            return None
        _audit(name, server, user_id)
        return reply
    except Exception as exc:
        _audit(name, server, user_id, "failed", type(exc).__name__)
        return OpsReply("%s %s暂不可用：%s。详情已写入审计日志。" %
                        (telemetry.prefix, name, type(exc).__name__))
