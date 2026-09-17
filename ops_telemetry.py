#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dual-server operational telemetry and read-only report generators.

Concepts are adapted from minecraft-server-ops-kit's error fingerprint, lag
forensics, timeline, incident postmortem and weekly report tools. Collection is
deterministic; interpretation remains suitable for an auditable Codex task.
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import re
import time
import urllib.request
from collections import Counter
from lag_forensics import parse_ticks, coordinator, format_capture
from datetime import datetime
from pathlib import Path

from backup_verify import atomic_write_json as write_json
from backup_verify import format_backup_verification, verify_latest_backups
from mod_inventory import format_inventory, jar_signature, scan_installed_mods
from ops_contract import append_jsonl, normalize_log_fingerprint
from recipe_index import (atomic_write_json as write_recipe_json, build_recipe_index,
                          format_recipe, query_recipes, recipe_signature, render_recipe_png)


WINDOWS = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "week": 604800}
ERROR_LEVEL = re.compile(r"(?:/|\s|\[)(ERROR|FATAL|WARN)(?:\]|\s|:)", re.I)
SOFT_NOISE = re.compile(r"^\s*(?:at\s+[\w.$]+\(|\.\.\.\s+\d+\s+more)", re.I)
CRITICAL_ERROR = re.compile(
    r"\bFATAL\b|Watchdog|OutOfMemoryError|Failed to start (?:the )?(?:minecraft )?server|"
    r"Exception stopping (?:the )?server|Preparing crash report|A single server tick took",
    re.I)
ERROR_FAMILIES = (
    (re.compile(r"DataMapLoader.*Object with ID .*doesn't exist", re.I),
     "datamap_missing", "数据映射引用不存在对象"),
    (re.compile(r"Load My F.*ing Tags|tags are a bit cooked", re.I),
     "broken_tags", "数据包标签不完整（LMFT）"),
    (re.compile(r"snownee\.jade\.Jade|JadeErrorOutput", re.I),
     "jade_provider", "Jade 信息提供器异常"),
    (re.compile(r"Exception caught in connection", re.I),
     "connection_exception", "客户端连接异常"),
    (re.compile(r"Detected setBlock in a far chunk", re.I),
     "far_chunk_setblock", "远区块生成发生 setBlock"),
    (re.compile(r"Block-attached entity at invalid position", re.I),
     "invalid_attachment", "方块附着实体位置无效"),
    (re.compile(r"Cannot get config value before config is loaded|ModConfigEvent\$Reloading", re.I),
     "config_reload_order", "配置热重载时序异常"),
)


def _read_json(path, default=None):
    try:
        with Path(path).open(encoding="utf-8-sig") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


def _read_jsonl(path, since=0.0, limit=20000, max_bytes=8 * 1024 * 1024):
    """Read a bounded recent JSONL tail; operational streams are chronological."""
    rows = []
    try:
        source = Path(path)
        size = source.stat().st_size
        start = max(0, size - max(4096, int(max_bytes)))
        with source.open("rb") as stream:
            stream.seek(start)
            raw = stream.read(size - start)
        if start:
            newline = raw.find(b"\n")
            raw = raw[newline + 1:] if newline >= 0 else b""
        for line in raw.decode("utf-8-sig", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                stamp = float(row.get("timestamp") or row.get("occurred_at") or row.get("time") or 0)
                if stamp >= since:
                    rows.append(row)
                    if len(rows) > limit:
                        rows = rows[-limit:]
    except OSError:
        pass
    return rows


def _window(value):
    return WINDOWS.get(str(value or "6h").casefold(), WINDOWS["6h"])


def _stamp(value):
    return datetime.fromtimestamp(float(value)).astimezone().strftime("%m-%d %H:%M:%S")


def _safe_sample(line):
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b", "<ip>", str(line or ""))
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()[:220]


def _error_family(line, fingerprint):
    text = str(line or "")
    for pattern, key, label in ERROR_FAMILIES:
        if pattern.search(text):
            return key, label
    logger_match = re.search(r"\[[^\]]+/(?:ERROR|FATAL)\]\s+\[([^\]]+)\]", text, re.I)
    if logger_match:
        logger = logger_match.group(1).strip(" /:")[:120]
        normalized = re.sub(r"[^a-z0-9_.:/-]+", "_", logger.casefold())
        short = logger.rsplit(".", 1)[-1].replace("/", " / ")
        return "logger:" + normalized, "%s 记录的其他错误" % short[:90]
    key = "other:" + __import__("hashlib").sha256(
        str(fingerprint).encode("utf-8")).hexdigest()[:16]
    sample = _safe_sample(line)
    sample = re.sub(r"^.*?/(?:\]|\]:)\s*", "", sample)
    return key, (sample[:100] or "其他错误")


def _format_error_digest(prefix, pending, interval):
    rows = sorted(pending.values(), key=lambda row: int(row.get("count", 0)), reverse=True)
    total = sum(int(row.get("count", 0)) for row in rows)
    minutes = max(1, int(interval // 60))
    lines = ["%s 日志聚合摘要（最多每 %d 分钟）：%d 条 ERROR，%d 类" %
             (prefix, minutes, total, len(rows))]
    for row in rows[:5]:
        lines.append("- %d× %s" % (int(row.get("count", 0)), str(row.get("label"))[:120]))
    if len(rows) > 5:
        remaining = sum(int(row.get("count", 0)) for row in rows[5:])
        lines.append("- 其余 %d 条 / %d 类" % (remaining, len(rows) - 5))
    lines.append("详情：!错误摘要 %s 6h" % prefix)
    return "\n".join(lines)


class OpsTelemetry:
    def __init__(self, server_id, prefix, server_dir, state_base=None, query_fn=None):
        self.server_id = str(server_id)
        self.prefix = str(prefix)
        self.server_dir = Path(server_dir)
        base = Path(state_base or Path(__file__).with_name("state") / "ops")
        self.root = base / self.server_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.query_fn = query_fn

    @property
    def fingerprints_path(self):
        return self.root / "error-fingerprints.jsonl"

    def observe_lines(self, lines, now=None, cooldown=3600, digest_interval=21600,
                      digest_min_count=3):
        now = float(time.time() if now is None else now)
        state_path = self.root / "fingerprint-state.json"
        state = _read_json(state_path, {}) or {}
        delivery_path = self.root / "alert-delivery-state.json"
        delivery = _read_json(delivery_path, {}) or {}
        pending = delivery.get("pending") if isinstance(delivery.get("pending"), dict) else {}
        alerts = []
        for line in lines:
            match = ERROR_LEVEL.search(str(line))
            if not match or SOFT_NOISE.match(str(line)):
                continue
            level = match.group(1).upper()
            if level == "WARN":
                continue
            fingerprint = normalize_log_fingerprint(line)
            if not fingerprint:
                continue
            key = __import__("hashlib").sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
            entry = state.get(key) or {"count": 0, "first": now, "last_alert": 0}
            entry["count"] = int(entry.get("count", 0)) + 1
            entry["last"] = now
            first = entry["count"] == 1
            due = now - float(entry.get("last_alert", 0)) >= cooldown
            critical = level == "FATAL" or bool(CRITICAL_ERROR.search(str(line)))
            action = "critical" if critical and (first or due) else "aggregate"
            if critical and (first or due):
                entry["last_alert"] = now
                label = "紧急" if first else "紧急重复 ×%d" % entry["count"]
                alerts.append("%s %s %s｜%s" %
                              (self.prefix, label, level, _safe_sample(line)))
            elif not critical:
                family, family_label = _error_family(line, fingerprint)
                aggregate = pending.get(family) or {"count": 0, "label": family_label,
                                                     "first": now}
                aggregate["count"] = int(aggregate.get("count", 0)) + 1
                aggregate["last"] = now
                aggregate["label"] = family_label
                pending[family] = aggregate
            state[key] = entry
            if action == "critical" or entry["count"] in (1, 2, 4, 8, 16, 32, 64, 128):
                append_jsonl(self.fingerprints_path, {
                    "timestamp": now, "server": self.server_id, "level": level,
                    "fingerprint": fingerprint, "sample": _safe_sample(line),
                    "count": entry["count"], "action": action,
                })
        if pending and not delivery.get("period_start"):
            delivery["period_start"] = now
        if pending and now - float(delivery.get("period_start") or now) >= digest_interval:
            total_pending = sum(int(row.get("count", 0)) for row in pending.values())
            if total_pending >= digest_min_count:
                alerts.append(_format_error_digest(self.prefix, pending, digest_interval))
                delivery["last_digest"] = now
                pending = {}
                delivery["period_start"] = 0
            else:
                # Keep isolated errors for a later aggregate instead of pushing one-off noise.
                delivery["period_start"] = now
        if len(pending) > 300:
            rows = sorted(pending.items(), key=lambda item: item[1].get("last", 0), reverse=True)
            kept = dict(rows[:250])
            overflow = rows[250:]
            kept["overflow"] = {
                "count": sum(int(row.get("count", 0)) for _, row in overflow),
                "label": "其他低频错误", "first": now, "last": now,
            }
            pending = kept
        delivery["pending"] = pending
        # Bound state even if a broken mod emits unbounded unique errors.
        if len(state) > 4000:
            state = dict(sorted(state.items(), key=lambda item: item[1].get("last", 0), reverse=True)[:3000])
        write_json(state_path, state)
        write_json(delivery_path, delivery)
        return alerts

    def record_event(self, event_type, text, occurred_at=None, **details):
        row = {"timestamp": float(occurred_at or time.time()), "server": self.server_id,
               "type": str(event_type), "text": str(text)[:500], "details": details}
        append_jsonl(self.root / "events.jsonl", row)
        return row

    def _query(self, command):
        if not self.query_fn:
            raise RuntimeError("RCON query unavailable")
        from request_runtime import Budget, CURRENT
        budget = Budget(12, parent=CURRENT.get())
        token = CURRENT.set(budget)
        try:
            return str(self.query_fn(command) or "")
        finally:
            CURRENT.reset(token)

    def sample_runtime(self, now=None):
        now = float(now or time.time())
        row = {"timestamp": now, "server": self.server_id, "players": None,
               "max_players": None, "tps": None, "mspt": None, "errors": [], "tick_parser": 2}
        try:
            output = self._query("list")
            match = re.search(r"(?:There are|当前共有)\s*(\d+).*?(?:max of|最多可容纳)\s*(\d+)", output, re.I)
            if match:
                row["players"], row["max_players"] = int(match.group(1)), int(match.group(2))
        except Exception as exc:
            row["errors"].append("list:" + type(exc).__name__)
        try:
            output = self._query("neoforge tps")
            row.update(parse_ticks(output))
        except Exception as exc:
            row["errors"].append("tps:" + type(exc).__name__)
        append_jsonl(self.root / "perf-samples.jsonl", row)
        write_json(self.root / "runtime.json", row)
        return row

    def collect_lag_evidence(self, reason="manual", now=None):
        return coordinator(self).request(reason)

    def format_lag(self, meta):
        if meta.get('schema') == 2:
            return format_capture(meta)
        runtime = meta.get('runtime') or {}
        return '%s 历史简版取证：TPS %s · MSPT %s；未采集线程栈或 Spark。' % (
            self.prefix, runtime.get('tps'), runtime.get('mspt'))

    def refresh_indexes(self, force=False):
        mods_path = self.root / "mod-inventory.json"
        recipe_path = self.root / "recipe-index.json"
        current_mods = _read_json(mods_path, {}) or {}
        current_recipes = _read_json(recipe_path, {}) or {}
        mod_sig = jar_signature(self.server_dir / "mods")
        recipe_sig = recipe_signature(self.server_dir)
        changed = []
        if force or current_mods.get("schema") != 2 or current_mods.get("signature") != mod_sig:
            write_json(mods_path, scan_installed_mods(self.server_dir, self.server_id, self.prefix))
            changed.append("mods")
        if force or current_recipes.get("signature") != recipe_sig:
            write_recipe_json(recipe_path, build_recipe_index(
                self.server_dir, self.server_id, self.prefix))
            changed.append("recipes")
        write_json(self.root / "index-refresh.json", {"timestamp": time.time(), "changed": changed})
        return changed

    def verify_backups(self, count=1, deep=False):
        data = verify_latest_backups(self.server_dir, count=count, deep=deep)
        data.update({"server": self.server_id, "prefix": self.prefix})
        write_json(self.root / "backup-verify.json", data)
        self.record_event("backup_verify", format_backup_verification(data, self.prefix),
                          ok=data.get("ok"), count=len(data.get("results") or []))
        return data

    def periodic_maintenance(self, now=None, sample_interval=60, index_interval=1800,
                             backup_interval=86400, report_interval=3600):
        """Run bounded read-only collectors when their interval expires."""
        now = float(now or time.time())
        state_path = self.root / "maintenance-state.json"
        state = _read_json(state_path, {}) or {}
        actions, errors = [], []

        def due(name, interval):
            return now - float(state.get(name, 0)) >= interval

        if due("sample", sample_interval):
            try:
                sample = self.sample_runtime(now)
                actions.append("sample")
                state["sample"] = now
                if self.server_id == 'legacy':
                    result = coordinator(self).observe(sample)
                    if result and result.get('status') == 'queued':
                        actions.append('lag-queued')
            except Exception as exc:
                errors.append("sample:" + type(exc).__name__)
        if due("index", index_interval):
            try:
                actions.extend("index:" + item for item in self.refresh_indexes())
                state["index"] = now
            except Exception as exc:
                errors.append("index:" + type(exc).__name__)
        if due("backup", backup_interval):
            try:
                self.verify_backups(count=1, deep=False)
                actions.append("backup")
                state["backup"] = now
            except Exception as exc:
                errors.append("backup:" + type(exc).__name__)
        if due("report", report_interval):
            try:
                self.timeline("24h", now)
                self.incident_postmortem("24h", now)
                self.weekly_report(now)
                actions.append("reports")
                state["report"] = now
            except Exception as exc:
                errors.append("report:" + type(exc).__name__)
        state["last_run"] = now
        state["last_actions"] = actions
        state["last_errors"] = errors
        write_json(state_path, state)
        return {"actions": actions, "errors": errors}

    def inventory(self):
        data = _read_json(self.root / "mod-inventory.json", {}) or {}
        if data.get('schema') != 2 and (self.server_dir / 'mods').is_dir():
            data = scan_installed_mods(self.server_dir, self.server_id, self.prefix)
        return data

    def recipes(self):
        return _read_json(self.root / "recipe-index.json", {}) or {}

    def timeline(self, window="6h", now=None):
        now = float(now or time.time())
        since = now - _window(window)
        events = []
        for row in _read_jsonl(self.root / "events.jsonl", since):
            events.append({"timestamp": row.get("timestamp", 0), "kind": row.get("type", "event"),
                           "text": row.get("text", "")})
        for row in _read_jsonl(self.fingerprints_path, since):
            if row.get("action") in ("first", "alert"):
                events.append({"timestamp": row.get("timestamp", 0), "kind": "error",
                               "text": row.get("sample") or row.get("fingerprint")})
        for row in _read_jsonl(self.root / "perf-samples.jsonl", since):
            tps, mspt = row.get("tps"), row.get("mspt")
            if (tps is not None and float(tps) < 18) or (mspt is not None and float(mspt) > 70):
                events.append({"timestamp": row.get("timestamp", 0), "kind": "lag",
                               "text": "TPS %s MSPT %s" % (tps, mspt)})
        crash_dir = self.server_dir / "crash-reports"
        if crash_dir.is_dir():
            for path in crash_dir.glob("*.txt"):
                try:
                    if path.stat().st_mtime >= since:
                        events.append({"timestamp": path.stat().st_mtime, "kind": "crash", "text": path.name})
                except OSError:
                    pass
        events.sort(key=lambda row: float(row.get("timestamp") or 0))
        data = {"schema": 1, "server": self.server_id, "prefix": self.prefix,
                "generated_at": now, "window": window, "events": events[-300:]}
        write_json(self.root / "timeline-latest.json", data)
        return data

    def format_timeline(self, data):
        rows = data.get("events") or []
        lines = ["%s 运维时间线（%s）：%d 项" %
                 (self.prefix, data.get("window", "6h"), len(rows))]
        for row in rows[-12:]:
            lines.append("- %s｜%s｜%s" % (_stamp(row.get("timestamp", 0)),
                         row.get("kind", "event"), str(row.get("text") or "")[:180]))
        if not rows:
            lines.append("窗口内没有已记录异常或运维事件。")
        return "\n".join(lines)

    def incident_postmortem(self, window="24h", now=None):
        timeline = self.timeline(window, now)
        kinds = Counter(row.get("kind") for row in timeline.get("events") or [])
        severity = "高" if kinds["crash"] else ("中" if kinds["lag"] or kinds["error"] >= 3 else "低")
        latest_crash = next((row for row in reversed(timeline.get("events") or [])
                             if row.get("kind") == "crash"), None)
        data = {"schema": 1, "server": self.server_id, "prefix": self.prefix,
                "generated_at": time.time(), "window": window, "severity": severity,
                "counts": dict(kinds), "latest_crash": latest_crash,
                "conclusion": ("发现崩溃恢复点，需由 Codex 结合 crash-report 与变更记录分析。"
                               if latest_crash else
                               "未发现新崩溃；继续观察错误指纹、TPS/MSPT 与备份验证结果。")}
        write_json(self.root / "postmortem-latest.json", data)
        return data

    def format_postmortem(self, data):
        counts = data.get("counts") or {}
        return ("%s 事故复盘（%s）｜风险 %s\n"
                "崩溃 %d｜错误告警 %d｜卡顿 %d｜其他事件 %d\n%s" %
                (self.prefix, data.get("window", "24h"), data.get("severity", "未知"),
                 counts.get("crash", 0), counts.get("error", 0), counts.get("lag", 0),
                 sum(counts.values()) - counts.get("crash", 0) - counts.get("error", 0) - counts.get("lag", 0),
                 data.get("conclusion", "")))

    def weekly_report(self, now=None):
        from weekly_reports import build_report
        return build_report(self, now=now)

    def format_weekly(self, data, details=False):
        from weekly_reports import format_report
        return format_report(data, details=details)


SNAPSHOT_FILES = (
    "mod-inventory.json", "recipe-index.json", "backup-verify.json", "runtime.json",
    "lag-latest.json", "timeline-latest.json", "postmortem-latest.json", "weekly-latest.json",
)


def build_snapshot_payload(telemetry):
    artifacts = {}
    for name in SNAPSHOT_FILES:
        value = _read_json(telemetry.root / name)
        if value is not None:
            artifacts[name] = value
    artifacts["events-tail.json"] = _read_jsonl(telemetry.root / "events.jsonl", 0)[-1000:]
    artifacts["fingerprints-tail.json"] = _read_jsonl(telemetry.fingerprints_path, 0)[-1000:]
    artifacts["perf-tail.json"] = _read_jsonl(telemetry.root / "perf-samples.jsonl", 0)[-2000:]
    return {"schema": 1, "server": telemetry.server_id, "generated_at": time.time(),
            "artifacts": artifacts}


def publish_snapshot(url, telemetry, timeout=20):
    payload = json.dumps(build_snapshot_payload(telemetry), ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    body = gzip.compress(payload, compresslevel=6)
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8", "Content-Encoding": "gzip",
        "X-Ops-Snapshot": "1",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(512).decode("utf-8", "replace")
    return response.status == 200 and '"ok":true' in raw.replace(" ", "").casefold()


def write_snapshot_export(telemetry, output_path):
    """Write an ASCII export consumable through the existing restricted file API."""
    payload = json.dumps(build_snapshot_payload(telemetry), ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    packed = gzip.compress(payload, compresslevel=6)
    encoded = base64.b64encode(packed).decode("ascii")
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(encoded, encoding="ascii")
    os.replace(str(temp), str(path))
    return {"raw_bytes": len(payload), "gzip_bytes": len(packed),
            "encoded_bytes": len(encoded)}


def decode_snapshot_export(value, max_packed=8 * 1024 * 1024,
                           max_unpacked=64 * 1024 * 1024):
    text = re.sub(r"\s+", "", str(value or ""))
    packed = base64.b64decode(text.encode("ascii"), validate=True)
    if len(packed) > max_packed:
        raise ValueError("snapshot archive too large")
    raw = gzip.decompress(packed)
    if len(raw) > max_unpacked:
        raise ValueError("snapshot payload too large")
    return json.loads(raw.decode("utf-8"))


def ingest_snapshot(state_base, payload, expected_server="nast"):
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise ValueError("invalid snapshot schema")
    server = str(payload.get("server") or "")
    if server != expected_server or not re.fullmatch(r"[a-z0-9_-]{1,32}", server):
        raise ValueError("unexpected snapshot server")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict) or len(artifacts) > 20:
        raise ValueError("invalid snapshot artifacts")
    root = Path(state_base) / server
    root.mkdir(parents=True, exist_ok=True)
    allowed = set(SNAPSHOT_FILES) | {"events-tail.json", "fingerprints-tail.json", "perf-tail.json"}
    for name, value in artifacts.items():
        if name not in allowed:
            raise ValueError("unexpected snapshot artifact")
        write_json(root / name, value)
    # Convert bounded tails back to local JSONL inputs for reports.
    for source, target in (("events-tail.json", "events.jsonl"),
                           ("fingerprints-tail.json", "error-fingerprints.jsonl"),
                           ("perf-tail.json", "perf-samples.jsonl")):
        rows = artifacts.get(source)
        if isinstance(rows, list):
            temp = root / (target + ".tmp")
            temp.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                                    for row in rows if isinstance(row, dict)), encoding="utf-8")
            os.replace(str(temp), str(root / target))
    write_json(root / "snapshot-meta.json", {"received_at": time.time(),
                                              "generated_at": payload.get("generated_at")})
    return root
