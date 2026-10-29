"""The game server's network, measured where the server runs (round 24, 10-27).

`!ping` measures the bot host. The game server is another machine on the same LAN and its
numbers differ (10-27: China Mobile 64 ms from the bot host, 12.6 ms from the game host), and
the bot host's DNS answers through its proxy, so public checks from here are not trustworthy.

One read-only SSH call runs a fixed script on the game host:
  ping           the four ISP reference nodes of network_probe and the default gateway (4 echoes each)
  lsof           established TCP connections on the game port, by kind (public / LAN / loopback);
                 remote addresses are reduced to /24 (first three parts) on the host
  egress         the host's public address (speed.cloudflare.com/cdn-cgi/trace)
  DNS            the public A record of the server address (dns.alidns.com DoH), for the DDNS check
  hairpin        a TCP connect from the host to <egress>:<public port> (port forward check; some
                 routers do not support NAT loopback, so "no" is not proof of a fault)
  log            "Timed out" disconnects in logs/latest.log and whether the server was running
                 behind ("Can't keep up", RTS TICK_GAP SEVERE) in the 45 s before each one, plus
                 whether the player had come in over the LAN (round 27)
TCP retransmissions per player connection come from server_traffic's nettop run (same SSH host,
in parallel). On 10-27 public players lost 3.5-6.6 % of what the server sent while LAN players
lost 0.4-0.6 % and plain pings lost nothing, and every timeout followed a stall of 2-7 s.
From the bot host: a Minecraft status ping to the server's LAN address. Nothing is written
or changed anywhere, and no upload test runs on the game host (it would take the players' uplink).
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import minecraft_status
import remote_server_reader
import server_traffic
from network_probe import BACKUP_NODES, REFERENCE_NODES

CACHE_SECONDS = 60
STALL_WINDOW = 45          # seconds before a timeout in which a stall counts
STALL_MS = 2000            # vanilla only warns "Can't keep up" from 2 s behind
RETX_HIGH = 2.0            # % of sent bytes retransmitted since login
RETX_MIN_BYTES = 512 * 1024
TZ = timezone(timedelta(hours=8))
_CACHE = {}
_LOCK = threading.Lock()

_SCRIPT = r"""
import base64, json, re, socket, subprocess, threading
cfg = json.loads(base64.b64decode('__CONFIG__'))
out = {'ping': {}, 'errors': []}

def run(args, timeout):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.stdout
    except Exception as exc:
        out['errors'].append(args[0].rsplit('/', 1)[-1] + ':' + type(exc).__name__)
        return ''

def ping(key, address):
    text = run(['/sbin/ping', '-c', '12', '-i', '0.25', '-W', '2000', '-t', '10', address], 15)
    loss = re.search(r'([\d.]+)% packet loss', text)
    avg = re.search(r'= [\d.]+/([\d.]+)/', text)
    out['ping'][key] = {'loss': float(loss.group(1)) if loss else None, 'avg': float(avg.group(1)) if avg else None}

def egress():
    text = run(['/usr/bin/curl', '-sS', '-m', '8', 'https://speed.cloudflare.com/cdn-cgi/trace'], 12)
    out['egress'] = next((l[3:].strip() for l in text.splitlines() if l.startswith('ip=')), '')

def dns():
    text = run(['/usr/bin/curl', '-sS', '-m', '6', 'https://dns.alidns.com/resolve?name=' + cfg['public_host'] + '&type=1'], 10)
    try:
        answers = json.loads(text).get('Answer') or []
    except Exception:
        answers = []
    out['dns'] = [a.get('data') for a in answers if a.get('type') == 1 and a.get('data')]

gateway = ''
for line in run(['/usr/sbin/netstat', '-rn', '-f', 'inet'], 10).splitlines():
    parts = line.split()
    if len(parts) > 1 and parts[0] == 'default':
        gateway = parts[1]
        break
jobs = [threading.Thread(target=ping, args=(address, address)) for address in cfg['nodes']]
if re.fullmatch(r'\d+\.\d+\.\d+\.\d+', gateway):
    jobs.append(threading.Thread(target=ping, args=('gateway', gateway)))
jobs += [threading.Thread(target=egress), threading.Thread(target=dns)]
for job in jobs:
    job.start()
conns = {'public': 0, 'lan': 0, 'loopback': 0}
prefixes = {}
for line in run(['/usr/sbin/lsof', '-nP', '-iTCP:%d' % cfg['port'], '-sTCP:ESTABLISHED'], 10).splitlines()[1:]:
    match = re.search(r'->\[?([0-9a-fA-F:.]+?)\]?:\d+ \(ESTABLISHED\)', line)
    if not match:
        continue
    address = match.group(1).replace('::ffff:', '')
    if address.startswith('127.') or address == '::1':
        kind = 'loopback'
    elif re.match(r'(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|fe80:|fd)', address):
        kind = 'lan'
    else:
        kind = 'public'
    conns[kind] += 1
    if kind == 'public':
        prefix = ('.'.join(address.split('.')[:3]) + '.x') if '.' in address else ':'.join(address.split(':')[:3]) + ':…'
        prefixes[prefix] = prefixes.get(prefix, 0) + 1
out['connections'] = conns
out['prefixes'] = sorted(prefixes.items(), key=lambda kv: -kv[1])[:10]
for job in jobs:
    job.join(20)
out['gateway'] = bool(gateway)

def lan(address):
    address = address.strip('[]').replace('::ffff:', '')
    return bool(re.match(r'(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|127\.|fe80:|fd)', address))

events, stalls, keepup, first, last_lan = [], [], [], None, {}
try:
    with open(cfg['root'] + '/logs/latest.log', encoding='utf-8', errors='replace') as stream:
        for line in stream:
            stamp = re.match(r'\[[^\]]*?(\d\d):(\d\d):(\d\d)\.\d+\]', line)
            if not stamp:
                continue
            second = int(stamp.group(1)) * 3600 + int(stamp.group(2)) * 60 + int(stamp.group(3))
            first = first or stamp.group(1) + ':' + stamp.group(2)
            if "Can't keep up" in line:
                behind = re.search(r'Running (\d+)ms', line)
                if behind:
                    keepup.append(int(behind.group(1)))
                    stalls.append((second, int(behind.group(1))))
            elif 'TICK_GAP' in line and 'SEVERE' in line:
                gap = re.search(r'gap_ms=(\d+)', line)
                if gap:
                    stalls.append((second, int(gap.group(1))))
            elif 'logged in with entity id' in line:
                join = re.search(r'\]: ([A-Za-z0-9_]{1,16})\[/([0-9a-fA-F:.\[\]%]+?):\d+\] logged in', line)
                if join:
                    last_lan[join.group(1)] = lan(join.group(2))
            elif 'lost connection: Timed out' in line:
                name = re.search(r'\]: ([A-Za-z0-9_]{1,16}) lost connection', line)
                if name:
                    events.append((second, name.group(1), last_lan.get(name.group(1))))
except OSError:
    out['errors'].append('latest.log')
timeouts = []
for second, name, on_lan in events:
    prior = [ms for at, ms in stalls if second - cfg.get('stall_window', 45) <= at <= second]
    timeouts.append({'time': '%02d:%02d' % (second // 3600, second // 60 % 60), 'player': name,
                     'lan': on_lan, 'stall_ms': max(prior) if prior else 0})
out['log'] = {'since': first, 'timeouts': timeouts, 'keepup': len(keepup), 'keepup_max_ms': max(keepup or [0])}
out['hairpin'] = None
if re.fullmatch(r'\d+\.\d+\.\d+\.\d+', out.get('egress') or ''):
    try:
        socket.create_connection((out['egress'], cfg['public_port']), 3).close()
        out['hairpin'] = True
    except Exception:
        out['hairpin'] = False
print(json.dumps(out))
"""


def _server_port(server):
    """server-port from the mirrored server.properties; 25565 when unknown."""
    try:
        text = (Path(server.get('server_dir') or '') / 'server.properties').read_text(encoding='utf-8', errors='replace')
        match = re.search(r'^server-port=(\d+)\s*$', text, re.M)
        return int(match.group(1)) if match else 25565
    except OSError:
        return 25565


def _public_address(server):
    host, _, port = str(server.get('address') or '').rpartition(':')
    if not host:
        host, port = str(server.get('address') or ''), '25565'
    return host, int(port) if port.isdigit() else 25565


def collect(server, *, runner=None, status=minecraft_status.query, now=None, players=None):
    """Measurements as a dict; cached for a minute so a burst of questions costs one run."""
    now = time.time() if now is None else now
    with _LOCK:
        hit = _CACHE.get(server['id'])
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    port = _server_port(server)
    public_host, public_port = _public_address(server)
    if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', public_host):
        public_host = ''
    data = {'port': port, 'public_host': public_host, 'public_port': public_port, 'at': now}
    tcp, worker = {}, None
    if server.get('remote_ssh_host'):
        fetch_players = players or (lambda: server_traffic.collect_players(server, runner=runner))

        def fetch():
            try:
                tcp['data'] = fetch_players()
            except Exception as exc:
                tcp['data'] = {'error': type(exc).__name__}
        worker = threading.Thread(target=fetch, name='sanae-net-tcp', daemon=True)
        worker.start()
    started = time.monotonic()
    try:
        state = status(server.get('host') or '', port, timeout=5)
        players = state.get('players') or {}
        data['status'] = {'ms': (time.monotonic() - started) * 1000, 'online': players.get('online'),
                          'max': players.get('max')}
    except Exception as exc:
        data['status'] = {'error': type(exc).__name__}
    if server.get('remote_ssh_host'):
        try:
            data['host'] = remote_server_reader._run(
                server, _SCRIPT, {'nodes': [address for _label, address in REFERENCE_NODES] + list(BACKUP_NODES.values()),
                                  'port': port,
                                  'public_host': public_host, 'public_port': public_port},
                runner=runner, timeout=45)
        except Exception as exc:
            data['host'] = {'error': type(exc).__name__}
    if worker:
        worker.join(60)
        data['tcp'] = tcp.get('data') or {}
    with _LOCK:
        _CACHE[server['id']] = (now, data)
    return data


def _carrier(pings, label, address):
    """The better of the reference node and its backup: path loss shows on both."""
    rows = [row for row in (pings.get(address), pings.get(BACKUP_NODES.get(label, ''))) if row]
    answered = [row for row in rows if row.get('avg') is not None]
    if not answered:
        return rows[0] if rows else None
    best = min(answered, key=lambda row: row.get('loss') or 0)
    primary = pings.get(address) or {}
    return dict(best, limited=bool((primary.get('loss') or 0) >= 10 and (best.get('loss') or 0) < 10))


def _ping_text(row):
    if not row or row.get('avg') is None:
        return '不通' if row and row.get('loss') == 100 else '没测到'
    text = f'{row["avg"]:.1f} ms'
    if row.get('loss'):
        text += f'（丢包 {row["loss"]:g}%）'
    return text


def format_report(prefix, data, detailed=False, online=None):
    lines = [f'{prefix} 服务器网络（在服务器主机上实测，{datetime.fromtimestamp(data.get("at") or time.time(), TZ):%H:%M}；'
             '一分钟内重复问用同一次结果）']
    status = data.get('status') or {}
    if 'ms' in status:
        lines.append(f'- 服务端：局域网状态查询 {status["ms"]:.0f} ms 回应，在线 {status.get("online")}/{status.get("max")}')
    else:
        lines.append(f'- 服务端：局域网状态查询没有回应（{status.get("error", "未知")}），服务器可能没开或卡住了')
    host = data.get('host') or {}
    verdicts = []
    if host.get('error'):
        lines.append(f'- 服务器主机连不上（{host["error"]}），外网部分这次测不了。')
        return '\n'.join(lines)
    pings = host.get('ping') or {}
    carriers = {label: _carrier(pings, label, address) for label, address in REFERENCE_NODES}
    lines.append('- 服务器主机到运营商：' + '，'.join(f'{label} {_ping_text(carriers[label])}' for label, _a in REFERENCE_NODES))
    # 12 echoes: one lost reply (8 %) alone is not called packet loss.
    lossy = [label for label, row in carriers.items() if ((row or {}).get('loss') or 0) >= 10]
    limited = [label for label, row in carriers.items() if (row or {}).get('limited')]
    if 'gateway' in pings:
        lines.append(f'- 局域网网关：{_ping_text(pings["gateway"])}')
        if ((pings['gateway'] or {}).get('loss') or 0) >= 10:
            verdicts.append('到网关就丢包，问题在局域网（网线、交换机或路由器）')
    if lossy:
        verdicts.append('、'.join(lossy) + '方向有丢包，这些运营商的玩家可能卡顿或掉线')
    conns = host.get('connections') or {}
    total = sum(conns.values())
    line = (f'- 游戏端口 {data.get("port")} 的连接：{total} 个（公网 {conns.get("public", 0)}、局域网 {conns.get("lan", 0)}、'
            f'本机 {conns.get("loopback", 0)}）')
    if online is not None:
        line += f'；在线名单 {online} 人'
        if total - online >= 3:
            verdicts.append(f'连接比在线人数多 {total - online} 个，有不在玩家名单里的连接（可能是服务器列表刷新、'
                            '正在登录的人，或者扫描）')
    lines.append(line)
    _tcp_lines(lines, verdicts, data.get('tcp') or {}, detailed, quiet_ping=not lossy)
    _timeout_lines(lines, verdicts, host.get('log') or {}, detailed)
    egress, dns = host.get('egress') or '', [d for d in host.get('dns') or [] if d]
    if egress and dns:
        same = egress in dns
        lines.append(f'- 公网入口 {data.get("public_host")}:{data.get("public_port")}：域名解析'
                     + ('和服务器出口地址一致' if same else '和服务器出口地址不一致') + '（DDNS）')
        if not same:
            verdicts.append('域名指向的地址和服务器现在的出口地址不一样：DDNS 没更新，或者入口走的是转发/隧道')
    elif not egress:
        lines.append('- 服务器出口地址没取到（外网可能不通）')
    if host.get('hairpin') is not None:
        lines.append('- 从服务器回连公网端口：' + ('通' if host['hairpin'] else '不通（路由器不支持回环时这项不准）'))
    if detailed:
        details = []
        if egress:
            details.append(f'出口 {egress}')
        if dns:
            details.append('解析 ' + '、'.join(dns))
        if host.get('prefixes'):
            details.append('公网连接来源：' + '、'.join(f'{p} ×{n}' for p, n in host['prefixes']))
        for label in limited:
            address = dict(REFERENCE_NODES)[label]
            details.append(f'{label}参考节点 {address} 丢包 {pings[address]["loss"]:g}%，备用节点 '
                           f'{BACKUP_NODES[label]} 正常，是节点自己限速 ping，不算线路丢包')
        if details:
            lines.append('- 管理员可见：' + '；'.join(details))
    lines.append('判断：' + ('；'.join(verdicts) if verdicts else '没看到明显问题') + '。')
    lines.append('说明：只测服务器这一端，玩家自己的网络测不到；TCP 重传是服务器发出的数据里需要重发的比例，'
                 '正常在 1% 以下；上传带宽测速用 !ping（在机器人主机上测，和服务器同一局域网）。')
    return '\n'.join(lines)


def _retx(rows):
    sent = sum(row.get('out_total') or 0 for row in rows)
    return 100 * sum(row.get('retx_total') or 0 for row in rows) / sent if sent else 0.0


def _tcp_lines(lines, verdicts, tcp, detailed, quiet_ping):
    """Retransmitted share of what the server sent each player since login (nettop)."""
    rows = [row for row in tcp.get('conns') or [] if (row.get('out_total') or 0) >= RETX_MIN_BYTES]
    public = [row for row in rows if row.get('kind') == 'public']
    lan = [row for row in rows if row.get('kind') == 'lan']
    if not public and not lan:
        return
    parts = []
    if public:
        parts.append(f'公网 {_retx(public):.1f}%（{len(public)} 人）')
    if lan:
        parts.append(f'局域网 {_retx(lan):.1f}%（{len(lan)} 人）')
    line = '- 玩家连接的 TCP 重传（本次登录以来）：' + '，'.join(parts)
    if detailed:
        ranked = sorted(public + lan, key=lambda row: -_retx([row]))
        line += '；按玩家（管理员可见）：' + '、'.join(
            f'{row.get("player") or "未对应"} {_retx([row]):.1f}%'
            + (f'/{row["rtt"]:.0f} ms' if row.get('rtt') is not None else '') for row in ranked[:10])
    else:
        high = sum(1 for row in public if _retx([row]) >= 3)
        if high:
            line += f'；公网里有 {high} 人超过 3%'
    lines.append(line)
    if public and _retx(public) >= RETX_HIGH:
        text = f'公网玩家的 TCP 重传 {_retx(public):.1f}%'
        if lan:
            text += f'（局域网只有 {_retx(lan):.1f}%）'
        text += '，公网路径上有丢包'
        if quiet_ping:
            text += ('；ping 运营商不丢包，多半是一次发大量数据（进服、跑图加载区块）时服务器上行出口丢包'
                     '（家宽限速或路由器缓冲），也可能在玩家那一端')
        verdicts.append(text)


def _timeout_lines(lines, verdicts, log, detailed):
    """'Timed out' disconnects and whether the server had stalled just before each."""
    timeouts = log.get('timeouts') or []
    since = log.get('since')
    head = f'- 超时掉线（本次日志{"，从 " + since + " 起" if since else ""}）：'
    if not timeouts:
        if since:
            lines.append(head + '没有')
        return
    stalled = [row for row in timeouts if (row.get('stall_ms') or 0) >= STALL_MS]
    lan = [row for row in timeouts if row.get('lan')]
    line = head + f'{len(timeouts)} 次'
    if stalled:
        line += (f'，其中 {len(stalled)} 次掉线前 {STALL_WINDOW} 秒内服务器落后 2 秒以上'
                 f'（最多 {max(row["stall_ms"] for row in stalled) / 1000:.1f} 秒）')
    if lan:
        line += f'；局域网玩家也掉了 {len(lan)} 次'
    if detailed:
        counts = {}
        for row in timeouts:
            counts[row['player']] = counts.get(row['player'], 0) + 1
        line += '；按玩家（管理员可见）：' + '、'.join(
            f'{name} {count} 次' for name, count in sorted(counts.items(), key=lambda kv: -kv[1]))
    lines.append(line)
    if log.get('keepup'):
        lines.append(f'- 服务器“跟不上”警告 {log["keepup"]} 次，最多落后 {log.get("keepup_max_ms", 0) / 1000:.1f} 秒')
    if len(stalled) * 2 >= len(timeouts):
        verdicts.append(f'{len(timeouts)} 次超时掉线里 {len(stalled)} 次发生在服务器卡住之后'
                        + ('，局域网玩家也会掉' if lan else '')
                        + '：掉线主要是服务器卡顿造成的，不是网络')


def report(server, prefix, privileged, online=None, **kwargs):
    return format_report(prefix, collect(server, **kwargs), detailed=privileged, online=online)
