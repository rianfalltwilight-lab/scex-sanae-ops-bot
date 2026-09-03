#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only Minecraft ZIP verification adapted from minecraft-server-ops-kit."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
import zipfile
from pathlib import Path


def _safe_error(exc):
    return "%s: %s" % (type(exc).__name__, str(exc)[:180])


def discover_backup_zips(server_dir, limit=50):
    root = Path(server_dir)
    candidates = (
        root / "backups", root / "simplebackups", root / "SimpleBackups",
        root.parent / "backups", root.parent / "simplebackups",
    )
    seen = set()
    found = []
    for directory in candidates:
        if not directory.is_dir():
            continue
        for path in directory.glob("**/*.zip"):
            try:
                key = str(path.resolve())
                if key in seen or not path.is_file():
                    continue
                seen.add(key)
                found.append(path)
            except OSError:
                continue
    found.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return found[:max(1, int(limit))]


def verify_backup_zip(path, deep=False, sample_count=24):
    path = Path(path)
    result = {
        "path": str(path), "file": path.name, "checked_at": time.time(),
        "size": path.stat().st_size if path.exists() else 0,
        "ok": False, "entries": 0, "level_dat": False, "region_count": 0,
        "playerdata_count": 0, "deep_checked": 0, "deep_failed": 0,
        "errors": [], "warnings": [],
    }
    try:
        with zipfile.ZipFile(str(path)) as archive:
            infos = [item for item in archive.infolist() if not item.is_dir()]
            result["entries"] = len(infos)
            level = [item for item in infos if item.filename.replace("\\", "/").endswith("/level.dat")
                     or item.filename.replace("\\", "/") == "level.dat"]
            regions = [item for item in infos if "/region/" in "/" + item.filename.replace("\\", "/")
                       and item.filename.casefold().endswith(".mca")]
            players = [item for item in infos if "/playerdata/" in "/" + item.filename.replace("\\", "/")
                       and item.filename.casefold().endswith(".dat")]
            result["level_dat"] = bool(level)
            result["region_count"] = len(regions)
            result["playerdata_count"] = len(players)
            if not level:
                result["errors"].append("ZIP 内未找到 level.dat")
            if not regions:
                result["warnings"].append("未找到 region/*.mca")
            if level:
                try:
                    raw = archive.read(level[0])
                    if raw.startswith(b"\x1f\x8b"):
                        unpacked = gzip.decompress(raw)
                        if len(unpacked) < 64:
                            result["errors"].append("level.dat GZIP 内容过短")
                    elif len(raw) < 128:
                        result["errors"].append("level.dat 解压后过短")
                except Exception as exc:
                    result["errors"].append("level.dat 读取失败：" + _safe_error(exc))
            if deep:
                sample = []
                for pool in (level, regions[:8], players[:4], infos[:4], infos[-4:]):
                    for info in pool:
                        if info not in sample:
                            sample.append(info)
                for info in sample[:max(1, int(sample_count))]:
                    try:
                        with archive.open(info) as stream:
                            while stream.read(1024 * 1024):
                                pass
                        result["deep_checked"] += 1
                    except Exception as exc:
                        result["deep_checked"] += 1
                        result["deep_failed"] += 1
                        result["errors"].append("抽样读取失败：%s（%s）" %
                                                (info.filename[:120], _safe_error(exc)))
                bad = archive.testzip()
                if bad:
                    result["deep_failed"] += 1
                    result["errors"].append("CRC 校验失败：" + bad[:160])
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        result["errors"].append(_safe_error(exc))
    result["ok"] = not result["errors"]
    return result


def verify_latest_backups(server_dir, count=1, deep=False):
    zips = discover_backup_zips(server_dir, limit=max(10, count))
    rows = [verify_backup_zip(path, deep=deep) for path in zips[:max(1, int(count))]]
    return {
        "schema": 1, "generated_at": time.time(), "server_dir_hash":
            hashlib.sha256(os.path.abspath(str(server_dir)).encode("utf-8")).hexdigest()[:16],
        "requested": int(count), "deep": bool(deep), "found": len(zips),
        "ok": bool(rows) and all(row.get("ok") for row in rows), "results": rows,
    }


def format_backup_verification(data, prefix="[未知服]"):
    rows = list(data.get("results") or [])
    if not rows:
        return "%s 备份验证：未发现可验证的 ZIP 恢复点。" % prefix
    lines = ["%s 备份验证：%s（%d 个恢复点%s）" % (
        prefix, "通过" if data.get("ok") else "有失败", len(rows),
        "，深度抽样" if data.get("deep") else "")]
    for row in rows[:5]:
        status = "通过" if row.get("ok") else "失败"
        line = "- %s｜%s｜%d 条目｜region %d｜playerdata %d" % (
            row.get("file", "unknown.zip"), status, row.get("entries", 0),
            row.get("region_count", 0), row.get("playerdata_count", 0))
        if row.get("deep_checked"):
            line += "｜抽样 %d/失败 %d" % (row["deep_checked"], row["deep_failed"])
        lines.append(line)
        for error in (row.get("errors") or [])[:2]:
            lines.append("  错误：" + str(error)[:180])
    return "\n".join(lines)


def atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(str(temp), str(path))
