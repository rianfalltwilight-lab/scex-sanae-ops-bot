"""Who and what uses the game server's network (round 26, 10-27).

topic=players (everyone): one read-only SSH call runs `nettop` twice, PLAYERS_SECONDS apart, on
the server JVM and keeps the game-port connections. Each connection is matched to a player on
the host through the join lines in logs/ ("Name[/address:port] logged in with entity id"), so
only player names and byte counts come back; addresses never leave the host. Members see the
totals and the anonymous spread, admins the names and what is loaded around each player.

topic=packets (admins start it): the vanilla `/jfr start`, PACKET_SECONDS later `/jfr stop`.
The server then writes debug/<name>.jfr (about 15 MB per 30 s on 10-27) and
debug/jfr-report-<name>.json, whose `network` part lists the packet types that carried the most
bytes. One SSH call reads that report and sums the recording's minecraft.NetworkSummary events per
connection (bytes per player). One sample at a time, a fresh one at most every SAMPLE_COOLDOWN
seconds, the stop is always attempted, and a marker file lets a later call stop a sample that a
bot restart left running. Members get the latest sample's packet types without names.

The 10-27 test sample (30 s, 5 players, MSPT 49 -> 51-59 -> 56, within its usual spread):
27 MB sent, block entity data 60 %, mod payloads 15 %, chunks 14 %; about 7 Mbps in total,
far below the uplink, so bandwidth was not what players felt as lag.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import remote_server_reader
import world_status

TZ = timezone(timedelta(hours=8))
PLAYERS_SECONDS = 5
PLAYERS_CACHE_SECONDS = 30
PACKET_SECONDS = (20, 30, 60)          # minimum, default, maximum
SAMPLE_COOLDOWN = 300
LAST_SAMPLE_HOURS = 24                 # members see the latest sample while it is this fresh
SAMPLE_FILE = 'traffic-sample.json'
MARKER_FILE = 'traffic-jfr-running.json'
_LOCK = threading.Lock()
_PLAYERS_CACHE = {}
_sleep = time.sleep

# Common host-side code: the server JVM, address normalisation and the log join lines.
_PRELUDE = r"""
import base64, glob, gzip, ipaddress, json, os, re, subprocess, time
cfg = json.loads(base64.b64decode('__CONFIG__'))
out = {'errors': []}

def run(args, timeout):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception as exc:
        out['errors'].append(args[0].rsplit('/', 1)[-1] + ':' + type(exc).__name__)
        return ''

def java():
    first = ('', '')
    for line in run(['/bin/ps', '-axo', 'pid=,command='], 10).splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[1].split()[0].endswith('/bin/java'):
            continue
        row = (parts[0], parts[1].split()[0])
        if 'neoforge' in parts[1] or cfg['root'] in parts[1]:
            return row
        first = first if first[0] else row
    return first

def norm(address):
    address = address.strip('[]').split('%')[0]
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address.lower()
    mapped = getattr(ip, 'ipv4_mapped', None)
    return str(mapped or ip)

LAN = [ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16',
                                         'fc00::/7', 'fe80::/10')]

def kind(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return 'other'
    if ip.is_loopback:
        return 'loopback'
    return 'lan' if any(ip.version == net.version and ip in net for net in LAN) else 'public'

JOIN = re.compile(r'\]: ([A-Za-z0-9_]{1,16})\[/([0-9a-fA-F:.\[\]%]+?):(\d+)\] logged in with entity id')

def scan(path):
    found = {}
    try:
        with (gzip.open if path.endswith('.gz') else open)(path, 'rb') as stream:
            for raw in stream:
                if b'logged in with entity id' in raw:
                    match = JOIN.search(raw.decode('utf-8', 'replace'))
                    if match:
                        found[(norm(match.group(2)), match.group(3))] = match.group(1)
    except (OSError, EOFError):
        pass
    return found

def joins(need):
    # (address, port) -> player; older logs only when latest.log (rolled at midnight) lacks someone.
    logs = os.path.join(cfg['root'], 'logs')
    found = scan(os.path.join(logs, 'latest.log'))
    for path in sorted(glob.glob(os.path.join(logs, '*.log.gz')), key=os.path.getmtime, reverse=True)[:2]:
        if need <= set(found):
            break
        for key, name in scan(path).items():
            found.setdefault(key, name)
    return found
"""

_PLAYERS_SCRIPT = _PRELUDE + r"""
pid, binary = java()
out['found'] = bool(pid)
blocks = []
if pid:
    text = run(['/usr/bin/nettop', '-L', '2', '-s', str(cfg['seconds']), '-m', 'tcp', '-n', '-p', pid,
                '-J', 'bytes_in,bytes_out,re-tx,rtt_avg', '-x'], cfg['seconds'] + 20)
    header = []
    for line in text.splitlines():
        if line.startswith(',bytes_in'):
            header = line.strip(',').split(',')
            blocks.append({'process': None, 'conns': {}})
            continue
        parts = line.split(',')
        row = dict(zip(header, parts[1:]))
        if not blocks or not (row.get('bytes_in', '').isdigit() and row.get('bytes_out', '').isdigit()):
            continue
        if '<->' not in parts[0]:
            blocks[-1]['process'] = (int(row['bytes_in']), int(row['bytes_out']))
            continue
        proto, _, pair = parts[0].partition(' ')
        local, _, remote = pair.partition('<->')
        sep = ':' if proto == 'tcp4' else '.'
        address, _, port = remote.rpartition(sep)
        if local.rpartition(sep)[2] == str(cfg['port']) and port.isdigit():
            rtt = re.match(r'\s*([\d.]+)', row.get('rtt_avg') or '')
            blocks[-1]['conns'][(norm(address), port)] = (
                int(row['bytes_in']), int(row['bytes_out']),
                int(row['re-tx']) if (row.get('re-tx') or '').isdigit() else 0, float(rtt.group(1)) if rtt else None)
if len(blocks) >= 2:
    first, last = blocks[0], blocks[-1]
    names = joins(set(last['conns']))
    rows = []
    for key, (total_in, total_out, retx, rtt) in last['conns'].items():
        before_in, before_out, before_retx, _rtt = first['conns'].get(key, (total_in, total_out, retx, rtt))
        rows.append({'player': names.get(key, ''), 'kind': kind(key[0]), 'in': max(0, total_in - before_in),
                     'out': max(0, total_out - before_out), 'in_total': total_in, 'out_total': total_out,
                     'retx': max(0, retx - before_retx), 'retx_total': retx, 'rtt': rtt})
    out['conns'] = sorted(rows, key=lambda row: -row['out'])
elif pid:
    out['errors'].append('nettop:no-sample')
print(json.dumps(out))
"""

_PACKETS_SCRIPT = _PRELUDE + r"""
debug = os.path.join(cfg['root'], 'debug')
name = cfg.get('file') or ''
path = os.path.join(debug, name) if re.fullmatch(r'[A-Za-z0-9_.-]{1,120}\.jfr', name) else ''
if not (path and os.path.isfile(path)):
    recent = sorted(glob.glob(os.path.join(debug, '*.jfr')), key=os.path.getmtime)
    path = recent[-1] if recent and time.time() - os.path.getmtime(recent[-1]) < 300 else ''
out['file'] = os.path.basename(path)
if path:
    out['size'] = os.path.getsize(path)
    report_path = os.path.join(debug, 'jfr-report-' + os.path.basename(path)[:-4] + '.json')
    deadline = time.time() + cfg.get('wait', 30)
    report = None
    while report is None:
        try:
            with open(report_path, encoding='utf-8') as stream:
                report = json.load(stream)
        except (OSError, ValueError):
            if time.time() > deadline:
                out['errors'].append('report:missing')
                break
            time.sleep(1)
    if report:
        out['report'] = {key: report.get(key) for key in ('durationMs', 'serverTick', 'network', 'cpuPercent')}
    pid, binary = java()
    if binary:
        tool = binary.rsplit('/', 1)[0] + '/jfr'
        events = None
        for options in (['-J-Xmx512m'], []):
            try:
                events = json.loads(run([tool] + options + ['print', '--json', '--events', 'minecraft.NetworkSummary',
                                                            path], 150))['recording']['events']
                break
            except (ValueError, KeyError, TypeError):
                continue
        if events is None:
            out['errors'].append('jfr:print')
        else:
            sums = {}
            for event in events:
                values = event.get('values') or {}
                match = re.match(r'^[^/]*/\[?([0-9a-fA-F:.%]+?)\]?:(\d+)$', str(values.get('remoteAddress') or ''))
                row = sums.setdefault((norm(match.group(1)), match.group(2)) if match else ('?', '?'), [0, 0, 0, 0])
                for index, field in enumerate(('sentBytes', 'sentPackets', 'receivedBytes', 'receivedPackets')):
                    row[index] += int(values.get(field) or 0)
            names = joins(set(sums))
            out['players'] = sorted(({'player': names.get(key, ''), 'kind': kind(key[0]), 'sent': row[0],
                                      'sent_packets': row[1], 'received': row[2], 'received_packets': row[3]}
                                     for key, row in sums.items()), key=lambda row: -row['sent'])
files = [f for f in glob.glob(os.path.join(debug, '*')) if f.endswith('.jfr') or os.path.basename(f).startswith('jfr-report-')]
out['debug'] = {'files': len(files), 'bytes': sum(os.path.getsize(f) for f in files)}
print(json.dumps(out))
"""

# Packet IDs (minecraft: namespace dropped) to what they carry.
PACKET_LABELS = {
    'block_entity_data': '方块实体数据', 'custom_payload': '模组自定义包',
    'level_chunk_with_light': '区块（含光照）', 'light_update': '光照更新', 'forget_level_chunk': '卸载区块',
    'chunk_batch_start': '区块批次', 'chunk_batch_finished': '区块批次', 'set_chunk_cache_center': '区块中心',
    'section_blocks_update': '方块批量变化', 'block_update': '单个方块变化', 'block_event': '方块事件（活塞、箱子开合等）',
    'block_destruction': '方块破坏进度', 'level_event': '世界事件', 'level_particles': '粒子', 'explode': '爆炸',
    'sound': '声音', 'sound_entity': '实体声音',
    'update_attributes': '实体属性更新', 'set_entity_data': '实体数据', 'set_entity_motion': '实体速度',
    'move_entity_pos': '实体移动', 'move_entity_pos_rot': '实体移动和转向', 'move_entity_rot': '实体转向',
    'rotate_head': '实体转头', 'teleport_entity': '实体传送', 'entity_position_sync': '实体位置同步',
    'add_entity': '生成实体', 'remove_entities': '移除实体', 'set_equipment': '实体装备', 'entity_event': '实体事件',
    'animate': '动作动画', 'set_passengers': '乘骑', 'take_item_entity': '拾取物品', 'update_mob_effect': '药水效果',
    'remove_mob_effect': '移除药水效果', 'hurt_animation': '受伤动画', 'damage_event': '伤害事件',
    'container_set_content': '容器内容', 'container_set_slot': '容器格子', 'container_set_data': '容器数据',
    'player_info_update': '玩家列表', 'player_info_remove': '玩家列表', 'set_time': '时间同步', 'keep_alive': '心跳',
    'system_chat': '系统消息', 'player_chat': '聊天', 'boss_event': 'Boss 血条', 'map_item_data': '地图',
    'set_health': '生命值', 'set_experience': '经验', 'update_advancements': '进度同步', 'update_recipes': '配方同步',
    'update_tags': '标签同步', 'commands': '命令列表', 'registry_data': '注册表（进服时）', 'bundle_delimiter': '包组分隔',
    'set_score': '计分板', 'set_objective': '计分板', 'set_display_objective': '计分板',
    'move_player_pos': '玩家移动', 'move_player_pos_rot': '玩家移动和转向', 'move_player_rot': '玩家转向',
    'move_player_status_only': '玩家着地状态', 'client_tick_end': '客户端刻结束', 'chunk_batch_received': '区块批次回执',
    'player_input': '玩家按键', 'player_command': '玩家动作', 'player_action': '挖掘/丢弃等动作', 'swing': '挥手',
    'use_item_on': '对方块使用物品', 'use_item': '使用物品', 'container_click': '点击容器格子', 'chat': '聊天',
    'chat_command': '命令', 'pong': '回应', 'accept_teleportation': '确认传送', 'move_vehicle': '载具移动',
}
# Explained once, for the types that make the top of a sample.
PACKET_NOTES = {
    'block_entity_data': '方块实体数据是机器、管道、箱子、告示牌等方块的状态同步，玩家附近这类方块越多、更新越频繁就越大',
    'custom_payload': '模组自定义包是各模组自己的同步数据，原版统计分不出是哪个模组发的',
    'level_chunk_with_light': '区块是玩家走到新地方或传送时整块发送的地形，跑图时会变多',
    'update_attributes': '实体属性更新是生物、玩家的属性（血量上限、速度等）变化',
    'container_set_content': '容器内容是打开的箱子、机器界面里的物品',
    'container_set_data': '容器数据是打开的机器界面里的进度条、能量等数值',
}
_PHASES = {'configuration': '进服配置阶段', 'login': '登录', 'status': '服务器列表'}


def packet_label(packet_id, protocol=''):
    packet_id = str(packet_id or '?')
    namespace, _, path = packet_id.rpartition(':')
    label = PACKET_LABELS.get(path, path) if namespace in ('', 'minecraft') else packet_id
    phase = _PHASES.get(str(protocol or ''))
    return f'{label}（{phase}）' if phase else label


def _size(count):
    count = float(count or 0)
    if count >= 1024 * 1024:
        return f'{count / 1024 / 1024:.1f} MB'
    if count >= 1024:
        return f'{count / 1024:.0f} KB'
    return f'{count:.0f} B'


def _rate(count, seconds):
    return _size(count / max(seconds, 0.001)) + '/s'


def _mbps(count, seconds):
    return count * 8 / max(seconds, 0.001) / 1_000_000


def _count(value):
    value = int(value or 0)
    return f'{value / 10000:.1f}万' if value >= 10000 else str(value)


def _clock(epoch):
    return f'{datetime.fromtimestamp(epoch or time.time(), TZ):%H:%M}'


def _remote_port(server):
    """The game port on the host (server.properties of the mirror, 25565 when unknown)."""
    try:
        text = (Path(server.get('server_dir') or '') / 'server.properties').read_text(encoding='utf-8', errors='replace')
        match = re.search(r'^server-port=(\d+)\s*$', text, re.M)
        return int(match.group(1)) if match else 25565
    except OSError:
        return 25565


# ===== topic=players =====

def collect_players(server, *, runner=None, now=None):
    now = time.time() if now is None else now
    hit = _PLAYERS_CACHE.get(server['id'])
    if hit and now - hit[0] < PLAYERS_CACHE_SECONDS:
        return hit[1]
    data = remote_server_reader._run(server, _PLAYERS_SCRIPT,
                                     {'seconds': PLAYERS_SECONDS, 'port': _remote_port(server)},
                                     runner=runner, timeout=PLAYERS_SECONDS + 45)
    data['at'], data['seconds'] = now, PLAYERS_SECONDS
    _PLAYERS_CACHE[server['id']] = (now, data)
    return data


def _nearby(query):
    """{player: (block entities, entities)} loaded around each online player (ServerCore)."""
    out = {}
    for index, command in enumerate(('statistics block-entities byPlayer', 'statistics entities byPlayer')):
        try:
            rows = world_status.parse_statistics(query(command)) or []
        except Exception:
            rows = []
        for name, count in rows:
            out.setdefault(name, [None, None])[index] = count
    return out


def _packet_brief(sample, top=4):
    sent = ((sample.get('report') or {}).get('network') or {}).get('sent') or {}
    total = sent.get('totalBytes') or 0
    rows = sent.get('topContributors') or []
    if not total or not rows:
        return ''
    return '、'.join(f'{packet_label(row.get("packetId"), row.get("protocolId"))} '
                     f'{100 * (row.get("totalBytes") or 0) / total:.0f}%' for row in rows[:top])


def format_players(prefix, data, detailed=False, tick=None, nearby=None, last_sample=None):
    seconds = data.get('seconds') or PLAYERS_SECONDS
    lines = [f'{prefix} 服务器流量（在服务器主机上实测 {seconds} 秒，{_clock(data.get("at"))}；30 秒内重复问用同一次结果）']
    conns = data.get('conns')
    if not data.get('found'):
        return lines[0] + '\n没找到服务器的 Java 进程，服务器可能没开。'
    if conns is None:
        return lines[0] + '\n这次没测到连接数据（' + ('、'.join(data.get('errors') or []) or '未知原因') + '）。'
    players = [row for row in conns if row.get('player')]
    others = [row for row in conns if not row.get('player')]
    total_out = sum(row['out'] for row in conns)
    total_in = sum(row['in'] for row in conns)
    mbps = _mbps(total_out, seconds)
    lines.append(f'- 服务器发给玩家（下行）{_rate(total_out, seconds)}，约 {mbps:.1f} Mbps；玩家发来（上行）'
                 f'{_rate(total_in, seconds)}；游戏端口 {len(conns)} 个连接，对应到在线玩家 {len(players)} 个')
    verdicts = []
    if players:
        top_share = 100 * players[0]['out'] / total_out if total_out else 0
        if detailed:
            parts = []
            for row in players[:10]:
                text = (f'{row["player"]} ↓{_rate(row["out"], seconds)} ↑{_rate(row["in"], seconds)}'
                        f'（本次登录共收 {_size(row["out_total"])}）')
                near = (nearby or {}).get(row['player'])
                if near and any(value is not None for value in near):
                    text += f'［附近方块实体 {near[0] if near[0] is not None else "?"}、实体 {near[1] if near[1] is not None else "?"}］'
                parts.append(text)
            lines.append('- 按玩家（管理员可见，↓=服务器发给他）：' + '；'.join(parts))
        else:
            lines.append('- 各玩家的下行（不显示名字）：' + '、'.join(_rate(row['out'], seconds) for row in players[:10])
                         + (f'；最多的一位占 {top_share:.0f}%' if len(players) > 1 else ''))
        if len(players) >= 3 and top_share >= 50:
            who = f'（{players[0]["player"]}）' if detailed else ''
            verdicts.append(f'流量集中在一位玩家身上{who}，占 {top_share:.0f}%：常见原因是他附近机器多、方块实体同步频繁，'
                            '或者在跑图、传送加载新区块')
    if others:
        lines.append(f'- 另有 {len(others)} 个连接没对应到玩家（服务器列表刷新、正在登录或别的程序查询），'
                     f'共 {_rate(sum(row["out"] for row in others), seconds)}')
    if tick:
        lines.append('- ' + world_status.tick_line(tick))
    brief = _packet_brief(last_sample or {})
    if brief:
        lines.append(f'- 最近一次数据包采样（{_clock(last_sample.get("at"))}）里发得最多的：{brief}')
    if mbps < 20:
        verdicts.append(f'总流量约 {mbps:.1f} Mbps，不大，一般家用宽带上行扛得住，延迟多半不是带宽造成的')
    else:
        verdicts.append(f'总流量约 {mbps:.1f} Mbps，已经不小；上行带宽只有几十 Mbps 的话会挤占，可以用 !ping 测一下上行')
    if tick and tick.get('average', 0) > tick.get('target', 50):
        verdicts.append(f'每刻平均 {tick["average"]:.0f} ms，超过 {tick.get("target", 50):.0f} ms，服务器本身跟不上，'
                        '所有人都会觉得“延迟”，这比流量更可能是原因')
    lines.append('判断：' + '；'.join(verdicts) + '。')
    lines.append('说明：丢包和线路问题看 server_network；只统计游戏端口，玩家自己那边的网络测不到。')
    return '\n'.join(lines)


def players_report(server, query, state_dir, privileged, *, runner=None):
    prefix = server['prefix']
    try:
        data = collect_players(server, runner=runner)
    except Exception as exc:
        return f'{prefix} 服务器主机暂时连不上（{type(exc).__name__}），这次测不了流量。'
    try:
        tick = world_status.parse_tick_query(query('tick query'))
    except Exception:
        tick = None
    nearby = _nearby(query) if privileged and data.get('conns') else None
    last = load_sample(state_dir)
    if last and time.time() - (last.get('at') or 0) > LAST_SAMPLE_HOURS * 3600:
        last = None
    return format_players(prefix, data, detailed=privileged, tick=tick, nearby=nearby, last_sample=last)


# ===== topic=packets =====

def load_sample(state_dir):
    try:
        return json.loads((Path(state_dir) / SAMPLE_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return None


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)


def recover(state_dir, query, now=None):
    """Stop a JFR sample that a bot restart left running (marker older than its sample plus 2 min)."""
    marker = Path(state_dir) / MARKER_FILE
    if not marker.exists() or _LOCK.locked():
        return False
    now = time.time() if now is None else now
    try:
        data = json.loads(marker.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = {}
    if now - float(data.get('at') or 0) < float(data.get('seconds') or PACKET_SECONDS[2]) + 120:
        return False
    try:
        print('[流量采样] 停止上次遗留的 JFR 采样：' + str(query('jfr stop')).strip()[:120], flush=True)
    except Exception:
        return False
    marker.unlink(missing_ok=True)
    return True


def _stop(query):
    for attempt in range(2):
        try:
            text = str(query('jfr stop') or '')
            if 'stop' in text.lower():
                return text
        except Exception:
            text = ''
        if not attempt:
            _sleep(2)
    return text


def sample_packets(server, query, state_dir, seconds=None, *, runner=None, now=time.time):
    """(data, state): state is 'fresh', 'cached', 'busy' or 'failed: <reason>'."""
    try:
        seconds = max(PACKET_SECONDS[0], min(PACKET_SECONDS[2], int(seconds or PACKET_SECONDS[1])))
    except (TypeError, ValueError):
        seconds = PACKET_SECONDS[1]
    if not _LOCK.acquire(blocking=False):
        return None, 'busy'
    try:
        last = load_sample(state_dir)
        if last and now() - (last.get('at') or 0) < SAMPLE_COOLDOWN:
            return last, 'cached'
        started = str(query('jfr start') or '').strip()
        if 'started' not in started.lower() or 'fail' in started.lower():
            # Someone else's recording, or JFR is unavailable: never stop what this did not start.
            return None, 'failed: ' + (started[:120] or '无输出')
        began = time.monotonic()
        marker = Path(state_dir) / MARKER_FILE
        _write_json(marker, {'at': now(), 'seconds': seconds})
        print(f'{server["prefix"]} [流量采样] JFR {seconds} 秒', flush=True)
        try:
            _sleep(seconds)
        finally:
            stopped = _stop(query)
            elapsed = time.monotonic() - began
            if 'stop' in stopped.lower():
                marker.unlink(missing_ok=True)
        match = re.search(r'([A-Za-z0-9_.-]{1,120}\.jfr)', stopped)
        data = remote_server_reader._run(server, _PACKETS_SCRIPT, {'file': match.group(1) if match else '', 'wait': 30},
                                         runner=runner, timeout=200)
        data.update(at=now(), seconds=seconds, elapsed=round(elapsed, 1))
        if (data.get('report') or {}).get('network'):
            _write_json(Path(state_dir) / SAMPLE_FILE, data)
            return data, 'fresh'
        return data, 'failed: ' + ('、'.join(data.get('errors') or []) or '没有生成统计报告')
    finally:
        _LOCK.release()


def _sample_seconds(data):
    """Start-to-stop time measured by the bot.

    The report's durationMs can reach back to an earlier recording in the same JVM: on 10-27 the
    second 30-second sample reported 905 s, which made every rate 30 times too small.
    """
    if (data.get('elapsed') or 0) >= 1:
        return data['elapsed']
    asked = data.get('seconds') or PACKET_SECONDS[1]
    duration = ((data.get('report') or {}).get('durationMs') or 0) / 1000
    return duration if 0 < duration <= asked * 1.5 + 10 else asked


def format_packets(prefix, data, detailed=False, cached_minutes=None):
    report = data.get('report') or {}
    network = report.get('network') or {}
    seconds = _sample_seconds(data)
    head = f'{prefix} 数据包采样（原版 JFR，{seconds:.0f} 秒，{_clock(data.get("at"))}）'
    if cached_minutes is not None:
        head += f'（约 {cached_minutes} 分钟前的结果；5 分钟内不重复采样）'
    lines = [head]
    sent, received = network.get('sent') or {}, network.get('received') or {}
    total = sent.get('totalBytes') or 0
    lines.append(f'- 服务器发出 {_size(total)}（{_rate(total, seconds)}，约 {_mbps(total, seconds):.1f} Mbps，'
                 f'{_count(sent.get("count"))}个包）；收到 {_size(received.get("totalBytes"))}'
                 f'（{_rate(received.get("totalBytes") or 0, seconds)}）')
    rows = sent.get('topContributors') or []
    if rows and total:
        lines.append('- 发出最多的数据包：' + '；'.join(
            f'{packet_label(row.get("packetId"), row.get("protocolId"))} {_size(row.get("totalBytes"))}'
            f'（{100 * (row.get("totalBytes") or 0) / total:.0f}%，{_count(row.get("count"))}个）' for row in rows[:8]))
    notes = [PACKET_NOTES[path] for path in (str(row.get('packetId') or '').rpartition(':')[2] for row in rows[:3])
             if path in PACKET_NOTES]
    if notes:
        lines.append('  （' + '；'.join(notes) + '）')
    incoming = received.get('topContributors') or []
    if incoming:
        lines.append('- 玩家发来最多的：' + '、'.join(
            f'{packet_label(row.get("packetId"), row.get("protocolId"))} {_size(row.get("totalBytes"))}'
            for row in incoming[:3]))
    players = [row for row in data.get('players') or [] if row.get('player') and row.get('sent')]
    others = [row for row in data.get('players') or [] if not row.get('player') and row.get('sent')]
    player_total = sum(row['sent'] for row in data.get('players') or []) or 0
    if players and player_total:
        if detailed:
            lines.append('- 按玩家（管理员可见，服务器发给他的）：' + '；'.join(
                f'{row["player"]} {_size(row["sent"])}（{100 * row["sent"] / player_total:.0f}%）' for row in players[:10])
                + (f'；另有 {len(others)} 个连接没对应到玩家' if others else ''))
        elif len(players) > 1:
            lines.append(f'- {len(players)} 位玩家里收到数据最多的一位占 {100 * players[0]["sent"] / player_total:.0f}%')
    tick = report.get('serverTick') or {}
    if tick.get('averageMs') is not None:
        lines.append(f'- 采样期间每刻平均 {tick["averageMs"]:.1f} ms'
                     + (f'（P90 {tick["p90"]:.1f} ms）' if tick.get('p90') is not None else ''))
    if detailed and data.get('size'):
        debug = data.get('debug') or {}
        lines.append(f'- 采样文件：本次 {_size(data["size"])}；服务器 debug/ 现有 {debug.get("files", "?")} 个采样文件，'
                     f'共 {_size(debug.get("bytes"))}。机器人只有只读权限删不掉，需要定期手动清理。')
    verdicts = []
    shares = {str(row.get('packetId') or '').rpartition(':')[2]: 100 * (row.get('totalBytes') or 0) / total
              for row in rows} if total else {}
    if shares.get('block_entity_data', 0) >= 30:
        verdicts.append(f'大头是方块实体数据（{shares["block_entity_data"]:.0f}%）；'
                        '谁附近的机器最多可以看 server_world topic=machines')
    if shares.get('custom_payload', 0) >= 30:
        verdicts.append(f'模组自定义包占 {shares["custom_payload"]:.0f}%')
    chunk = shares.get('level_chunk_with_light', 0) + shares.get('light_update', 0)
    if chunk >= 30:
        verdicts.append(f'区块数据占 {chunk:.0f}%，多半有人在跑图或传送')
    mbps = _mbps(total, seconds)
    verdicts.append(f'总量约 {mbps:.1f} Mbps，' + ('不大，延迟多半不是带宽造成的' if mbps < 20 else '已经不小，上行带宽紧的话会挤占'))
    if (tick.get('averageMs') or 0) > 50:
        verdicts.append(f'每刻平均 {tick["averageMs"]:.0f} ms 超过 50 ms，服务器本身跟不上，这比流量更可能是“卡”的原因')
    lines.append('判断：' + '；'.join(verdicts) + '。')
    return '\n'.join(lines)


def packets_report(server, query, state_dir, seconds=None, *, runner=None):
    """Admins: a fresh sample (or the one from the last 5 minutes)."""
    prefix = server['prefix']
    try:
        data, state = sample_packets(server, query, state_dir, seconds, runner=runner)
    except Exception as exc:
        return f'{prefix} 数据包采样没完成（{type(exc).__name__}）。'
    if state == 'busy':
        return f'{prefix} 已经有一次数据包采样在进行，等它结束再查。'
    if state.startswith('failed'):
        return f'{prefix} 数据包采样没成功：{state[8:]}。'
    minutes = max(1, int((time.time() - data['at']) // 60)) if state == 'cached' else None
    return format_packets(prefix, data, detailed=True, cached_minutes=minutes)


def last_packets_report(server, state_dir):
    """Members: the latest sample's packet types, without player names; never starts one."""
    prefix = server['prefix']
    data = load_sample(state_dir)
    if not data or time.time() - (data.get('at') or 0) > LAST_SAMPLE_HOURS * 3600:
        return (f'{prefix} 最近 {LAST_SAMPLE_HOURS} 小时没有数据包采样（采样只有管理员能开）。'
                '各玩家实时流量可以用 topic=players 查。')
    return format_packets(prefix, data, detailed=False)
