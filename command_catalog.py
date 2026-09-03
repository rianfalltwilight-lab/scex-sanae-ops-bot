#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small, auditable cache of the command tree registered by each MC server."""
import json
import os
import re
import tempfile
import time
from pathlib import Path


CATALOG_PATH = Path(os.environ.get(
    'SANAE_COMMAND_CATALOG',
    Path(__file__).with_name('resources') / 'server-command-catalog.json'))

# Roots registered by vanilla 1.21.x. Everything else is marked "extended";
# that bucket intentionally includes server utility mods and permissions mods.
VANILLA_ROOTS = {
    'advancement', 'attribute', 'execute', 'bossbar', 'clear', 'clone', 'damage',
    'data', 'datapack', 'debug', 'defaultgamemode', 'difficulty', 'effect', 'me',
    'enchant', 'experience', 'xp', 'fill', 'fillbiome', 'forceload', 'function',
    'gamemode', 'gamerule', 'give', 'help', 'item', 'kick', 'kill', 'list',
    'locate', 'loot', 'msg', 'tell', 'w', 'particle', 'place', 'playsound',
    'random', 'reload', 'recipe', 'return', 'ride', 'say', 'schedule',
    'scoreboard', 'seed', 'setblock', 'spawnpoint', 'setworldspawn', 'spectate',
    'spreadplayers', 'stopsound', 'summon', 'tag', 'team', 'teammsg', 'tm',
    'teleport', 'tp', 'tellraw', 'tick', 'time', 'title', 'trigger', 'weather',
    'worldborder', 'jfr', 'ban-ip', 'banlist', 'ban', 'deop', 'op', 'pardon',
    'pardon-ip', 'perf', 'save-all', 'save-off', 'save-on', 'setidletimeout',
    'stop', 'transfer', 'whitelist',
}

QUERY_ALIASES = {
    '蓝图': ('buildinggadgets2', 'redprints'),
    '建筑小帮手': ('buildinggadgets2', 'redprints'),
    '地图': ('bluemap',),
    '蓝图地图': ('bluemap',),
    '背包': ('sophisticatedbackpacks', 'sbp'),
    '精妙背包': ('sophisticatedbackpacks', 'sbp'),
    '世界编辑': ('worldedit', '//'),
    '创世神': ('worldedit', '//'),
    '备份': ('simplebackups',),
    '区块预生成': ('chunky',),
    '任务': ('ftbquests', 'questenhance'),
    '团队': ('ftbteams',),
    '传送石': ('waystones',),
    '机械动力': ('create', 'catnip'),
    '应用能源': ('ae2',),
    '脚本': ('kubejs',),
}

PLAYER_CONTEXT_PREFIXES = ('buildinggadgets2 redprints',)


def normalize_console_command(command):
    value = str(command or '').replace('\x00', '').strip()
    if '\r' in value or '\n' in value:
        raise ValueError('命令只能有一行')
    if value.startswith('/') and not value.startswith('//'):
        value = value[1:]
    value = value.strip()
    if not value or len(value) > 500:
        raise ValueError('命令为空或过长')
    return value


def command_token(command):
    value = normalize_console_command(command)
    first = value.split(None, 1)[0]
    return first if first.startswith('//') else '/' + first.casefold()


def requires_player_context(command):
    try:
        value = normalize_console_command(command).casefold()
    except ValueError:
        return False
    return any(value == prefix or value.startswith(prefix + ' ')
               for prefix in PLAYER_CONTEXT_PREFIXES)


def parse_help(output):
    rows, seen = [], set()
    for raw in str(output or '').splitlines():
        usage = raw.strip()
        if not usage.startswith('/'):
            continue
        token = usage.split(None, 1)[0]
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        root = token[1:].casefold() if not token.startswith('//') else token.casefold()
        rows.append({
            'token': token,
            'root': root,
            'usage': usage[:1200],
            'kind': 'vanilla' if root in VANILLA_ROOTS else 'extended',
            'requires_player_context': any(
                usage[1:].casefold() == prefix or usage[1:].casefold().startswith(prefix + ' ')
                for prefix in PLAYER_CONTEXT_PREFIXES),
        })
    return rows


def _load(path=None):
    target = Path(path or CATALOG_PATH)
    try:
        return json.loads(target.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, TypeError):
        return {'schema': 1, 'servers': {}}


def _save(payload, path=None):
    target = Path(path or CATALOG_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=target.name + '.', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def refresh_server(server, query_fn, path=None):
    output = query_fn(server, 'help')
    commands = parse_help(output)
    if not commands:
        raise RuntimeError('RCON help 没有返回可解析命令')
    payload = _load(path)
    servers = payload.setdefault('servers', {})
    servers[str(server['id'])] = {
        'name': server.get('name', server['id']),
        'prefix': server.get('prefix', ''),
        'scanned_at': time.time(),
        'count': len(commands),
        'extended_count': sum(row['kind'] == 'extended' for row in commands),
        'commands': commands,
    }
    payload['schema'] = 1
    payload['updated_at'] = time.time()
    _save(payload, path)
    return servers[str(server['id'])]


def _server_rows(server_id, path=None):
    return (_load(path).get('servers', {}).get(str(server_id), {}) or {}).get('commands', [])


def search(server_id, query='', limit=20, path=None):
    rows = _server_rows(server_id, path)
    if not rows:
        return '本服命令目录尚未扫描，不能猜测命令。'
    raw_query = str(query or '').strip().casefold()
    terms = [raw_query] if raw_query else []
    for key, aliases in QUERY_ALIASES.items():
        if key in raw_query:
            terms.extend(aliases)
    terms.extend(part for part in re.split(r'\s+', raw_query) if part)
    terms = list(dict.fromkeys(term for term in terms if term))
    scored = []
    for row in rows:
        haystack = (row['token'] + ' ' + row['usage']).casefold()
        if not terms:
            score = 1 if row['kind'] == 'extended' else 0
        else:
            score = sum(4 if term in row['token'].casefold() else 1
                        for term in terms if term in haystack)
        if score:
            scored.append((score, row['kind'] == 'extended', row['usage']))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2].casefold()))
    usages = [item[2] for item in scored[:max(1, min(int(limit), 40))]]
    if not usages:
        return f'本服命令目录中没有匹配“{query}”的注册命令。'
    rendered = []
    for usage in usages:
        suffix = (' 〔RCON 需指定在线玩家上下文〕'
                  if requires_player_context(usage[1:]) else '')
        rendered.append(usage + suffix)
    return '本服已注册命令匹配：\n' + '\n'.join(rendered)


def validate(server_id, command, path=None):
    try:
        value = normalize_console_command(command)
        token = command_token(value).casefold()
    except ValueError as exc:
        return False, '', str(exc)
    rows = _server_rows(server_id, path)
    matches = [row for row in rows if row['token'].casefold() == token]
    if not matches:
        return False, '', f'命令根 {token} 不在本服已扫描目录中'
    usage = matches[0]['usage']
    return True, usage, ''


def summary(server_id, path=None):
    server = (_load(path).get('servers', {}).get(str(server_id), {}) or {})
    return {
        'count': int(server.get('count') or 0),
        'extended_count': int(server.get('extended_count') or 0),
        'scanned_at': server.get('scanned_at'),
    }
