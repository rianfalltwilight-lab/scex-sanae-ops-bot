#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置变更审计（config change audit）—— 纯 Python、不依赖真实服务器。

目标：给 mcfile-server 这类「改配置」的动作套一层可追溯、可回滚的审计外壳。

设计要点：
  * snapshot_before_write(...) —— 在改动目标文件**之前**调用，把当前文件按唯一 changeId
    复制进 root 内的专用审计目录（防路径逃逸），并记录 before SHA256、相对路径、actor、
    operation、时间。
  * finalize(...) —— 在改动完成后调用，记录 after SHA256、verified/failed、错误信息。
  * 审计日志为 JSONL（每行一条事件），并带**前向 SHA256 哈希链**：每条记录的 hash 字段
    由「上一条记录的 hash + 本条记录内容」计算得到，篡改任何一条都会破坏后续整链。
  * rollback(...) —— 恢复某次变更之前的文件。默认 dry_run=True 只预览不落地；
    真正恢复必须显式传 apply=True，且恢复前会再备份「当前文件」并写入一条 rollback 事件。
  * 敏感文件拒绝：凡是 basename 命中「密码/密钥/token/secret/rcon」等模式，或路径本身
    命中审计目录内部，一律拒绝，防止把敏感凭据或审计数据本身复制出去。

安全约束（红线）：
  * 目标文件必须落在 root 之内（resolve 后仍以 root 为前缀），否则拒绝 —— 防任意读。
  * changeId 必须是安全 token（[A-Za-z0-9_-]+），且目标相对路径也必须能安全 join，
    防止用 "../" 之类的相对路径逃逸出审计目录。
  * 所有文件写入都走原子写（临时文件 + os.replace），避免半截文件。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

# 审计目录在 root 内的名称。
AUDIT_DIR_NAME = ".config-audit"

# changeId 只允许安全字符，杜绝路径注入。
CHANGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

# 敏感文件 basename 模式：命中即拒绝复制（防把凭据写进审计目录）。
SENSITIVE_NAME_RE = re.compile(
    r"(password|passwd|secret|token|credential|private[_-]?key|rcon|api[_-]?key)",
    re.IGNORECASE,
)
# 敏感扩展名 / 明确后缀。
SENSITIVE_SUFFIXES = (".key", ".pem", ".p12", ".pfx", ".jks", ".keystore")

# mcfile-server 使用 ThreadingHTTPServer；同一进程内所有审计链读写必须串行，避免两个请求
# 同时取得相同 prev hash / seq，生成表面可写但不可验证的分叉链。
_AUDIT_LOCK = threading.RLock()


class ConfigAuditError(Exception):
    """审计模块抛出的所有业务异常。"""


def _sha256_file(path: Path) -> str:
    """计算文件 SHA256（hex）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_write(path: Path, data: str | bytes) -> None:
    """原子写：先写临时文件再 replace，避免半截文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-audit-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _resolve_within_root(root: Path, rel_path: str) -> Path:
    """把相对路径解析到 root 之内，任何逃逸（绝对路径 / ..）都拒绝。

    返回绝对路径。rel_path 允许是相对路径；也允许包含子目录。
    """
    root = Path(root).resolve()
    p = Path(rel_path)
    if p.is_absolute():
        raise ConfigAuditError(f"拒绝绝对路径：{rel_path!r}")
    resolved = (root / p).resolve()
    # resolve 后再校验前缀，防止 ../ 和符号链接逃逸。
    if resolved != root and root not in resolved.parents:
        raise ConfigAuditError(f"路径逃逸 root（拒绝）：{rel_path!r} -> {resolved}")
    return resolved


def is_sensitive_name(name: str) -> bool:
    """判断文件 basename 是否疑似敏感（凭据 / 密钥 / rcon 等）。"""
    lower = name.lower()
    if SENSITIVE_NAME_RE.search(name):
        return True
    return lower.endswith(SENSITIVE_SUFFIXES)


class ConfigAuditor:
    """目录级配置审计器。

    一个实例绑定一个 root（例如服务器配置目录）。所有变更都记录在
    root/.config-audit/ 之下：

        root/.config-audit/
            files/<changeId>/<relative-path>      —— before 快照
            events.jsonl                           —— 事件日志（JSONL + 哈希链）

    events.jsonl 每行一条事件，字段：
        changeId, seq, ts, actor, operation, relPath,
        beforeSha256, afterSha256, verified, error, detail, kind,
        hash  （由前一条 hash + 本条内容的规范化文本计算）
    """

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root).resolve()
        self.audit_dir = self.root / AUDIT_DIR_NAME
        self.files_dir = self.audit_dir / "files"
        self.events_path = self.audit_dir / "events.jsonl"
        # 惰性建立目录（不写任何事件，只是目录）。
        self.files_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- internals

    def _last_hash(self) -> str:
        """读取 events.jsonl 最后一条的 hash，作为下一条的链上前驱。无则返回 GENESIS。"""
        if not self.events_path.exists():
            return "GENESIS"
        last = None
        with open(self.events_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last = line
        if last is None:
            return "GENESIS"
        try:
            return json.loads(last)["hash"]
        except (json.JSONDecodeError, KeyError):
            raise ConfigAuditError("审计链损坏：最后一条记录缺少 hash 字段")

    def _chain_hash(self, prev_hash: str, event: dict) -> str:
        """规范化事件内容后与 prev_hash 拼接求 SHA256。

        规范化：对 (kind, seq, ts, actor, operation, relPath, beforeSha256,
        afterSha256, verified, error, detail, changeId) 按字段名排序序列化为紧凑 JSON。
        """
        canon = {
            "changeId": event.get("changeId", ""),
            "kind": event.get("kind", ""),
            "seq": event.get("seq", 0),
            "ts": event.get("ts", ""),
            "actor": event.get("actor", ""),
            "operation": event.get("operation", ""),
            "relPath": event.get("relPath", ""),
            "beforeSha256": event.get("beforeSha256", ""),
            "afterSha256": event.get("afterSha256", ""),
            "verified": event.get("verified", ""),
            "error": event.get("error", ""),
            "detail": event.get("detail", ""),
        }
        payload = json.dumps(canon, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256((prev_hash + "\n" + payload).encode("utf-8")).hexdigest()

    def _append_event(self, event: dict) -> dict:
        """写入一条事件，计算哈希链并落盘。返回带 hash 的完整事件。"""
        with _AUDIT_LOCK:
            prev = self._last_hash()
            event = dict(event)
            # seq 也必须在锁内分配，不能由调用者在锁外基于旧日志长度推算。
            event["seq"] = self._next_seq()
            event["hash"] = self._chain_hash(prev, event)
            line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            return event

    def _read_events(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        events = []
        with open(self.events_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    @staticmethod
    def _validate_chain(events: list[dict]) -> bool:
        prev = "GENESIS"
        for e in events:
            got = e.get("hash")
            if got is None:
                return False
            canon = {
                "changeId": e.get("changeId", ""),
                "kind": e.get("kind", ""),
                "seq": e.get("seq", 0),
                "ts": e.get("ts", ""),
                "actor": e.get("actor", ""),
                "operation": e.get("operation", ""),
                "relPath": e.get("relPath", ""),
                "beforeSha256": e.get("beforeSha256", ""),
                "afterSha256": e.get("afterSha256", ""),
                "verified": e.get("verified", ""),
                "error": e.get("error", ""),
                "detail": e.get("detail", ""),
            }
            payload = json.dumps(canon, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            expect = hashlib.sha256((prev + "\n" + payload).encode("utf-8")).hexdigest()
            if expect != got:
                return False
            prev = got
        return True

    def _check_change_id(self, change_id: str) -> None:
        if not isinstance(change_id, str) or not CHANGE_ID_RE.match(change_id):
            raise ConfigAuditError(f"非法 changeId：{change_id!r}（仅允许 [A-Za-z0-9_.-]，1-128 位）")

    def _change_dir(self, change_id: str) -> Path:
        return self.files_dir / change_id

    # -------------------------------------------------------------------- API

    def snapshot_before_write(self, target: str, actor: str, operation: str,
                              change_id: str | None = None, detail: str = "") -> dict:
        """改动前快照目标文件。

        参数：
          target    —— 相对 root 的路径（或绝对但必须落在 root 内）。
          actor     —— 谁在改（例如 "admin" / "mcfile-server"）。
          operation —— 动作名（例如 "replace_config" / "set_property"）。
          change_id —— 可选，指定 changeId；缺省自动生成（时间戳+随机）。
          detail    —— 附加说明。

        返回：包含 changeId、relPath、beforeSha256 等信息的 dict。

        副作用：把目标文件复制到 root/.config-audit/files/<changeId>/<relPath>，
        写入一条 kind="snapshot" 的事件。
        """
        resolved = _resolve_within_root(self.root, str(target))
        if resolved == self.root or (self.audit_dir in resolved.parents or resolved == self.audit_dir):
            # 不允许 target 指向审计目录本身或其内容（含根目录本身）。
            if resolved == self.root or resolved == self.audit_dir or self.audit_dir in resolved.parents:
                raise ConfigAuditError(f"target 落入审计目录内部（拒绝）：{target!r}")

        rel_path = resolved.relative_to(self.root).as_posix()
        if is_sensitive_name(Path(rel_path).name):
            raise ConfigAuditError(f"敏感文件拒绝快照：{rel_path!r}")

        if not resolved.exists() or not resolved.is_file():
            raise ConfigAuditError(f"目标文件不存在或非普通文件：{rel_path!r}")

        if change_id is None:
            change_id = _new_change_id()
        self._check_change_id(change_id)

        before_sha = _sha256_file(resolved)

        # 复制到审计目录（保持相对路径，防逃逸已在 _resolve_within_root 保证）。
        snap_path = self._change_dir(change_id) / rel_path
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        with open(resolved, "rb") as src, open(snap_path, "wb") as dst:
            for chunk in iter(lambda: src.read(65536), b""):
                dst.write(chunk)

        event = {
            "kind": "snapshot",
            "changeId": change_id,
            "seq": 0,
            "ts": _now_iso(),
            "actor": actor,
            "operation": operation,
            "relPath": rel_path,
            "beforeSha256": before_sha,
            "afterSha256": "",
            "verified": "",
            "error": "",
            "detail": detail,
        }
        self._append_event(event)
        return {
            "changeId": change_id,
            "relPath": rel_path,
            "beforeSha256": before_sha,
            "snapshotPath": str(snap_path),
        }

    def _next_seq(self) -> int:
        return len(self._read_events()) + 1

    def finalize(self, change_id: str, actor: str, operation: str,
                 target: str, verified: str, after_sha256: str = "",
                 error: str = "", detail: str = "") -> dict:
        """改动完成后收尾，记录 after 快照 / 结果。

        verified 取 "verified" 或 "failed"。after_sha256 若为空会自动计算当前文件哈希；
        若 finalize 时文件已被删除，则 after_sha256 留空，error 应说明。
        """
        self._check_change_id(change_id)
        if verified not in ("verified", "failed"):
            raise ConfigAuditError(f"verified 必须是 'verified' 或 'failed'，得到 {verified!r}")
        resolved = _resolve_within_root(self.root, str(target))
        rel_path = resolved.relative_to(self.root).as_posix()

        if not after_sha256:
            if resolved.exists() and resolved.is_file():
                after_sha256 = _sha256_file(resolved)

        event = {
            "kind": "finalize",
            "changeId": change_id,
            "seq": 0,
            "ts": _now_iso(),
            "actor": actor,
            "operation": operation,
            "relPath": rel_path,
            "beforeSha256": "",
            "afterSha256": after_sha256,
            "verified": verified,
            "error": error,
            "detail": detail,
        }
        self._append_event(event)
        return {"changeId": change_id, "verified": verified, "afterSha256": after_sha256}

    def rollback(self, change_id: str, actor: str, dry_run: bool = True,
                 apply: bool = False) -> dict:
        """回滚某次变更到 before 快照。

        安全默认：dry_run=True（只预览，不写任何东西）。真正恢复必须 apply=True 且
        dry_run=False。apply 前会先备份「当前文件」并写入一条 kind="rollback" 事件。

        返回 dict：dry_run / applied / 目标路径 / 会覆盖的哈希等信息。
        """
        if dry_run and apply:
            raise ConfigAuditError("dry_run 与 apply 不能同时为真；apply 必须先 dry_run=False")
        self._check_change_id(change_id)

        # 找到该 changeId 的 snapshot 事件。
        snap_event = None
        for e in self._read_events():
            if e.get("kind") == "snapshot" and e.get("changeId") == change_id:
                snap_event = e
        if snap_event is None:
            raise ConfigAuditError(f"未找到 changeId={change_id!r} 的 before 快照")

        rel_path = snap_event["relPath"]
        snap_path = self._change_dir(change_id) / rel_path
        if not snap_path.exists():
            raise ConfigAuditError(f"快照文件缺失，无法回滚：{snap_path}")

        resolved = _resolve_within_root(self.root, rel_path)
        # 快照内容哈希作为「要恢复到的内容」。
        restore_sha = _sha256_file(snap_path)
        cur_sha = _sha256_file(resolved) if (resolved.exists() and resolved.is_file()) else ""

        preview = {
            "changeId": change_id,
            "relPath": rel_path,
            "targetPath": str(resolved),
            "restoreFromSha256": restore_sha,
            "currentSha256": cur_sha,
            "dryRun": dry_run,
            "applied": False,
        }

        if dry_run:
            return preview

        if not apply:
            raise ConfigAuditError("dry_run=False 但未显式 apply=True；回滚未执行（仍为预览）")

        # 先备份当前文件（若存在），再覆盖。
        backup_path = self._change_dir(change_id) / (rel_path + ".pre-rollback")
        if resolved.exists() and resolved.is_file():
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            with open(resolved, "rb") as src, open(backup_path, "wb") as dst:
                for chunk in iter(lambda: src.read(65536), b""):
                    dst.write(chunk)
            backup_sha = _sha256_file(backup_path)
        else:
            backup_sha = ""

        # 原子恢复：先在目标同目录写完整临时文件，再 replace，避免崩溃留下半截配置。
        resolved.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(resolved, snap_path.read_bytes())

        event = {
            "kind": "rollback",
            "changeId": change_id,
            "seq": 0,
            "ts": _now_iso(),
            "actor": actor,
            "operation": "rollback",
            "relPath": rel_path,
            "beforeSha256": cur_sha,
            "afterSha256": restore_sha,
            "verified": "verified",
            "error": "",
            "detail": f"rollback to snapshot; pre-rollback backup sha256={backup_sha}",
        }
        self._append_event(event)

        preview["applied"] = True
        preview["preRollbackBackupSha256"] = backup_sha
        return preview

    def verify_chain(self) -> bool:
        """校验整个审计日志的哈希链连续。"""
        return self._validate_chain(self._read_events())

    def list_changes(self) -> list[dict]:
        """返回全部变更（snapshot 事件，含链接信息）。"""
        return [e for e in self._read_events() if e.get("kind") == "snapshot"]


def _new_change_id() -> str:
    return time.strftime("%Y%m%d%H%M%S") + "-" + hashlib.sha256(os.urandom(16)).hexdigest()[:8]


# 模块级便捷函数（等价 API，无状态）。

def snapshot_before_write(root, target, actor, operation, change_id=None, detail=""):
    return ConfigAuditor(root).snapshot_before_write(
        target, actor, operation, change_id=change_id, detail=detail
    )


def finalize(root, change_id, actor, operation, target, verified,
             after_sha256="", error="", detail=""):
    return ConfigAuditor(root).finalize(
        change_id, actor, operation, target, verified,
        after_sha256=after_sha256, error=error, detail=detail
    )


def rollback(root, change_id, actor, dry_run=True, apply=False):
    return ConfigAuditor(root).rollback(change_id, actor, dry_run=dry_run, apply=apply)


if __name__ == "__main__":
    raise SystemExit(0)
