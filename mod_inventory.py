#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only installed-mod inventory for Forge/NeoForge/Fabric servers.

Classification and presentation are adapted from minecraft-server-ops-kit's
QQ mod-list feature. Only local JAR metadata is read; no network or RCON call is
made and uncertain entries stay in the explicit "other" bucket.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import zipfile
from pathlib import Path


CATEGORY_ORDER = (
    "玩法内容", "玩法扩展与兼容", "玩家体验与信息", "性能优化",
    "服务器管理与本服定制", "前置与库", "其他/待识别",
)

_KEYWORDS = {
    "性能优化": (
        "performance", "optimi", "lithium", "ferrite", "modernfix", "spark",
        "memory", "c2me", "servercore", "radium", "starlight", "embeddium",
    ),
    "服务器管理与本服定制": (
        "server", "permission", "claim", "protect", "backup", "bluemap",
        "chunk pregenerator", "watchdog", "admin", "ftb chunks", "luckperms",
    ),
    "玩家体验与信息": (
        "jade", "jei", "rei", "emi", "map", "tooltip", "inventory hud",
        "journeymap", "xaero", "mouse tweaks", "configured", "catalogue",
    ),
    "前置与库": (
        " api", "library", " lib", "core", "framework", "architectury",
        "cloth config", "kotlin", "bookshelf", "citadel", "curios", "geckolib",
        "resourceful", "puzzles lib", "placebo", "moonlight", "balm",
    ),
    "玩法扩展与兼容": (
        "compat", "integration", "addon", "add-on", "expansion", "bridge",
        "tweaks", "upgrade", "support", "联动", "兼容", "扩展",
    ),
    "玩法内容": (
        "magic", "tech", "adventure", "world", "biome", "dimension", "mob",
        "weapon", "armor", "industrial", "mekanism", "create", "thermal",
        "botania", "applied energistics", "探索", "科技", "魔法", "生物",
    ),
}


def _safe_text(value, limit=180):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _toml_value(text, key):
    match = re.search(r"(?mi)^\s*" + re.escape(key) + r"\s*=\s*['\"]([^'\"]+)['\"]", text)
    return _safe_text(match.group(1)) if match else ""


def _toml_dependencies(text, mod_id):
    deps = []
    current = ""
    in_target = False
    for line in text.splitlines():
        section = re.match(r"\s*\[\[dependencies\.([^\]]+)\]\]", line)
        if section:
            current = section.group(1)
            in_target = not mod_id or current == mod_id
            continue
        if not in_target:
            continue
        found = re.match(r"\s*modId\s*=\s*['\"]([^'\"]+)['\"]", line)
        if found:
            dep = _safe_text(found.group(1), 80)
            if dep and dep not in ("minecraft", "forge", "neoforge", "java"):
                deps.append(dep)
    return sorted(set(deps))[:80]


def _manifest(text):
    rows = {}
    current = ""
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith(" ") and current:
            rows[current] += line[1:]
        elif ":" in line:
            key, value = line.split(":", 1)
            current = key.strip()
            rows[current] = value.strip()
    return rows


def read_jar_metadata(path):
    path = Path(path)
    info = {
        "file": path.name, "id": path.stem, "name": path.stem,
        "version": "", "dependencies": [], "side": "unknown", "metadata": "filename",
    }
    try:
        with zipfile.ZipFile(str(path)) as jar:
            names = set(jar.namelist())
            if "fabric.mod.json" in names:
                raw = json.loads(jar.read("fabric.mod.json").decode("utf-8-sig", "replace"))
                info.update({
                    "id": _safe_text(raw.get("id"), 100) or path.stem,
                    "name": _safe_text(raw.get("name")) or path.stem,
                    "version": _safe_text(raw.get("version"), 80),
                    "dependencies": sorted((raw.get("depends") or {}).keys())[:80],
                    "side": _safe_text(raw.get("environment"), 30) or "unknown",
                    "metadata": "fabric.mod.json",
                })
            else:
                toml_name = next((name for name in (
                    "META-INF/neoforge.mods.toml", "META-INF/mods.toml") if name in names), "")
                if toml_name:
                    text = jar.read(toml_name).decode("utf-8-sig", "replace")
                    mod_id = _toml_value(text, "modId") or path.stem
                    info.update({
                        "id": mod_id,
                        "name": _toml_value(text, "displayName") or mod_id,
                        "version": _toml_value(text, "version"),
                        "dependencies": _toml_dependencies(text, mod_id),
                        "side": _toml_value(text, "side") or "unknown",
                        "metadata": toml_name,
                    })
                elif "META-INF/MANIFEST.MF" in names:
                    rows = _manifest(jar.read("META-INF/MANIFEST.MF").decode("utf-8", "replace"))
                    info.update({
                        "name": _safe_text(rows.get("Implementation-Title") or rows.get("Specification-Title")) or path.stem,
                        "version": _safe_text(rows.get("Implementation-Version"), 80),
                        "metadata": "META-INF/MANIFEST.MF",
                    })
    except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        info["metadata_error"] = type(exc).__name__
    return info


def classify_mod(info):
    text = " " + " ".join((info.get("id", ""), info.get("name", ""), info.get("file", ""))).casefold()
    for category in ("性能优化", "服务器管理与本服定制", "玩家体验与信息",
                     "前置与库", "玩法扩展与兼容", "玩法内容"):
        if any(word in text for word in _KEYWORDS[category]):
            return category
    if info.get("dependencies") and any(word in text for word in ("addon", "compat", "integration")):
        return "玩法扩展与兼容"
    return "其他/待识别"


def jar_signature(mods_dir):
    root = Path(mods_dir)
    digest = hashlib.sha256()
    for path in sorted(root.glob("*.jar"), key=lambda item: item.name.casefold()):
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(path.name.encode("utf-8", "replace"))
        digest.update(("%d:%d" % (stat.st_size, int(stat.st_mtime_ns))).encode("ascii"))
    return digest.hexdigest()


def scan_installed_mods(server_dir, server_id="unknown", prefix="[未知服]"):
    root = Path(server_dir)
    mods_dir = root / "mods"
    mods = []
    for path in sorted(mods_dir.glob("*.jar"), key=lambda item: item.name.casefold()):
        item = read_jar_metadata(path)
        item["category"] = classify_mod(item)
        mods.append(item)
    return {
        "schema": 1, "server": server_id, "prefix": prefix,
        "generated_at": time.time(), "signature": jar_signature(mods_dir),
        "count": len(mods), "mods": mods,
    }


def format_inventory(data, max_chars=3600):
    prefix = str(data.get("prefix") or "[未知服]")
    mods = list(data.get("mods") or [])
    grouped = {name: [] for name in CATEGORY_ORDER}
    for item in mods:
        grouped.setdefault(item.get("category") or "其他/待识别", []).append(item)
    lines = ["%s 已安装模组：%d 个" % (prefix, len(mods))]
    for category in CATEGORY_ORDER:
        rows = grouped.get(category) or []
        if not rows:
            continue
        lines.append("\n%s（%d）" % (category, len(rows)))
        for item in rows:
            label = item.get("name") or item.get("id") or item.get("file")
            version = (" " + item["version"]) if item.get("version") else ""
            line = "- %s%s" % (_safe_text(label, 100), _safe_text(version, 50))
            if sum(len(part) + 1 for part in lines) + len(line) > max_chars:
                lines.append("…清单过长，已显示前 %d 项；完整索引保存在运维证据中。" %
                             sum(1 for part in lines if part.startswith("- ")))
                return "\n".join(lines)
            lines.append(line)
    return "\n".join(lines)

