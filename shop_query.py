"""Mention-only shop snapshots for Sanae; deployed beside llbot_bridge.py."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from onebot_utf8 import onebot_headers
from pathlib import Path
import re
import time
import urllib.request
from sanae_identity import SANAE_QQ, HISTORICAL_BOT_QQ

BOT_QQ = SANAE_QQ
MONITOR_ROOT = Path(os.environ.get("SANAE_SHOP_MONITOR_ROOT", str(Path(__file__).resolve().parent / "shop-monitor")))
CST = timezone(timedelta(hours=8))


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def is_query(event, *, root=None, config=None):
    """Only a real mention in an enabled shop group may enter this route."""
    if event.get("post_type") != "message" or event.get("message_type") != "group":
        return False
    if (str(event.get("user_id", "")) in HISTORICAL_BOT_QQ
            or str(event.get("user_id", "")) == str(event.get("self_id", ""))):
        return False
    segments = event.get("message")
    if isinstance(segments, list):
        mentioned = any(isinstance(seg, dict) and seg.get("type") == "at"
                        and isinstance(seg.get("data"), dict)
                        and str(seg["data"].get("qq")) == BOT_QQ for seg in segments)
    else:
        mentioned = bool(re.search(r"\[CQ:at,qq=" + BOT_QQ + r"(?:,|\])",
                                   str(event.get("raw_message", ""))))
    if not mentioned:
        return False
    try:
        config = config if config is not None else _read((root or MONITOR_ROOT) / "config.json")
        allowed = set()
        if int(config.get("all_alert_group_id", 0)) > 0:
            allowed.add(int(config["all_alert_group_id"]))
        if config.get("priority_group_enabled", True) and int(config.get("group_id", 0)) > 0:
            allowed.add(int(config["group_id"]))
        allowed.intersection_update(int(gid) for gid in config.get("query_group_ids", allowed))
        return int(event.get("group_id", 0)) in allowed
    except (OSError, ValueError, TypeError):
        return False


def _text(value, limit=100):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def render_snapshot(config, state, *, now=None):
    """Use only fresh, healthy, confirmed listings; never infer missing stock."""
    now = time.time() if now is None else now
    fresh_seconds = max(60, config.get("poll_seconds", 20) * 3)
    shops = {shop["id"]: shop for shop in config["shops"]}
    health = state.get("shop_health", {})
    healthy = {key for key in shops if not health.get(key, {}).get("error")
               and 0 <= now - health.get(key, {}).get("last_success", 0) <= fresh_seconds}
    rows = []
    for item in state.get("items", {}).values():
        if (item.get("shop_id") not in healthy or not item.get("active")
                or not item.get("target") or item.get("classification_pending")
                or not 0 <= now - item.get("observed_epoch", 0) <= fresh_seconds
                or type(item.get("stock")) is not int or item["stock"] <= 0):
            continue
        try:
            price = Decimal(str(item.get("price", "")))
        except InvalidOperation:
            continue
        if price.is_finite() and price >= 0:
            rows.append((price, item))
    rows.sort(key=lambda row: (row[0], str(row[1].get("id", ""))))
    lines = ["【链动小铺·按需查询】", f"采集正常：{len(healthy)}/{len(shops)} 家"]
    if rows:
        lines.append(f"已确认有货最低单价：¥{rows[0][0]:f}；有货商品 {len(rows)} 件")
        for price, item in rows[:8]:
            lines.append(f"\n{_text(item.get('shop_name'))}｜{_text(item.get('title'))}")
            stock = str(item["stock"])
            lines.append(f"¥{price:f}｜库存 {stock}")
            lines.append(_text(item.get("url"), 300))
        if len(rows) > 8:
            lines.append(f"\n仅列最低价前 8 件，共 {len(rows)} 件。")
    else:
        lines.append("当前正常采集的店铺中，暂未发现已确认有货的目标商品。")
    unavailable = [shop["name"] for key, shop in shops.items() if key not in healthy]
    if unavailable:
        lines.append("暂未取得新鲜数据：" + "、".join(map(_text, unavailable)) + "（未计入比价）")
    observed = state.get("last_poll")
    if observed:
        stamp = datetime.fromtimestamp(observed, CST).strftime("%m-%d %H:%M:%S")
        lines.append("最近采集：" + stamp + "（北京时间）")
    lines.append("默认静默，仅在本群 @早苗 时查询。")
    return "\n".join(lines)


def _send(config, group_id, text, *, opener=None):
    if not config.get("onebot_url"):
        raise ValueError("onebot_url is not configured")
    payload = {"group_id": int(group_id), "message": [{"type": "text", "data": {"text": text}}],
               "auto_escape": True}
    request = urllib.request.Request(config["onebot_url"] + "/send_group_msg",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=onebot_headers(), method="POST")
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        result = json.load(response)
    receipt = (result.get("data") or {}).get("message_id")
    if result.get("status") != "ok" or result.get("retcode") not in (0, "0") or receipt is None:
        raise RuntimeError("onebot_query_failure")
    return receipt


def reply(event, *, root=None, sender=None):
    root = root or MONITOR_ROOT
    try:
        config = _read(root / "config.json")
        if not is_query(event, config=config):
            return None
        try:
            text = render_snapshot(config, _read(root / "state" / "monitor.json"))
        except (OSError, ValueError, TypeError, KeyError):
            text = "【链动小铺·按需查询】暂时无法读取采集结果，请稍后再 @早苗 查询。"
        return (sender or _send)(config, int(event["group_id"]), text)
    except Exception as exc:
        print(f"[liandong-query] failed: {type(exc).__name__}", flush=True)
        return None
