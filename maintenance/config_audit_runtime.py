#!/usr/bin/env python3
"""Runtime-only config audit: snapshot, hash-chain events, finalize, read-only status."""
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

_LOCK = threading.RLock()
_SENSITIVE = re.compile(r"(password|passwd|secret|token|credential|private[_-]?key|rcon|api[_-]?key)", re.I)


class AuditError(Exception):
    pass


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve(root, rel):
    root = Path(root).resolve()
    item = Path(rel)
    if item.is_absolute():
        raise AuditError("absolute path rejected")
    target = (root / item).resolve()
    if target != root and root not in target.parents:
        raise AuditError("path escape rejected")
    if target == root or ".config-audit" in target.parts:
        raise AuditError("audit path rejected")
    if _SENSITIVE.search(target.name):
        raise AuditError("sensitive path rejected")
    return root, target


class Auditor:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.base = self.root / ".config-audit"
        self.events_path = self.base / "events.jsonl"
        self.files = self.base / "files"

    def _rows(self):
        if not self.events_path.exists():
            return []
        with open(self.events_path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def _append(self, event):
        with _LOCK:
            rows = self._rows()
            previous = rows[-1]["hash"] if rows else "GENESIS"
            event = dict(event)
            event["seq"] = len(rows) + 1
            canonical_event = {
                key: event.get(key, "")
                for key in (
                    "changeId", "kind", "seq", "ts", "actor", "operation", "relPath",
                    "beforeSha256", "afterSha256", "verified", "error", "detail",
                )
            }
            canonical = json.dumps(canonical_event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            event["hash"] = hashlib.sha256((previous + "\n" + canonical).encode("utf-8")).hexdigest()
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.events_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return event

    def snapshot(self, rel, operation, detail=""):
        status = self.status()
        if not status.get("chainValid"):
            raise AuditError("audit chain invalid; write rejected")
        root, target = _resolve(self.root, rel)
        if not target.is_file():
            raise AuditError("target is not a regular file")
        change_id = time.strftime("%Y%m%d%H%M%S") + "-" + hashlib.sha256(os.urandom(16)).hexdigest()[:8]
        relative = target.relative_to(root).as_posix()
        copy = self.files / change_id / relative
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(target.read_bytes())
        before = _sha256(target)
        self._append({
            "kind": "snapshot", "changeId": change_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "actor": "mcfile-api", "operation": operation, "relPath": relative,
            "beforeSha256": before, "afterSha256": "", "verified": "", "error": "", "detail": detail,
        })
        return change_id

    def finalize(self, change_id, rel, operation, verified, error=""):
        root, target = _resolve(self.root, rel)
        after = _sha256(target) if target.is_file() else ""
        self._append({
            "kind": "finalize", "changeId": change_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "actor": "mcfile-api", "operation": operation,
            "relPath": target.relative_to(root).as_posix(), "beforeSha256": "",
            "afterSha256": after, "verified": verified, "error": error, "detail": "",
        })
        return after

    def status(self):
        rows = self._rows()
        previous = "GENESIS"
        fields = (
            "changeId", "kind", "seq", "ts", "actor", "operation", "relPath",
            "beforeSha256", "afterSha256", "verified", "error", "detail",
        )
        for row in rows:
            claimed = row.get("hash")
            payload = {key: row.get(key, "") for key in fields}
            canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            expected = hashlib.sha256((previous + "\n" + canonical).encode("utf-8")).hexdigest()
            if claimed != expected:
                return {"ok": False, "chainValid": False, "eventCount": len(rows), "error": "audit chain invalid"}
            previous = claimed
        return {
            "ok": True,
            "chainValid": True,
            "eventCount": len(rows),
            "changes": [row for row in rows if row.get("kind") == "snapshot"][-20:],
        }
