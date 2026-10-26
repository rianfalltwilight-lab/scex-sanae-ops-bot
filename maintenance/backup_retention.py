#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 备份分层保留 —— 纯 dry-run 分析工具。

只做一件事：解析 world_YYYY-MM-DD_HH-MM-SS.zip 形式的备份文件名，按策略划分
「保留 / 删除」，输出 JSON 报告。**绝不**触发备份、绝不执行同步、绝不删除任何文件，
也没有 apply/delete 之类的落地能力。空间低于阈值时只追加 warning 提示，不做任何动作。

策略（全部满足才保留，否则进入 delete 且给出 reasons）：
  * newest N      —— 按时间最新保留 N 份（受保护对象除外，见 protected）
  * daily  D      —— 最近 D 天内，每天保留「当天最后一份」
  * weekly W      —— 最近 W 周内，每周（ISO 周）保留「当周最后一份」
  * protected     —— 显式指定受保护的文件名，永远保留

输入：--list-file 指定一个按行列出的文件名清单，或直接在命令行传文件名。
输出：JSON { keep: [...], delete: [...], decisions: {name: {action, reasons}}, warnings: [...] }
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

NAME_RE = re.compile(r"^world_(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})\.zip$")

# 可能继承自外部（--list-file 可选带路径前缀），这里只按文件名尾段解析。
_TS_CACHE: dict[str, dt.datetime] = {}


def parse_name(name: str) -> dt.datetime | None:
    """解析 world_YYYY-MM-DD_HH-MM-SS.zip，返回本地时区 datetime；非法返回 None。"""
    if name not in _TS_CACHE:
        m = NAME_RE.match(name)
        if not m:
            _TS_CACHE[name] = None
        else:
            try:
                _TS_CACHE[name] = dt.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H-%M-%S")
            except ValueError:
                _TS_CACHE[name] = None
    return _TS_CACHE[name]


def basename(path: str) -> str:
    return Path(path).name


def _last_per_bucket(sorted_asc: list[tuple[dt.datetime, str]], key_fn) -> dict:
    """按 key_fn 分桶，每个桶取时间最大（最后）一份，返回 key -> (ts, name)。"""
    buckets: dict = {}
    for ts, name in sorted_asc:
        buckets[key_fn(ts)] = (ts, name)
    return buckets


def plan(names: list[str],
         newest_n: int = 3,
         daily_d: int = 7,
         weekly_w: int = 4,
         protected: set[str] | None = None,
         now: dt.datetime | None = None) -> dict:
    """计算保留/删除方案。纯只读，不产生任何副作用。"""
    now = now or dt.datetime.now()
    protected = {basename(p) for p in (protected or set())}
    newest_n = max(0, int(newest_n))
    daily_d = max(0, int(daily_d))
    weekly_w = max(0, int(weekly_w))

    parsed: list[tuple[dt.datetime, str]] = []
    bad_names: list[str] = []
    for raw in names:
        bn = basename(raw)
        ts = parse_name(bn)
        if ts is None:
            bad_names.append(bn)
        else:
            parsed.append((ts, bn))

    parsed.sort(key=lambda x: x[0])  # 升序

    keep: set[str] = set()
    reasons: dict[str, list[str]] = {}

    def keep_with_reason(bn: str, reason: str) -> None:
        keep.add(bn)
        reasons.setdefault(bn, []).append(reason)

    # 1) protected —— 永远保留（即使名字非法也保留其原始名）
    for p in protected:
        keep.add(p)
        reasons.setdefault(p, []).append("protected")

    # 2) newest N —— 按时间最新 N 份（跳过 protected，protected 单独占用保留名额）
    newest_kept = 0
    for ts, bn in reversed(parsed):
        if bn in protected:
            continue
        if newest_kept >= newest_n:
            break
        keep_with_reason(bn, f"newest-{newest_n}")
        newest_kept += 1

    # 3) daily —— 最近 D 天内每天最后一份
    cutoff_day = (now - dt.timedelta(days=daily_d)).date() if daily_d > 0 else None
    if cutoff_day is not None:
        recent = [(ts, bn) for ts, bn in parsed if ts.date() >= cutoff_day]
        for ts, bn in _last_per_bucket(recent, lambda t: t.date()).values():
            if bn in keep:
                continue
            keep_with_reason(bn, f"daily-{daily_d}")

    # 4) weekly —— 最近 W 周内每周最后一份（ISO 周）
    if weekly_w > 0:
        cutoff_week = now.date() - dt.timedelta(weeks=weekly_w)
        week_start = cutoff_week - dt.timedelta(days=cutoff_week.weekday())  # 周一
        recent_weeks = [(ts, bn) for ts, bn in parsed if ts.date() >= week_start]
        for ts, bn in _last_per_bucket(recent_weeks, lambda t: (t.isocalendar()[0], t.isocalendar()[1])).values():
            if bn in keep:
                continue
            keep_with_reason(bn, f"weekly-{weekly_w}")

    decisions: dict[str, dict] = {}
    delete: list[str] = []
    keep_list: list[str] = []
    for ts, bn in parsed:
        if bn in keep:
            keep_list.append(bn)
            decisions[bn] = {"action": "keep", "reasons": reasons.get(bn, [])}
        else:
            delete.append(bn)
            decisions[bn] = {"action": "delete", "reasons": []}
    # protected 中可能含有无法解析/不在 parsed 里的名字，也要进入 keep_list
    for p in protected:
        if p not in decisions:
            keep_list.append(p)
            decisions[p] = {"action": "keep", "reasons": reasons.get(p, [])}

    warnings: list[str] = []
    for bn in bad_names:
        warnings.append(f"文件名无法解析，跳过：{bn}")

    return {
        "keep": keep_list,
        "delete": delete,
        "decisions": decisions,
        "warnings": warnings,
        "invalid_names": bad_names,
    }


def space_warning(result: dict, threshold_gib: float | None = None,
                  free_gib: float | None = None) -> list[str]:
    """仅当给出了 free_gib 且低于阈值时，向 warnings 追加提示。绝不触发任何动作。"""
    if threshold_gib is None or free_gib is None:
        return result.get("warnings", [])
    if free_gib < threshold_gib:
        message = (
            f"剩余空间 {free_gib:.2f} GiB 低于阈值 {threshold_gib:.2f} GiB；"
            "本工具仅做 dry-run 分析，不会自动删除任何备份，请人工确认后处理。"
        )
        result.setdefault("warnings", []).append(message)
    return result["warnings"]


def load_names(list_file: str | None, positional: list[str]) -> list[str]:
    names: list[str] = []
    if list_file:
        with open(list_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    names.append(line)
    names.extend(positional)
    return names


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P2 备份分层保留 dry-run 分析（永不删除文件）")
    ap.add_argument("--list-file", help="每行一个备份文件名的清单文件")
    ap.add_argument("--newest", type=int, default=3, help="最新保留份数（默认 3）")
    ap.add_argument("--daily", type=int, default=7, help="最近 N 天每天保留最后一份（默认 7）")
    ap.add_argument("--weekly", type=int, default=4, help="最近 N 周每周保留最后一份（默认 4）")
    ap.add_argument("--protected", nargs="*", default=[], help="显式受保护文件名，永远保留")
    ap.add_argument("--free-gib", type=float, help="当前剩余空间（GiB），仅用于 warning 判断")
    ap.add_argument("--threshold-gib", type=float, help="空间阈值（GiB），低于则追加 warning")
    ap.add_argument("names", nargs="*", help="备份文件名（可直接跟在命令行）")
    args = ap.parse_args(argv)

    names = load_names(args.list_file, args.names)
    if not names:
        ap.error("未提供任何备份文件名：请用 --list-file 或直接传文件名参数")

    result = plan(
        names,
        newest_n=args.newest,
        daily_d=args.daily,
        weekly_w=args.weekly,
        protected=set(args.protected),
    )
    space_warning(result, threshold_gib=args.threshold_gib, free_gib=args.free_gib)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
