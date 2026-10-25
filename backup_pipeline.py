#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可靠的手动备份流水线：保存→触发→等待稳定→快速验证→异地同步→群回报。"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"
LOCK_PATH = LOG_DIR / "backup-pipeline.lock"
STATE_PATH = LOG_DIR / "backup-pipeline-status.json"
LOG_PATH = LOG_DIR / "backup-pipeline.log"
BACKUP_DIR = "simplebackups/world"
WAIT_TIMEOUT = 15 * 60
SYNC_TIMEOUT = 90 * 60
POLL_SECONDS = 10
STABLE_CHECKS = 3

sys.path.insert(0, str(HERE))
from mcfile_client import mcfile_get, mcfile_post  # noqa: E402
from rcon_client import query  # noqa: E402
import backup_local


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + message
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def state(stage: str, message: str, **extra) -> None:
    data = {
        "state": "running" if stage not in ("success", "failed") else stage,
        "stage": stage,
        "message": message,
        "updatedAt": int(time.time()),
        **extra,
    }
    try:
        old = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        data.setdefault("startedAt", old.get("startedAt", int(time.time())))
    except Exception:
        data.setdefault("startedAt", int(time.time()))
    if stage in ("success", "failed"):
        data["finishedAt"] = int(time.time())
    atomic_json(STATE_PATH, data)
    log(f"[{stage}] {message}")


def send_group(message: str) -> bool:
    """Send only to the operator-configured notification groups."""
    from legacy_backup_control import notify
    return notify(message)


def backups() -> list[dict]:
    entries = backup_local.backups() if backup_local.enabled() else json.loads(mcfile_get("/list", path=BACKUP_DIR))
    return [e for e in entries if not e.get("dir") and e.get("name", "").endswith(".zip")]


def newest(items: list[dict]) -> dict | None:
    return max(items, key=lambda e: (e.get("mtime", 0), e.get("name", ""))) if items else None


def wait_for_new(before_names: set[str], started_at: int) -> dict:
    deadline = time.time() + WAIT_TIMEOUT
    candidate_name = None
    last_size = None
    stable = 0
    while time.time() < deadline:
        items = backups()
        candidates = [
            e for e in items
            if e["name"] not in before_names and e.get("mtime", 0) >= started_at - 5
        ]
        candidate = newest(candidates)
        if candidate:
            name, size = candidate["name"], int(candidate.get("size", 0))
            if name == candidate_name and size == last_size and size >= 1024 * 1024:
                stable += 1
            else:
                candidate_name, last_size, stable = name, size, 0
            state("waiting", f"等待新 ZIP 写入稳定：{name}，{size} bytes，稳定检查 {stable}/{STABLE_CHECKS}",
                  file=name, size=size, stableChecks=stable)
            if stable >= STABLE_CHECKS:
                return candidate
        else:
            state("waiting", "等待 SimpleBackups 生成新 ZIP")
        time.sleep(POLL_SECONDS)
    raise TimeoutError(f"{WAIT_TIMEOUT // 60} 分钟内未检测到稳定的新备份 ZIP")


def verify(name: str) -> dict:
    result = backup_local.verify(name) if backup_local.enabled() else json.loads(mcfile_get("/backup/verify", path=f"{BACKUP_DIR}/{name}"))
    if not result.get("ok"):
        raise RuntimeError("备份结构验证失败：" + "；".join(result.get("errors") or ["未知错误"]))
    return result


def wait_sync(name: str) -> dict:
    if backup_local.enabled():
        state("syncing", f"正在复制到 NAS 并校验 SHA-256：{name}", file=name)
        return backup_local.sync(name)
    deadline = time.time() + SYNC_TIMEOUT
    submitted = False
    while time.time() < deadline:
        if not submitted:
            try:
                accepted = json.loads(mcfile_post("/sync", file=name))
                submitted = bool(accepted.get("accepted", True))
                state("syncing", f"等待异地同步：{name}", file=name, accepted=accepted)
            except Exception as exc:
                # 409 表示周期同步占锁。先等它结束，再重新提交本目标，不能误认别的任务为完成。
                log(f"[sync] 当前无法提交，等待现有同步释放：{type(exc).__name__}: {exc}")

        result = json.loads(mcfile_get("/sync/status"))
        current = result.get("file") or ""
        sync_state = result.get("state")
        locked = bool(result.get("lockPresent"))
        state("syncing", result.get("message", sync_state or "同步中"), file=name, sync=result)

        if submitted and sync_state == "success" and current == name:
            local_size = int(result.get("localSize", 0))
            remote_size = int(result.get("remoteSize", 0))
            if local_size <= 0 or local_size != remote_size:
                raise RuntimeError(f"异地同步大小校验异常：源端={local_size}，目标端={remote_size}")
            return result
        if submitted and sync_state == "failed" and (not current or current == name):
            raise RuntimeError("异地同步失败：" + result.get("message", "未知错误"))
        if not submitted and not locked and sync_state not in ("running", "starting"):
            # 其他同步已经结束；下一轮重新调用 /sync 提交本目标。
            continue
        time.sleep(POLL_SECONDS)
    raise TimeoutError(f"异地同步超过 {SYNC_TIMEOUT // 60} 分钟仍未完成")


def format_size(size: int) -> str:
    return f"{size / 1024 / 1024 / 1024:.2f} GiB"


def run(trigger: bool = True, existing: str | None = None, notify: bool = True,
        source: str = "manual") -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock_fp = LOCK_PATH.open("a+")
    try:
        backup_local.lock(lock_fp)
    except BlockingIOError:
        if notify:
            send_group("[备份] 已有备份流水线运行中，本次请求未重复执行。")
        return 3

    started = int(time.time())
    atomic_json(STATE_PATH, {"state": "running", "stage": "starting", "message": "初始化",
                             "source": source, "startedAt": started, "updatedAt": started})
    try:
        if existing:
            target = next((e for e in backups() if e["name"] == existing), None)
            if not target:
                raise FileNotFoundError("指定备份不存在：" + existing)
        elif trigger:
            before = backups()
            before_names = {e["name"] for e in before}
            state("saving", "正在执行 save-all flush")
            query("save-all flush")
            state("triggering", "正在触发 SimpleBackups 手动备份")
            response = query("simplebackups backup start").strip()
            log("[rcon] " + (response or "(无返回)"))
            target = wait_for_new(before_names, started)
        else:
            target = newest(backups())
            if not target:
                raise FileNotFoundError("没有可验证的备份 ZIP")

        name = target["name"]
        state("verifying", f"快速验证 {name}", file=name)
        checked = verify(name)
        state("verified", f"结构验证通过：{name}", file=name, verify=checked)
        synced = wait_sync(name)
        elapsed = int(time.time()) - started
        heading = "[定时备份完成]" if source == "scheduled" else "[备份完成]"
        message = (
            heading + "\n"
            f"文件：{name}\n"
            f"大小：{format_size(int(checked['size']))}\n"
            f"耗时：{elapsed // 60}分{elapsed % 60}秒\n"
            f"结构验证：通过（{checked['entryCount']} 个条目）\n"
            f"level.dat：可读\n"
            f"region：{checked['regionCount']} 个\n"
            f"玩家数据：{checked['playerdataCount']} 个\n"
            + ("异地同步：NAS 已确认，SHA-256 一致" if backup_local.enabled() else "异地同步：目标端已确认，字节数一致")
        )
        state("success", "本地验证和异地同步均完成", source=source,
              file=name, verify=checked, sync=synced)
        if notify:
            send_group(message)
        return 0
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        state("failed", message, source=source, file=locals().get("name"))
        if notify:
            heading = "[定时备份异常]" if source == "scheduled" else "[备份异常]"
            send_group(heading + "\n" + message + "\n线上世界和已有旧备份未受影响。")
        return 1

    finally:
        backup_local.unlock(lock_fp)
        lock_fp.close()

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--existing", help="不触发新备份，验证并同步指定 ZIP")
    ap.add_argument("--latest", action="store_true", help="不触发新备份，验证并同步最新 ZIP")
    ap.add_argument("--no-notify", action="store_true", help="不发送群消息，用于回归")
    ap.add_argument("--source", choices=("manual", "scheduled"), default="manual",
                    help="群通知来源标签")
    args = ap.parse_args()
    return run(trigger=not (args.existing or args.latest), existing=args.existing,
               notify=not args.no_notify, source=args.source)


if __name__ == "__main__":
    raise SystemExit(main())
