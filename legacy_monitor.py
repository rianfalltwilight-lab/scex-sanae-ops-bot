#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SCEX Legacy Genesis 事件监控（WSL systemd user service）。"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

from onebot_utf8 import JSON_CONTENT_TYPE, json_bytes, onebot_success_utf8, utf8_text
from ops_contract import read_incremental_lines
from ops_telemetry import OpsTelemetry
from weekly_reports import publish_due
from startup_notice import StartupNotifier
from lag_forensics import coordinator
from server_registry import parse_player_list, query_server, resolve_server, server_prefix
from advancement_localization import AdvancementLocalizer


SERVER = resolve_server('legacy')
PREFIX = server_prefix(SERVER)
SERVER_DIR = SERVER['server_dir']
LOG_PATH = os.path.join(SERVER_DIR, 'logs', 'latest.log')
CRASH_DIR = os.path.join(SERVER_DIR, 'crash-reports')
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state')
LLBOT_API = os.environ.get('LLBOT_API', 'http://127.0.0.1:3000')
LLBOT_GROUP = int(os.environ.get('LLBOT_GROUP', '0'))
LLBOT_MANAGEMENT_GROUP = int(os.environ.get('LLBOT_MANAGEMENT_GROUP', '0'))
POLL_SECONDS = max(2, int(os.environ.get('LEGACY_MONITOR_INTERVAL', '5')))
os.makedirs(STATE_DIR, exist_ok=True)
OPS = OpsTelemetry('legacy', PREFIX, SERVER_DIR,
                   query_fn=lambda command: query_server(SERVER, command))
STARTUP = StartupNotifier(SERVER, OPS.root)
ADVANCEMENTS = AdvancementLocalizer(
    SERVER_DIR, os.path.join(STATE_DIR, 'legacy-advancement-localization.json'),
    extra_roots=[item for item in os.environ.get('ADVANCEMENT_PACK_ROOTS', '').split(os.pathsep)
                 if item])

JOIN_PATTERN = re.compile(
    r'\[Server thread/INFO\] \[net\.minecraft\.server\.MinecraftServer/\]: (\S+) joined the game')
CHAT_PATTERN = re.compile(
    r'\[Server thread/INFO\] \[net\.minecraft\.server\.MinecraftServer/\]: '
    r'(?:\[Not Secure\]\s+)?<([^>\r\n]{1,64})>\s+(.+)$')
CHAT_ECHO_PREFIXES = ('[QQ群]', '[Server]', '<Server>')
EVENT_PATTERNS = [
    (re.compile(r'\[Server thread/INFO\] \[net\.minecraft\.server\.MinecraftServer/\]: (\S+) left the game'),
     lambda match: f'{PREFIX} 玩家 {match.group(1)} 离开服务器'),
    (re.compile(r'\[Server thread/INFO\] \[net\.minecraft\.server\.MinecraftServer/\]: (\S+) was slain by (\S+)'),
     lambda match: f'{PREFIX} {match.group(1)} 被 {match.group(2)} 击杀'),
    (re.compile(r'\[Server thread/INFO\] \[net\.minecraft\.server\.MinecraftServer/\]: (\S+) drowned'),
     lambda match: f'{PREFIX} {match.group(1)} 淹死了'),
]
ADVANCEMENT_PATTERNS = [
    (re.compile(r'\[Server thread/INFO\] \[net\.minecraft\.server\.MinecraftServer/\]: (\S+) has made the advancement \[(.+)\]'), 'progress'),
    (re.compile(r'\[Server thread/INFO\] \[net\.minecraft\.server\.MinecraftServer/\]: (\S+) has reached the goal \[(.+)\]'), 'goal'),
    (re.compile(r'\[Server thread/INFO\] \[net\.minecraft\.server\.MinecraftServer/\]: (\S+) has completed the challenge \[(.+)\]'), 'challenge'),
]


def _state_path(name):
    return os.path.join(STATE_DIR, name + '.json')


def load_state(name, default=None):
    try:
        with open(_state_path(name), encoding='utf-8-sig') as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


def save_state(name, value):
    path = _state_path(name)
    temp = path + '.tmp'
    with open(temp, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
    os.replace(temp, path)


def _baseline_log_state():
    stat = os.stat(LOG_PATH)
    with open(LOG_PATH, 'rb') as stream:
        head = hashlib.sha256(stream.read(min(128, stat.st_size))).hexdigest()
    return {'inode': stat.st_ino, 'offset': stat.st_size, 'partial': '', 'head': head}


def _join_message(player, query_fn=query_server):
    try:
        parsed = parse_player_list(query_fn(SERVER, 'list'))
    except Exception:
        parsed = None
    if not parsed:
        return f'{PREFIX} 玩家 {player} 加入服务器（人数暂不可查）'
    return f'{PREFIX} 玩家 {player} 加入服务器（{parsed["current"]}/{parsed["max"]}）'


def _join_is_duplicate(player, now=None):
    now = float(now if now is not None else time.time())
    state = load_state('legacy-join-dedupe', {}) or {}
    recent = {name: float(seen) for name, seen in state.items() if now - float(seen) < 30}
    key = player.casefold()
    duplicate = key in recent
    recent[key] = now
    save_state('legacy-join-dedupe', recent)
    return duplicate


def _player_chat_message(line):
    """Render an ordinary player chat line without allowing bridge/CQ echoes."""
    match = CHAT_PATTERN.search(line)
    if not match:
        return None
    player = match.group(1).strip()
    content = re.sub(r'[\r\n\t]+', ' ', match.group(2)).strip()
    content = re.sub(r'§[0-9A-FK-ORa-fk-or]', '', content)
    if not player or player.casefold() == 'server':
        return None
    if not content or content.startswith(CHAT_ECHO_PREFIXES):
        return None
    # OneBot interprets CQ segments embedded in plain text. Full-width '[' keeps
    # the visible text while preventing a player from injecting mentions/images.
    content = re.sub(r'\[CQ:', '［CQ:', content, flags=re.IGNORECASE)
    return f'{PREFIX} <{player}> {content[:400]}'


def logwatch(query_fn=query_server, include_ops=False):
    if not os.path.isfile(LOG_PATH):
        return []
    state = load_state('legacy-logwatch')
    if state is None:
        save_state('legacy-logwatch', _baseline_log_state())
        return ([], []) if include_ops else []
    lines, next_state, ok = read_incremental_lines(LOG_PATH, state)
    if not ok:
        return ([], []) if include_ops else []
    ops_alerts = OPS.observe_lines(lines)
    events = []
    for line in lines:
        joined = JOIN_PATTERN.search(line)
        if joined:
            player = joined.group(1)
            if not _join_is_duplicate(player):
                rendered = _join_message(player, query_fn)
                events.append(rendered)
                OPS.record_event('join', rendered, player=player)
            continue
        chat = _player_chat_message(line)
        if chat:
            events.append(chat)
            continue
        advancement = None
        for pattern, kind in ADVANCEMENT_PATTERNS:
            match = pattern.search(line)
            if match:
                advancement = (match, kind)
                break
        if advancement:
            match, kind = advancement
            rendered = ADVANCEMENTS.format_event(PREFIX, match.group(1), kind, match.group(2))
            events.append(rendered)
            OPS.record_event('minecraft_event', rendered)
            continue
        for pattern, render in EVENT_PATTERNS:
            match = pattern.search(line)
            if match:
                rendered = render(match)
                events.append(rendered)
                OPS.record_event('minecraft_event', rendered)
                break
    save_state('legacy-logwatch', next_state)
    return (events, ops_alerts) if include_ops else events


def _crash_summary(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as stream:
            lines = [stream.readline() for _ in range(800)]
        description = root_cause = ''
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('Description:') and not description:
                description = stripped.split(':', 1)[1].strip()[:220]
            if stripped.startswith('Caused by:') and not root_cause:
                root_cause = stripped[:260]
            elif (not root_cause and
                  re.match(r'(?:java|net|com)\.[\w.$]+(?:Exception|Error)(?::|$)', stripped)):
                root_cause = stripped[:260]
        return root_cause or description or '关键根因暂不可读'
    except Exception:
        return 'crash-report 不可读'


def crashwatch():
    if not os.path.isdir(CRASH_DIR):
        return []
    files = sorted(name for name in os.listdir(CRASH_DIR) if name.endswith('.txt'))
    state = load_state('legacy-crashwatch')
    if state is None:
        save_state('legacy-crashwatch', {'initialized': True, 'seen': files})
        return []
    seen = set(state.get('seen', []))
    new = [name for name in files if name not in seen]
    save_state('legacy-crashwatch', {'initialized': True, 'seen': files})
    events = []
    for name in new:
        path = os.path.join(CRASH_DIR, name)
        when = datetime.fromtimestamp(os.path.getmtime(path)).astimezone().strftime('%Y-%m-%d %H:%M:%S %z')
        rendered = f'{PREFIX} 崩溃 {when}｜{name}｜{_crash_summary(path)}'
        events.append(rendered)
        OPS.record_event('crash', rendered, report=name)
    return events


def push_group(text, group_id=LLBOT_GROUP):
    request = urllib.request.Request(
        LLBOT_API + '/send_group_msg',
        data=json_bytes({'group_id': group_id, 'message': text}),
        headers={'Content-Type': JSON_CONTENT_TYPE}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = utf8_text(response.read())[:500]
        ok, data = onebot_success_utf8(raw)
        if not ok:
            print(f'{PREFIX} [push错误] group={group_id} '
                  f'retcode={(data or {}).get("retcode")}', file=sys.stderr)
        return ok
    except Exception as exc:
        print(f'{PREFIX} [push错误] group={group_id} '
              f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return False


def _flush_pending(state_name, new_events, group_id, limit, push_fn):
    pending = (load_state(state_name, {'items': []}) or {}).get('items', [])
    queue = pending + [event for event in new_events if event]
    if not queue:
        return []
    save_state(state_name, {'items': queue})
    batch = queue[:limit]
    if push_fn('\n'.join(batch), group_id):
        save_state(state_name, {'items': queue[len(batch):]})
        return batch
    return []


def scan_once(push_fn=push_group):
    watched = logwatch(include_ops=True)
    if isinstance(watched, tuple) and len(watched) == 2:
        regular_events, ops_alerts = watched
    else:  # Preserve compatibility with existing offline fixtures/mocks.
        regular_events, ops_alerts = watched, []
    crash_events = crashwatch()
    player_batch = _flush_pending(
        'legacy-monitor-pending', regular_events + crash_events,
        LLBOT_GROUP, 10, push_fn)
    _flush_pending(
        'legacy-crash-management-pending', crash_events,
        LLBOT_MANAGEMENT_GROUP, 10, push_fn)
    _flush_pending(
        'legacy-ops-management-pending', ops_alerts,
        LLBOT_MANAGEMENT_GROUP, 10, push_fn)
    if push_fn is push_group:
        STARTUP.poll(push_fn, LLBOT_GROUP)
        coordinator(OPS).deliver(lambda summary: push_fn(summary, LLBOT_GROUP))
        maintenance = OPS.periodic_maintenance()
        if maintenance['errors']:
            print(f'{PREFIX} [ops采集] ' + ','.join(maintenance['errors']), file=sys.stderr)
        weekly_status = publish_due(OPS, push_fn, LLBOT_GROUP)
        if weekly_status == 'generation-failed':
            print(f'{PREFIX} [周报] 生成失败，30分钟后重试；详见 weekly-delivery.json', file=sys.stderr)
    if player_batch:
        print('\n'.join(player_batch), flush=True)
    return player_batch


def main():
    if '--once' in sys.argv:
        scan_once()
        return
    while True:
        try:
            scan_once()
        except Exception as exc:
            print(f'{PREFIX} [monitor错误] {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()
