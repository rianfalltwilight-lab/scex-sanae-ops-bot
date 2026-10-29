"""Read-only views of a remote POSIX server: player statistics, log tail, crash report.

Each call is one SSH command running a small Python script on the remote host.
Nothing on the remote host is written. Paths are fixed by this module; callers
cannot pass arbitrary remote paths.
"""
import json
import re
import subprocess
import time

from remote_mods_mirror import _ssh_command

_STATS_SCRIPT = r"""
import base64, json, os
cfg = json.loads(base64.b64decode('__CONFIG__'))
root = cfg['root']
names = {}
try:
    for row in json.load(open(os.path.join(root, 'usercache.json'), encoding='utf-8')):
        names[str(row.get('uuid', '')).lower()] = row.get('name')
except Exception:
    pass
DIST = ('walk_one_cm', 'sprint_one_cm', 'crouch_one_cm', 'swim_one_cm', 'walk_on_water_one_cm',
        'walk_under_water_one_cm', 'fly_one_cm', 'climb_one_cm', 'fall_one_cm', 'boat_one_cm',
        'horse_one_cm', 'minecart_one_cm', 'pig_one_cm', 'strider_one_cm', 'aviate_one_cm')
players = []
folder = os.path.join(root, 'world', 'stats')
for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
    if not name.endswith('.json'):
        continue
    path = os.path.join(folder, name)
    try:
        stats = (json.load(open(path, encoding='utf-8')).get('stats') or {})
    except Exception:
        continue
    uuid = name[:-5].lower()
    custom = stats.get('minecraft:custom') or {}
    get = lambda key: int(custom.get('minecraft:' + key) or 0)
    mined = stats.get('minecraft:mined') or {}
    killed = stats.get('minecraft:killed') or {}
    crafted = stats.get('minecraft:crafted') or {}
    players.append({
        'name': names.get(uuid) or uuid[:8],
        'play_hours': round(get('play_time') / 20 / 3600, 1),
        'deaths': get('deaths'), 'mob_kills': get('mob_kills'), 'player_kills': get('player_kills'),
        'mined': sum(int(v) for v in mined.values()), 'crafted': sum(int(v) for v in crafted.values()),
        'distance_km': round(sum(get(k) for k in DIST) / 100000, 1), 'jumps': get('jump'),
        'sessions': get('leave_game'),
        'top_mined': sorted(([k, int(v)] for k, v in mined.items()), key=lambda r: -r[1])[:3],
        'top_killed': sorted(([k, int(v)] for k, v in killed.items()), key=lambda r: -r[1])[:3],
        'last_seen': os.path.getmtime(path),
    })
print(json.dumps({'players': players}, ensure_ascii=False))
"""

_TAIL_SCRIPT = r"""
import base64, json, os
cfg = json.loads(base64.b64decode('__CONFIG__'))
path = os.path.join(cfg['root'], 'logs', 'latest.log')
with open(path, 'rb') as stream:
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(max(0, size - cfg['max_bytes']))
    data = stream.read()
lines = data.decode('utf-8', 'replace').splitlines()[-cfg['lines']:]
print(json.dumps({'lines': lines, 'size': size, 'mtime': os.path.getmtime(path)}, ensure_ascii=False))
"""

_CRASH_SCRIPT = r"""
import base64, json, os
cfg = json.loads(base64.b64decode('__CONFIG__'))
folder = os.path.join(cfg['root'], 'crash-reports')
paths = {}
if os.path.isdir(folder):
    paths.update({n: os.path.join(folder, n) for n in os.listdir(folder) if n.endswith('.txt')})
# JVM fatal errors (hs_err_pid*.log) land in the server root, not in crash-reports.
paths.update({n: os.path.join(cfg['root'], n) for n in os.listdir(cfg['root'])
              if n.startswith('hs_err_pid') and n.endswith('.log')})
names = sorted(paths, key=lambda n: os.path.getmtime(paths[n]))
kind = cfg.get('kind') or ''
pool = [n for n in names if (n.startswith('hs_err_pid') if kind == 'jvm' else n.endswith('.txt'))] if kind else names
wanted = cfg.get('name') or (pool[-1] if pool else '')
out = {'names': names[-10:], 'name': '', 'lines': []}
if wanted in names:
    with open(paths[wanted], encoding='utf-8', errors='replace') as stream:
        for _ in range(cfg['lines']):
            line = stream.readline()
            if not line:
                break
            out['lines'].append(line.rstrip('\n'))
    out['name'] = wanted
    out['mtime'] = os.path.getmtime(paths[wanted])
print(json.dumps(out, ensure_ascii=False))
"""

# Every force-loading ticket the world saved: vanilla /forceload ("Forced") and
# mod tickets ("ModForced", grouped by controller such as ae2:default). One file
# per dimension at world/**/data/chunks.dat, gzip NBT; parsed on the remote host.
_FORCED_SCRIPT = r"""
import base64, gzip, io, json, os, struct
cfg = json.loads(base64.b64decode('__CONFIG__'))
root = os.path.join(cfg['root'], 'world')

def parse(raw):
    stream = io.BytesIO(raw)
    def read(n):
        data = stream.read(n)
        if len(data) != n:
            raise EOFError
        return data
    def string():
        (length,) = struct.unpack('>H', read(2))
        return read(length).decode('utf-8', 'replace')
    def payload(kind):
        if kind == 1: return struct.unpack('>b', read(1))[0]
        if kind == 2: return struct.unpack('>h', read(2))[0]
        if kind == 3: return struct.unpack('>i', read(4))[0]
        if kind == 4: return struct.unpack('>q', read(8))[0]
        if kind == 5: return struct.unpack('>f', read(4))[0]
        if kind == 6: return struct.unpack('>d', read(8))[0]
        if kind == 7:
            (n,) = struct.unpack('>i', read(4)); read(n); return None
        if kind == 8: return string()
        if kind == 9:
            item = read(1)[0]; (n,) = struct.unpack('>i', read(4))
            return [payload(item) for _ in range(n)]
        if kind == 10:
            out = {}
            while True:
                tag = read(1)[0]
                if tag == 0:
                    return out
                name = string()          # the name comes before the value
                out[name] = payload(tag)
        if kind == 11:
            (n,) = struct.unpack('>i', read(4)); return list(struct.unpack('>%di' % n, read(4 * n)))
        if kind == 12:
            (n,) = struct.unpack('>i', read(4)); return list(struct.unpack('>%dq' % n, read(8 * n)))
        raise ValueError('nbt tag %d' % kind)
    tag = read(1)[0]
    string()
    return payload(tag)

def chunk(value):
    x, z = value & 0xffffffff, (value >> 32) & 0xffffffff
    return [x - (1 << 32) if x >= 1 << 31 else x, z - (1 << 32) if z >= 1 << 31 else z]

def dimension(rel):
    parts = rel.split(os.sep)
    if parts[0] == 'data': return 'minecraft:overworld'
    if parts[0] == 'DIM-1': return 'minecraft:the_nether'
    if parts[0] == 'DIM1': return 'minecraft:the_end'
    if parts[0] == 'dimensions' and len(parts) >= 5: return parts[1] + ':' + '/'.join(parts[2:-2])
    return None

dims, errors, newest = {}, [], 0
for base, subdirs, files in os.walk(root):
    if base[len(root):].count(os.sep) > 5:
        subdirs[:] = []
    subdirs[:] = [d for d in subdirs if d not in ('region', 'entities', 'poi', 'playerdata', 'stats',
                                                  'advancements', 'ftbquests', 'ftbteams', 'serverconfig')]
    if 'chunks.dat' not in files:
        continue
    path = os.path.join(base, 'chunks.dat')
    rel = os.path.relpath(path, root)
    dim = dimension(rel)
    if not dim:
        continue
    try:
        with open(path, 'rb') as stream:
            raw = stream.read(4 * 1024 * 1024)
        newest = max(newest, os.path.getmtime(path))
        data = (parse(gzip.decompress(raw) if raw[:2] == b'\x1f\x8b' else raw) or {}).get('data') or {}
    except Exception as exc:
        errors.append(rel + ': ' + type(exc).__name__)
        continue
    tickets = {}
    for group in data.get('ModForced') or []:
        controller = str(group.get('Controller') or 'unknown')
        entry = tickets.setdefault(controller, {'chunks': [], 'owners': [], 'entities': 0})
        for row in group.get('ModForced') or []:
            if isinstance(row.get('Chunk'), int):
                entry['chunks'].append(chunk(row['Chunk']))
            for key in ('Blocks', 'TickingBlocks'):
                for owner in row.get(key) or []:
                    if isinstance(owner, dict) and all(k in owner for k in 'XYZ'):
                        entry['owners'].append([owner['X'], owner['Y'], owner['Z']])
            for key in ('Entities', 'TickingEntities'):
                entry['entities'] += len(row.get(key) or [])
        entry['chunks'] = sorted({tuple(c) for c in entry['chunks']})
        entry['owners'] = sorted({tuple(o) for o in entry['owners']})
    vanilla = [chunk(v) for v in data.get('Forced') or [] if isinstance(v, int)]
    if vanilla or tickets:
        dims[dim] = {'vanilla': vanilla, 'tickets': tickets}
print(json.dumps({'dims': dims, 'errors': errors, 'saved_at': newest}))
"""

# Thaumcraft knowledge per player from world/playerdata/*.dat. Only research keys,
# discovered-aspect count and warp leave the host; inventories and positions do not.
# The port keeps these in the player's persistent data ("NeoForgeData"), mirrored by
# the thaumcraft:player_progress_data attachment.
_RESEARCH_SCRIPT = r"""
import base64, gzip, io, json, os, re, struct, time
cfg = json.loads(base64.b64decode('__CONFIG__'))
root = cfg['root']
KEEP = {'NeoForgeData', 'neoforge:attachments', 'thaumcraft:player_progress_data', 'ThaumcraftResearch',
        'ThaumcraftDiscoveredAspects', 'ThaumcraftWarp', 'ThaumcraftWarpTemporary', 'ThaumcraftWarpSticky'}

def parse(raw):
    stream = io.BytesIO(raw)
    read = stream.read
    def skip(kind):
        sizes = {1: 1, 2: 2, 3: 4, 4: 8, 5: 4, 6: 8}
        if kind in sizes: read(sizes[kind]); return
        if kind == 7: (n,) = struct.unpack('>i', read(4)); read(n); return
        if kind == 8: (n,) = struct.unpack('>H', read(2)); read(n); return
        if kind == 9:
            item = read(1)[0]; (n,) = struct.unpack('>i', read(4))
            for _ in range(n): skip(item)
            return
        if kind == 10:
            while True:
                tag = read(1)[0]
                if tag == 0: return
                (n,) = struct.unpack('>H', read(2)); read(n); skip(tag)
        if kind == 11: (n,) = struct.unpack('>i', read(4)); read(4 * n); return
        if kind == 12: (n,) = struct.unpack('>i', read(4)); read(8 * n); return
        raise ValueError(kind)
    def compound(keep_all):
        out = {}
        while True:
            tag = read(1)[0]
            if tag == 0: return out
            (n,) = struct.unpack('>H', read(2))
            name = read(n).decode('utf-8', 'replace')
            if tag == 10 and (keep_all or name in KEEP):
                out[name] = compound(keep_all or name in ('ThaumcraftResearch', 'ThaumcraftDiscoveredAspects'))
            elif tag in (1, 3) and (keep_all or name in KEEP):
                out[name] = struct.unpack('>b' if tag == 1 else '>i', read(1 if tag == 1 else 4))[0]
            else:
                skip(tag)
    tag = read(1)[0]
    (n,) = struct.unpack('>H', read(2)); read(n)
    return compound(False)

started = time.time()
names = {}
try:
    for row in json.load(open(os.path.join(root, 'usercache.json'), encoding='utf-8')):
        names[str(row.get('uuid', '')).lower()] = row.get('name')
except Exception:
    pass
players, errors = {}, 0
folder = os.path.join(root, 'world', 'playerdata')
UUID_DAT = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.dat', re.I)
for name in os.listdir(folder):
    # A save interrupted on 10-14 left "<uuid>-14321109007495561602.dat", a truncated temp copy
    # the game never reads; it was reported as an unreadable player file every 30 minutes.
    if not UUID_DAT.fullmatch(name):
        continue
    try:
        with open(os.path.join(folder, name), 'rb') as stream:
            data = parse(gzip.decompress(stream.read()))
    except Exception:
        errors += 1
        continue
    source = data.get('NeoForgeData') or {}
    if 'ThaumcraftResearch' not in source:
        source = ((data.get('neoforge:attachments') or {}).get('thaumcraft:player_progress_data')) or source
    research = source.get('ThaumcraftResearch') or {}
    done = sorted(k for k, v in research.items() if v and not k.startswith('@'))
    if not done:
        continue
    uuid = name[:-4].lower()
    players[uuid] = {
        'name': names.get(uuid) or uuid[:8], 'research': done, 'clues': sum(1 for k, v in research.items() if v and k.startswith('@')),
        'aspects': sum(1 for v in (source.get('ThaumcraftDiscoveredAspects') or {}).values() if v),
        'warp': [source.get('ThaumcraftWarp') or 0, source.get('ThaumcraftWarpSticky') or 0,
                 source.get('ThaumcraftWarpTemporary') or 0],
        'saved_at': os.path.getmtime(os.path.join(folder, name))}
print(json.dumps({'players': players, 'errors': errors, 'seconds': round(time.time() - started, 2)}))
"""

# Live host numbers for the server process: memory, CPU, uptime, JVM heap, disk, world size.
_HOST_SCRIPT = r"""
import base64, json, os, shutil, subprocess
cfg = json.loads(base64.b64decode('__CONFIG__'))
root = cfg['root']
out = {}
ps = subprocess.run(['ps', '-axo', 'pid=,rss=,%cpu=,etime=,command='], capture_output=True, text=True, timeout=10).stdout
for line in ps.splitlines():
    parts = line.split(None, 4)
    if len(parts) == 5 and '/bin/java' in parts[4] and 'neoforge' in parts[4]:
        pid, rss, cpu, etime, command = parts
        out['process'] = {'rss_kb': int(rss), 'cpu': float(cpu), 'etime': etime,
                          'xmx': next((p[4:] for p in command.split() if p.startswith('-Xmx')), '')}
        jcmd = command.split()[0].rsplit('/', 1)[0] + '/jcmd'
        if os.path.exists(jcmd):
            for name, option in (('heap', 'GC.heap_info'), ('uptime', 'VM.uptime')):
                try:
                    r = subprocess.run([jcmd, pid, option], capture_output=True, text=True, timeout=20)
                    out[name] = (r.stdout or '')[:1500]
                except Exception as exc:
                    out[name] = 'error ' + type(exc).__name__
        break
out['cores'] = os.cpu_count() or 0
try:
    out['memory'] = int(subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True, text=True,
                                       timeout=5).stdout.strip() or 0)
except Exception:
    out['memory'] = 0
usage = shutil.disk_usage(root)
out['disk'] = [usage.total, usage.used, usage.free]
sizes = {}
world = os.path.join(root, 'world')
for label, rel in (('region', 'region'), ('entities', 'entities'), ('poi', 'poi'), ('nether', 'DIM-1'),
                   ('end', 'DIM1'), ('dimensions', 'dimensions'), ('playerdata', 'playerdata')):
    total = 0
    for base, dirs, files in os.walk(os.path.join(world, rel)):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    sizes[label] = total
out['world'] = sizes
print(json.dumps(out))
"""

# MSLX panel login records. The panel also keeps each player's last IP; it is
# dropped on the host and never leaves it.
_LOGIN_SCRIPT = r"""
import base64, json, os
cfg = json.loads(base64.b64decode('__CONFIG__'))
path = os.path.join(cfg['root'], 'mslx_players.json')
rows = []
for row in ((json.load(open(path, encoding='utf-8')) or {}).get('players') or {}).values():
    if isinstance(row, dict) and row.get('name'):
        rows.append({'name': str(row.get('name')), 'uuid': str(row.get('uuid') or '').lower(),
                     'last_login': str(row.get('lastLoginTime') or ''), 'login_count': int(row.get('loginCount') or 0)})
print(json.dumps({'players': rows, 'saved_at': os.path.getmtime(path)}, ensure_ascii=False))
"""

# Vanilla advancement definitions (only those shown in the advancement screen) from the
# server jar NeoForge unpacked under libraries/; mods' definitions come from the local mirror.
_VANILLA_ADV_SCRIPT = r"""
import base64, glob, json, os, zipfile
cfg = json.loads(base64.b64decode('__CONFIG__'))
out = {}
for path in sorted(glob.glob(os.path.join(cfg['root'], 'libraries', 'net', 'minecraft', 'server', '*', '*.jar'))):
    try:
        archive = zipfile.ZipFile(path)
        names = archive.namelist()
    except Exception:
        continue
    files = [n for n in names if n.startswith('data/minecraft/advancement/') and n.endswith('.json')
             and '/recipes/' not in n]
    if not files:
        continue
    try:
        lang = json.loads(archive.read('assets/minecraft/lang/en_us.json'))
    except Exception:
        lang = {}
    for name in files:
        try:
            row = json.loads(archive.read(name))
        except Exception:
            continue
        display = row.get('display')
        if not isinstance(display, dict):
            continue
        title = display.get('title')
        key = title.get('translate') if isinstance(title, dict) else ''
        out['minecraft:' + name[len('data/minecraft/advancement/'):-5]] = {
            'key': key or '', 'en': lang.get(key, '') if key else str(title or ''),
            'frame': display.get('frame') or 'task', 'hidden': bool(display.get('hidden'))}
    break
print(json.dumps({'advancements': out}, ensure_ascii=False))
"""

# Shared NBT reader for the saved-data scripts below (gzip or raw, full parse).
_NBT_READER = r"""
import base64, glob, gzip, io, json, os, struct, time
from collections import Counter
cfg = json.loads(base64.b64decode('__CONFIG__'))
root = cfg['root']
world = os.path.join(root, 'world')
def parse(raw):
    s = io.BytesIO(raw)
    def r(n):
        d = s.read(n)
        if len(d) != n:
            raise EOFError
        return d
    def st():
        (n,) = struct.unpack('>H', r(2))
        return r(n).decode('utf-8', 'replace')
    def p(k):
        if k == 1: return struct.unpack('>b', r(1))[0]
        if k == 2: return struct.unpack('>h', r(2))[0]
        if k == 3: return struct.unpack('>i', r(4))[0]
        if k == 4: return struct.unpack('>q', r(8))[0]
        if k == 5: return struct.unpack('>f', r(4))[0]
        if k == 6: return struct.unpack('>d', r(8))[0]
        if k == 7:
            (n,) = struct.unpack('>i', r(4)); r(n); return None
        if k == 8: return st()
        if k == 9:
            t = r(1)[0]; (n,) = struct.unpack('>i', r(4))
            return [p(t) for _ in range(n)]
        if k == 10:
            o = {}
            while True:
                t = r(1)[0]
                if t == 0:
                    return o
                name = st()
                o[name] = p(t)
        if k == 11:
            (n,) = struct.unpack('>i', r(4)); return list(struct.unpack('>%di' % n, r(4 * n)))
        if k == 12:
            (n,) = struct.unpack('>i', r(4)); return list(struct.unpack('>%dq' % n, r(8 * n)))
        raise ValueError(k)
    t = r(1)[0]
    st()
    return p(t)
def load(path):
    with open(path, 'rb') as stream:
        raw = stream.read()
    if raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)
    return (parse(raw) or {}).get('data') or {}
def block_pos(v):
    # BlockPos.asLong: x 26 bits, z 26 bits, y 12 bits
    y = v & 0xFFF
    z = (v >> 12) & 0x3FFFFFF
    return [v >> 38, y - 4096 if y >= 2048 else y, z - (1 << 26) if z >= 1 << 25 else z]
"""

# SI futures market and exchange (world/data/mio_icif_*.dat). Personal parts stay on the
# host: per-player mailbox coins, futures positions, trade volume and evidence.
_MARKET_SCRIPT = _NBT_READER + r"""
out = {'errors': []}
try:
    path = os.path.join(world, 'data', 'mio_icif_future_market.dat')
    fm = load(path)
    out['saved_at'] = os.path.getmtime(path)
    out['day'] = fm.get('CurrentDay')
    out['prices'] = fm.get('CurrentPrices') or {}
    out['history'] = fm.get('PriceHistory') or {}
    out['events'] = {item: [{'type': e.get('Type'), 'start': e.get('StartDay'), 'duration': e.get('Duration'),
                             'intensity': round(float(e.get('Intensity') or 0), 3)} for e in rows]
                     for item, rows in (fm.get('ActiveEvents') or {}).items()}
except Exception as exc:
    out['errors'].append('future_market ' + type(exc).__name__)
try:
    ex = load(os.path.join(world, 'data', 'mio_icif_exchange.dat'))
    out['commissions'] = [{'item': c.get('Item'), 'quantity': c.get('Quantity'), 'delivered': c.get('Delivered'),
                           'reward': c.get('RewardTotal'), 'reward_item': c.get('RewardItem') or '',
                           'kind': c.get('Kind'), 'day': c.get('Day'), 'expiry': c.get('Expiry'),
                           'system': bool(c.get('System')), 'requester': c.get('Requester') or ''}
                          for c in ex.get('Commissions') or []]
    out['bars'] = {item: [{'day': b.get('Day'), 'open': b.get('O'), 'high': b.get('H'), 'low': b.get('L'),
                           'close': b.get('C'), 'volume': b.get('V')} for b in rows]
                   for item, rows in (ex.get('Bars') or {}).items()}
    out['ledger'] = [{'day': r.get('Day'), 'minted': r.get('Minted'), 'burned': r.get('Burned')}
                     for r in ex.get('Ledger') or []]
    out['treasury'] = ex.get('Treasury') or {}
    out['listings'] = len(ex.get('Listings') or [])
except Exception as exc:
    out['errors'].append('exchange ' + type(exc).__name__)
print(json.dumps(out, ensure_ascii=False))
"""

# Thaumcraft aura nodes per dimension, Outer Lands mazes and boss rooms, shock fields.
# Positions leave the host only for the rare node kinds and the maze portals (admin views).
_TC_WORLD_SCRIPT = _NBT_READER + r"""
started = time.time()
out = {'dims': {}, 'errors': []}
RARE = ('TAINTED', 'HUNGRY')
def dim_of(rel):
    parts = rel.split(os.sep)
    if parts[0] == 'data': return 'minecraft:overworld'
    if parts[0] == 'DIM-1': return 'minecraft:the_nether'
    if parts[0] == 'DIM1': return 'minecraft:the_end'
    if parts[0] == 'dimensions' and len(parts) >= 5: return parts[1] + ':' + '/'.join(parts[2:-2])
    return rel
for path in sorted(glob.glob(os.path.join(world, '**', 'thaumcraft_aura_nodes.dat'), recursive=True)):
    rel = os.path.relpath(path, world)
    try:
        nodes = load(path).get('Nodes') or []
    except Exception as exc:
        out['errors'].append(rel + ' ' + type(exc).__name__)
        continue
    aspects, holders, rare = Counter(), Counter(), []
    for node in nodes:
        for row in node.get('Aspects') or []:
            aspects[row.get('key')] += int(row.get('amount') or 0)
            holders[row.get('key')] += 1
        if node.get('TypeName') in RARE or node.get('Energized'):
            if len(rare) < 800 and isinstance(node.get('Pos'), int):
                top = sorted(node.get('Aspects') or [], key=lambda a: -int(a.get('amount') or 0))[:3]
                rare.append({'pos': block_pos(node['Pos']), 'type': node.get('TypeName'),
                             'energized': bool(node.get('Energized')), 'modifier': node.get('Modifier'),
                             'aspects': [[a.get('key'), int(a.get('amount') or 0)] for a in top]})
    out['dims'][dim_of(rel)] = {
        'count': len(nodes), 'types': dict(Counter(n.get('TypeName') for n in nodes)),
        'modifiers': {str(k): v for k, v in Counter(n.get('Modifier') for n in nodes).items()},
        'energized': sum(1 for n in nodes if n.get('Energized')), 'aspects': dict(aspects),
        'holders': dict(holders), 'rare': rare, 'saved_at': os.path.getmtime(path)}
outer = os.path.join(world, 'dimensions', 'thaumcraft', 'outer_lands', 'data')
try:
    data = load(os.path.join(outer, 'thaumcraft_outer_lands_mazes.dat'))
    mazes = data.get('Mazes') or []
    out['mazes'] = {'count': len(mazes), 'states': dict(Counter(m.get('GenerationState') for m in mazes)),
                    'sizes': dict(Counter('%sx%s' % (m.get('Width'), m.get('Height')) for m in mazes)),
                    'generated_cells': sum(len(m.get('GeneratedCells') or []) for m in mazes),
                    'sources': dict(Counter(m.get('SourceDimension') for m in mazes)),
                    'portals': [{'dim': m.get('SourceDimension'), 'pos': block_pos(m['SourcePortal']),
                                 'state': m.get('GenerationState')}
                                for m in mazes[:400] if isinstance(m.get('SourcePortal'), int)]}
except Exception as exc:
    out['errors'].append('mazes ' + type(exc).__name__)
try:
    data = load(os.path.join(outer, 'thaumcraft_outer_lands_bosses.dat'))
    out['bosses'] = {'count': data.get('bossCount'),
                     'rooms': [{'type': r.get('BossType'), 'state': r.get('State'), 'complete': bool(r.get('Complete')),
                                'spawned': len([v for v in str(r.get('SpawnedRoles') or '').split('\n') if v]),
                                'defeated': len([v for v in str(r.get('DefeatedRoles') or '').split('\n') if v])}
                               for r in data.get('Rooms') or []]}
except Exception as exc:
    out['errors'].append('bosses ' + type(exc).__name__)
shock = {}
for path in glob.glob(os.path.join(world, '**', 'thaumcraft_shock_fields.dat'), recursive=True):
    try:
        shock[dim_of(os.path.relpath(path, world))] = len(load(path).get('Positions') or [])
    except Exception as exc:
        out['errors'].append('shock ' + type(exc).__name__)
out['shock_fields'] = shock
out['seconds'] = round(time.time() - started, 2)
print(json.dumps(out, ensure_ascii=False))
"""


_SECRET = re.compile(r'(?i)\b(password|passwd|token|secret|api[_-]?key)\b\s*[:=]\s*[^\s,;]+')
_IP = re.compile(r'\b(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}\b')


def _redact(line):
    # Players' IPs appear in join lines; keep only the first three octets.
    return _IP.sub(r'\1.*', _SECRET.sub(r'\1=<redacted>', str(line)))


def _run(server, script, config, runner=None, timeout=40):
    argv = _ssh_command(server, script, dict(config, root=str(server['remote_server_dir'])))
    result = (runner or subprocess.run)(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise OSError('SSH exit %s' % result.returncode)
    return json.loads(result.stdout.decode('utf-8-sig'))


def player_stats(server, *, runner=None):
    """{'generated_at', 'players': [...]} summarised on the remote host."""
    data = _run(server, _STATS_SCRIPT, {}, runner=runner, timeout=60)
    return {'generated_at': time.time(), 'players': data.get('players') or []}


def tail_log(server, lines=150, *, runner=None):
    lines = max(1, min(int(lines or 150), 400))
    data = _run(server, _TAIL_SCRIPT, {'lines': lines, 'max_bytes': 1024 * 1024}, runner=runner)
    return [_redact(line)[:500] for line in data.get('lines') or []]


def crash_report(server, name='', lines=160, *, runner=None, kind=''):
    name = str(name or '')
    if name and not (re.fullmatch(r'[A-Za-z0-9_.-]{1,120}\.txt', name) or re.fullmatch(r'hs_err_pid\d{1,10}\.log', name)):
        raise ValueError('崩溃报告文件名无效')
    kind = kind if kind in ('crash', 'jvm') else ''
    data = _run(server, _CRASH_SCRIPT, {'name': name, 'kind': kind, 'lines': max(20, min(int(lines or 160), 400))},
                runner=runner)
    return {'name': data.get('name') or '', 'names': data.get('names') or [],
            'mtime': data.get('mtime'),
            'lines': [_redact(line)[:500] for line in data.get('lines') or []]}


def forced_chunks(server, *, runner=None):
    """{'generated_at', 'saved_at', 'dims': {dim: {'vanilla', 'tickets'}}, 'errors'} from world chunks.dat files."""
    data = _run(server, _FORCED_SCRIPT, {}, runner=runner, timeout=60)
    return {'generated_at': time.time(), 'saved_at': data.get('saved_at') or None,
            'dims': data.get('dims') or {}, 'errors': data.get('errors') or []}


def thaumcraft_progress(server, *, runner=None):
    """{'generated_at', 'players': {uuid: {name, research, clues, aspects, warp, saved_at}}, 'errors'}.

    warp is [permanent, sticky, temporary]; clues are '@key' hints for research not yet done.
    """
    data = _run(server, _RESEARCH_SCRIPT, {}, runner=runner, timeout=90)
    return {'generated_at': time.time(), 'players': data.get('players') or {},
            'errors': int(data.get('errors') or 0), 'seconds': data.get('seconds')}


def host_status(server, *, runner=None):
    """Live process memory/CPU/uptime, JVM heap text, cores, RAM, disk and world sizes of the remote host.

    The JVM is asked only GC.heap_info and VM.uptime: neither pauses the server.
    """
    data = _run(server, _HOST_SCRIPT, {}, runner=runner, timeout=60)
    data['collected_at'] = time.time()
    return data


def login_records(server, *, runner=None):
    """{'generated_at', 'saved_at', 'players': [{name, uuid, last_login, login_count}]} from the MSLX panel; no IPs."""
    data = _run(server, _LOGIN_SCRIPT, {}, runner=runner, timeout=40)
    return {'generated_at': time.time(), 'saved_at': data.get('saved_at'), 'players': data.get('players') or []}


def vanilla_advancements(server, *, runner=None):
    """{id: {key, en, frame, hidden}} for vanilla advancements that have a display."""
    return _run(server, _VANILLA_ADV_SCRIPT, {}, runner=runner, timeout=60).get('advancements') or {}


def market_snapshot(server, *, runner=None):
    """Prices, 20-day history, events, exchange commissions/bars/ledger as last saved; nothing per player."""
    data = _run(server, _MARKET_SCRIPT, {}, runner=runner, timeout=60)
    data['generated_at'] = time.time()
    return data


def thaumcraft_world(server, *, runner=None):
    """Aura nodes per dimension, Outer Lands mazes/boss rooms and shock fields as last saved."""
    data = _run(server, _TC_WORLD_SCRIPT, {}, runner=runner, timeout=90)
    data['generated_at'] = time.time()
    return data
