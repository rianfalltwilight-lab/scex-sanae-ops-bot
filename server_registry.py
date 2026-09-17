#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双服注册表与受限 RCON 路由。

配置只保存主机、端口和 secret 文件引用；密码始终在执行 RCON 时就地读取，
不会复制进注册表、日志或返回值。
"""
import html
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

from rcon_client import RconClient


REGISTRY_PATH = os.environ.get(
    'SCE_SERVER_REGISTRY', os.path.join(os.path.dirname(__file__), 'servers.json'))
SELECTION_TTL = int(os.environ.get('SCE_SERVER_SELECTION_TTL', '0'))
SELECTION_PATH = os.environ.get(
    'SCE_SERVER_SELECTION_PATH',
    os.path.join(os.path.dirname(__file__), 'logs', 'server-selections.json'))
_SELECTIONS, _SELECTIONS_LOADED_FROM = {}, None
_LOCK = threading.RLock()
_ACTIVE = ContextVar('active_server', default=None)


def _normalized_alias(value):
    value = str(value or '').strip().casefold()
    if value.startswith('[') and value.endswith(']'):
        value = value[1:-1].strip()
    return value


def _validated_rows():
    with open(REGISTRY_PATH, encoding='utf-8-sig') as stream:
        data = json.load(stream)
    rows = data.get('servers', data)
    if not isinstance(rows, list):
        raise ValueError('servers.json: servers 必须是数组')
    result = []
    seen_ids = set()
    seen_prefixes = set()
    for raw in rows:
        if not raw.get('enabled', True):
            continue
        item = dict(raw)
        item['id'] = str(item.get('id') or item.get('name') or '').strip().casefold()
        item['name'] = str(item.get('name') or item['id']).strip()
        item['prefix'] = str(item.get('prefix') or '').strip()
        if not item['id'] or not item['prefix']:
            raise ValueError('servers.json: 每服必须设置 id/name/prefix')
        if item['id'] in seen_ids or item['prefix'] in seen_prefixes:
            raise ValueError('servers.json: id 和 prefix 必须唯一')
        seen_ids.add(item['id'])
        seen_prefixes.add(item['prefix'])
        aliases = [item['id'], item['name'], item['prefix']]
        aliases.extend(item.get('aliases') or [])
        item['aliases'] = sorted({_normalized_alias(value) for value in aliases if value})
        result.append(item)
    return result


def load_servers():
    """返回按 id 索引的启用服务器副本。"""
    return {item['id']: item for item in _validated_rows()}


def list_servers():
    return _validated_rows()


def resolve_server(value):
    needle = _normalized_alias(value)
    if not needle:
        return None
    for item in _validated_rows():
        if needle in item['aliases']:
            return item
    return None


def server_prefix(server):
    return str((server or {}).get('prefix') or '[未知服]')


def server_hint():
    """Compact user-facing list of currently enabled operation targets."""
    prefixes = [server_prefix(item) for item in list_servers()]
    return '/'.join(prefixes) if prefixes else '[无可用服务器]'


def _load_selections_locked():
    """Load non-sensitive per-user operation targets once per state path."""
    global _SELECTIONS_LOADED_FROM
    path = os.path.abspath(SELECTION_PATH)
    if _SELECTIONS_LOADED_FROM == path:
        return
    _SELECTIONS.clear()
    try:
        with open(path, encoding='utf-8-sig') as stream:
            data = json.load(stream)
        for row in data.get('selections', []):
            group_id = str(row.get('group_id') or '')
            user_id = str(row.get('user_id') or '')
            server_id = str(row.get('server_id') or '').casefold()
            expires_at = row.get('expires_at')
            if group_id and user_id and server_id:
                _SELECTIONS[(group_id, user_id)] = (
                    server_id, float(expires_at) if expires_at is not None else None)
    except (OSError, ValueError, TypeError):
        pass
    _SELECTIONS_LOADED_FROM = path


def _save_selections_locked():
    path = os.path.abspath(SELECTION_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = []
    for (group_id, user_id), (server_id, expires_at) in sorted(_SELECTIONS.items()):
        rows.append({
            'group_id': group_id,
            'user_id': user_id,
            'server_id': server_id,
            'expires_at': expires_at,
        })
    payload = {'version': 1, 'updated_at': time.time(), 'selections': rows}
    temp = path + '.tmp'
    with open(temp, 'w', encoding='utf-8', newline='\n') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def select_server(group_id, user_id, name):
    item = resolve_server(name)
    if item:
        with _LOCK:
            _load_selections_locked()
            expires_at = time.time() + SELECTION_TTL if SELECTION_TTL > 0 else None
            _SELECTIONS[(str(group_id), str(user_id))] = (
                item['id'], expires_at)
            _save_selections_locked()
    return item


def get_selected(group_id, user_id):
    key = (str(group_id), str(user_id))
    with _LOCK:
        _load_selections_locked()
        value = _SELECTIONS.get(key)
        if not value:
            return None, '未设置操作目标'
        server_id, expires = value
        if expires is not None and time.time() > expires:
            _SELECTIONS.pop(key, None)
            _save_selections_locked()
            return None, '操作目标已过期'
    item = load_servers().get(server_id)
    return (item, None) if item else (None, '所选服务器已不在允许清单')


def clear_selected(group_id, user_id):
    """Clear a saved operation target; return whether one existed."""
    key = (str(group_id), str(user_id))
    with _LOCK:
        _load_selections_locked()
        existed = _SELECTIONS.pop(key, None) is not None
        if existed:
            _save_selections_locked()
    return existed


def _read_password(server):
    path = os.path.expanduser(server['password_file'])
    with open(path, encoding='utf-8-sig') as stream:
        raw = stream.read()
    if server.get('password_json_field'):
        return str(json.loads(raw)[server['password_json_field']]).strip()
    return raw.strip()


def query_server(server, command):
    if not server:
        raise ValueError('未指定服务器')
    password = _read_password(server)
    try:
        with RconClient(server.get('host', '127.0.0.1'), int(server['port']), password) as client:
            return client.exec(command)
    finally:
        password = None


def parse_player_list(output):
    """解析原版/常见汉化 list 输出，无法确认人数时返回 None。"""
    text = str(output or '').strip()
    patterns = (
        r'There are\s+(\d+)\s+of a max of\s+(\d+)\s+players online:\s*(.*)$',
        r'当前共有\s*(\d+)\s*个玩家在线[，,]\s*服务器最多可容纳\s*(\d+)\s*个玩家[：:]\s*(.*)$',
    )
    match = next((found for pattern in patterns
                  if (found := re.search(pattern, text, re.I | re.S))), None)
    if not match:
        return None
    names = [name.strip() for name in match.group(3).split(',') if name.strip()]
    return {'current': int(match.group(1)), 'max': int(match.group(2)), 'players': names}


def extract_server_selector(text):
    """从任意位置的 ``[nast]``/``[怀旧]`` 提取服务器并移除一次标记。"""
    original = html.unescape(str(text or ''))
    for match in re.finditer(r'\[([^\]\r\n]{1,32})\]', original):
        server = resolve_server(match.group(1))
        if server:
            cleaned = (original[:match.start()] + ' ' + original[match.end():]).strip()
            return server, re.sub(r'\s+', ' ', cleaned)
    return None, original.strip()


def extract_natural_server_selector(text):
    """Extract a per-message operation target from brackets or natural names."""
    server, cleaned = extract_server_selector(text)
    if server:
        return server, cleaned
    original = html.unescape(str(text or ''))
    patterns = (
        (resolve_server('legacy'), re.compile(r'(?i)(?:SCEX\s*Legacy\s*Genesis|Legacy\s*Genesis|怀旧(?:服|服务器))')),
        (resolve_server('nast'), re.compile(r'(?i)(?<![A-Za-z0-9_])NAST(?:服|服务器)?(?![A-Za-z0-9_])')),
    )
    found = []
    for item, pattern in patterns:
        match = pattern.search(original)
        if item and match:
            found.append((item, match))
    if len({item['id'] for item, _match in found}) != 1:
        return None, original.strip()
    item, match = found[0]
    cleaned = (original[:match.start()] + ' ' + original[match.end():]).strip()
    return item, re.sub(r'\s+', ' ', cleaned)


def _chat_component(value, limit):
    value = re.sub(r'[\r\n\t]+', ' ', str(value or '')).strip()
    return re.sub(r'\s{2,}', ' ', value)[:limit]


def fanout_group_message(nickname, text, servers=None, query_fn=None):
    """把一条普通群文本逐服 best-effort 发送，返回逐服结果。"""
    query_fn = query_fn or query_server
    servers = list(servers if servers is not None else list_servers())
    nick = _chat_component(nickname, 64) or '群友'
    content = _chat_component(text, 320)
    if not content:
        return []
    results = []
    for server in servers:
        command = f'say [QQ群] {nick}: {content}'
        try:
            query_fn(server, command)
            results.append({'server': server, 'ok': True, 'error': ''})
        except Exception as exc:
            results.append({'server': server, 'ok': False,
                            'error': type(exc).__name__ + ': ' + str(exc)[:160]})
    return results


@contextmanager
def server_context(server):
    token = _ACTIVE.set(server)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def active_server():
    return _ACTIVE.get()
