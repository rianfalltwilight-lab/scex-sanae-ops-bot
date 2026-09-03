#!/usr/bin/env python3
"""Safe OneBot media segment extraction and local media limits."""
import hashlib
import re

CQ_RE = re.compile(r"\[CQ:([^,\]]+)([^\]]*)\]")
KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^,\]]*)")


def parse_segments(event_or_raw):
    raw = event_or_raw if isinstance(event_or_raw, str) else str((event_or_raw or {}).get("raw_message") or "")
    segments = []
    if isinstance(event_or_raw, dict) and isinstance(event_or_raw.get("message"), list):
        for item in event_or_raw["message"]:
            if not isinstance(item, dict):
                continue
            data = item.get("data") or {}
            segments.append({"type": str(item.get("type") or ""), **{str(k): str(v) for k, v in data.items()}})
    if segments:
        return segments
    for match in CQ_RE.finditer(raw):
        values = {key: value for key, value in KV_RE.findall(match.group(2))}
        segments.append({"type": match.group(1), **values})
    return segments


def media_segments(event_or_raw, kinds=("image", "record", "audio", "video", "file", "forward")):
    wanted = set(kinds)
    return [item for item in parse_segments(event_or_raw) if item.get("type") in wanted]


def media_urls(event_or_raw, kinds=("image", "record", "audio", "video")):
    result = []
    for item in media_segments(event_or_raw, kinds):
        value = item.get("url") or item.get("file") or item.get("path")
        if value and value not in result:
            result.append(value)
    return result


def content_fingerprint(data):
    return hashlib.sha256(bytes(data)).hexdigest()


def media_summary(event_or_raw):
    rows = []
    for item in media_segments(event_or_raw):
        kind = item.get("type")
        value = item.get("url") or item.get("file") or item.get("path") or item.get("id") or ""
        rows.append({"type": kind, "available": bool(value), "name": str(value).split("/")[-1][:120]})
    return rows
