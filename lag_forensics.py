"""Bounded native Windows lag capture for the enabled Legacy server.

Adapted operational workflow: i0czf/minecraft-server-ops-kit (see UPSTREAM-NOTICE).
No server restart, heap dump, configuration mutation or profiler stop/cancel.
"""
from __future__ import annotations
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return default


def parse_ticks(text):
    """Prefer Overall; never interpret dimension MSPT as TPS."""
    rows = []
    for line in re.sub(r'§.', '', str(text)).splitlines():
        match = re.search(r'([\d.]+)\s*TPS\s*\(([\d.]+)\s*ms/tick\)', line, re.I)
        if match:
            tps, mspt = map(float, match.groups())
        else:
            match = re.search(r'Mean tick time:\s*([\d.]+).*?Mean TPS:\s*([\d.]+)', line, re.I)
            if match:
                mspt, tps = map(float, match.groups())
            else:
                a = re.search(r'\bTPS\s*[:=]\s*([\d.]+)', line, re.I)
                b = re.search(r'\bMSPT\s*[:=]\s*([\d.]+)', line, re.I)
                if not a or not b:
                    continue
                tps, mspt = float(a[1]), float(b[1])
        if not (math.isfinite(tps) and math.isfinite(mspt) and 0 <= tps <= 20.1 and mspt >= 0):
            continue
        row = dict(tps=min(20.0, tps), mspt=mspt)
        if re.search(r'Overall|总体|所有维度', line, re.I):
            return row
        rows.append(row)
    if len(rows) == 1:
        return rows[0]
    return dict(tps=None, mspt=None)  # Multiple dimensions without total are ambiguous.


def tail(path, maximum=8*1024*1024):
    try:
        with Path(path).open('rb') as stream:
            size = os.fstat(stream.fileno()).st_size
            start = max(0, size-maximum)
            stream.seek(start)
            raw = stream.read(maximum)
        if start:
            raw = raw.partition(b'\n')[2]
        return raw.decode('utf-8-sig', 'replace'), bool(start)
    except OSError:
        return '', False


def log_window(text, now, seconds=1200):
    current = datetime.fromtimestamp(now)
    selected = []
    for line in text.splitlines():
        head = re.match(r'\[([^\]]+)\]', line)
        if not head:
            continue
        match = re.search(r'(\d{2}):(\d{2}):(\d{2})(?:[.,](\d{3}))?$',head[1])
        if not match:
            continue
        try:
            stamp = current.replace(hour=int(match[1]), minute=int(match[2]), second=int(match[3]),
                                    microsecond=int(match[4] or 0)*1000)
            date = head[1][:match.start()].strip()
            cn = re.fullmatch(r'(\d{2})(\d{1,2})月(\d{4})',date)
            iso = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})[ T]?',date)
            en = re.fullmatch(r'(\d{1,2})(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(\d{4})',date,re.I)
            if cn:
                stamp=stamp.replace(year=int(cn[3]),month=int(cn[2]),day=int(cn[1]))
            elif iso:
                stamp=stamp.replace(year=int(iso[1]),month=int(iso[2]),day=int(iso[3]))
            elif en:
                month=['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'].index(en[2].lower())+1
                stamp=stamp.replace(year=int(en[3]),month=month,day=int(en[1]))
            elif date:
                continue
            elif stamp > current:
                stamp -= timedelta(days=1)
        except ValueError:
            continue
        if 0 <= now-stamp.timestamp() <= seconds:
            selected.append(line)
    return selected


def redact(text):
    text = re.sub(r'(?i)((?:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*)\S+', r'\1<redacted>', str(text))
    return re.sub(r'\[CQ:', '［CQ:', text, flags=re.I)


class LogCursor:
    def __init__(self, path):
        self.path = Path(path)
        with self.path.open('rb') as f:
            st = os.fstat(f.fileno())
            self.identity, self.offset = st.st_ino, st.st_size
            self.head = f.read(min(128, self.offset))
        self.partial = b''

    def read(self):
        with self.path.open('rb') as f:
            st = os.fstat(f.fileno())
            if st.st_ino != self.identity or st.st_size < self.offset or f.read(len(self.head)) != self.head:
                raise ValueError('log rotated during capture')
            if st.st_size-self.offset > 2*1024*1024:
                raise ValueError('log output exceeds capture bound')
            f.seek(self.offset)
            raw = self.partial + f.read(st.st_size-self.offset)
            self.offset = st.st_size
        last = raw.rfind(b'\n')
        if last < 0:
            self.partial = raw
            return ''
        self.partial = raw[last+1:]
        return raw[:last+1].decode('utf-8', 'replace')


def spark_log_lines(text):
    # Match the actual log header, never Spark-looking text inside player chat.
    # NeoForge sends profiler broadcasts under MinecraftServer on a Spark worker.
    header = (r'^\[[^\]\r\n]+\]\s+\['
              r'(?:spark-worker[^\]]*\]\s+\[(?:spark/|net\.minecraft\.server\.MinecraftServer/)[^\]]*'
              r'|[^\]]+\]\s+\[spark/)\]:')
    return '\n'.join(line for line in text.splitlines()
                     if re.match(header, line, re.I) or re.match(r'^\[spark/\]:', line))


def new_spark_url(text):
    found = re.findall(r'https://spark\.lucko\.me/[A-Za-z0-9]+\b', spark_log_lines(text))
    return found[-1] if found else None


class NativeProbe:
    def __init__(self, server, seconds=100):
        self.server = server
        self.deadline = time.monotonic()+seconds

    def run(self, argv, maximum=12):
        left = min(maximum, self.deadline-time.monotonic())
        if left <= 0:
            raise TimeoutError('capture deadline')
        # communicate(timeout) drains both pipes together; run kills and reaps
        # this exact child on timeout. Use the base Python to avoid venv launchers.
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=left, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            env={**os.environ, 'PYTHONPATH':os.pathsep.join(sys.path), 'PYTHONUTF8':'1'})
        if result.returncode:
            raise RuntimeError('probe failed')
        return result.stdout.decode('utf-8-sig', 'replace')[:4*1024*1024]

    def query(self, action):
        return self.run([getattr(sys, '_base_executable', sys.executable), '-X', 'utf8',
                         str(Path(__file__).resolve()), '--query', action])

    def process(self):
        props = (Path(self.server['server_dir'])/'server.properties').read_text(encoding='utf-8-sig')
        match = re.search(r'(?m)^server-port=(\d+)\s*$', props)
        if not match:
            raise ValueError('unknown game port')
        game, rcon = int(match[1]), int(self.server['port'])
        if not (0 < game < 65536 and 0 < rcon < 65536):
            raise ValueError('invalid ports')
        script = '''$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.Encoding]::UTF8;
$a=@(Get-NetTCPConnection -State Listen -LocalPort GAME | Select-Object -ExpandProperty OwningProcess -Unique);
$b=@(Get-NetTCPConnection -State Listen -LocalPort RCON | Select-Object -ExpandProperty OwningProcess -Unique);
if($a.Count -ne 1 -or $b.Count -ne 1 -or $a[0] -ne $b[0]){throw 'identity'};
$p=Get-CimInstance Win32_Process -Filter ('ProcessId='+$a[0]);
if($p.Name -notin @('java.exe','javaw.exe') -or -not $p.ExecutablePath){throw 'identity'};
$q=Get-Process -Id $a[0]; $cpu=$q.TotalProcessorTime.TotalSeconds; $watch=[Diagnostics.Stopwatch]::StartNew();
Start-Sleep -Milliseconds 400; $q.Refresh(); $watch.Stop();
@{pid=$a[0];created=$p.CreationDate.ToUniversalTime().ToString('o');exe=$p.ExecutablePath;
cpu=[math]::Round(($q.TotalProcessorTime.TotalSeconds-$cpu)/$watch.Elapsed.TotalSeconds/[Environment]::ProcessorCount*100,1);
memory_mb=[math]::Round($q.WorkingSet64/1MB,1)} | ConvertTo-Json -Compress'''.replace('GAME', str(game)).replace('RCON', str(rcon))
        return json.loads(self.run(['powershell.exe','-NoProfile','-Command',script], 10))

    def thread_dump(self, identity):
        fresh = self.process()
        if (fresh['pid'],fresh['created'],fresh['exe']) != (identity['pid'],identity['created'],identity['exe']):
            raise ValueError('server process changed')
        jcmd = Path(identity['exe']).with_name('jcmd.exe')
        if not jcmd.is_file():
            raise FileNotFoundError('matching JDK jcmd unavailable')
        output = self.run([str(jcmd),str(identity['pid']),'Thread.print'], 15)
        if 'Full thread dump' not in output or 'Server thread' not in output:
            raise ValueError('not a server thread dump')
        return output

    def spark(self, log):
        cursor = LogCursor(log)
        info = self.query('spark-info')
        time.sleep(0.3)
        info += spark_log_lines(cursor.read())
        if re.search(r'Unknown or incomplete command|Unknown command', info, re.I):
            return dict(status='unavailable', url=None)
        idle = re.search(r'not (?:currently )?(?:running|active)|no active profiler|isn.t.*(?:running|active)', info, re.I)
        # Spark runs commands asynchronously: RCON may return an empty packet.
        # Opening an existing sampler is safe even when its status reply is lost.
        # Start a timed sampler only after an explicit idle reply.
        action = 'spark-start' if idle else 'spark-open'
        cursor = LogCursor(log)
        response = self.query(action)
        # Direct command output is trusted too; normalize it to the logger format.
        notes = '\n'.join('[spark/]: '+line for line in response.splitlines())+'\n'
        limit = min(self.deadline-2, time.monotonic()+60)
        while time.monotonic() < limit:
            time.sleep(1)
            notes += spark_log_lines(cursor.read())+'\n'
            if len(notes) > 2*1024*1024:
                raise ValueError('spark log bound')
            if re.search(r'Unknown or incomplete command|Unknown command', notes, re.I):
                return dict(status='unavailable', url=None)
            if action == 'spark-start' and re.search(r'already (?:running|active)', notes, re.I):
                return dict(status='busy_or_unknown', url=None)
            if action == 'spark-open' and re.search(r"isn.t running|not (?:currently )?running", notes, re.I):
                return dict(status='inactive', url=None)
            if re.search(r'Error whilst (?:opening live profiler|uploading profiler results)', notes, re.I):
                return dict(status='upload_failed', url=None, notes=redact(notes[-8000:]))
            url = new_spark_url(notes)
            marker = (r'Profiler live viewer:' if action == 'spark-open' else
                      r'Profiler is now running|Starting a new profiler')
            if url and re.search(marker, notes, re.I):
                live = action == 'spark-open'
                return dict(status='captured_live' if live else 'captured', url=url,
                            temporary=live, source='existing_profiler' if live else 'timed_30s',
                            notes=redact(notes[-8000:]))
        return dict(status='no_new_report', url=None, action=action, notes=redact(notes[-8000:]))

    def archive_spark(self, result, directory):
        from spark_archive import saved_profile
        try:
            output = self.run([getattr(sys,'_base_executable',sys.executable),'-B','-X','utf8',
                str(Path(__file__).with_name('spark_archive.py')),result['url'],str(directory)],35)
            archived = json.loads(output)
        except Exception as exc:
            try:
                archived = saved_profile(directory)
            except Exception:
                archived = dict(status='archive_failed',url=None)
            archived['archive_error'] = type(exc).__name__
        archived['source'] = result.get('source')
        archived['source_url'] = result.get('url')
        return archived


def capture(telemetry, out, trigger, reason, probe):
    now = time.time()
    runtime = dict(timestamp=now, tps=None, mspt=None, players=None, max_players=None)
    errors = []
    from server_registry import parse_player_list
    try:
        runtime.update(parse_ticks(probe.query('ticks')))
        players = parse_player_list(probe.query('list'))
        if players:
            runtime.update(players=players['current'], max_players=players['max'])
    except Exception as exc:
        errors.append('runtime:'+type(exc).__name__)
    logfile = telemetry.server_dir/'logs/latest.log'
    if not logfile.is_file():
        errors.append('log:unavailable')
    raw, truncated = tail(logfile)
    window = log_window(raw, now)
    keep = [x for x in window if re.search(r"Can'?t keep up|Running.*ms behind", x, re.I)]
    error = [x for x in window if re.search(r'/(?:ERROR|FATAL)\]|\[(?:ERROR|FATAL)\]', x)]
    gc = [x for x in window if re.search(r'included GC lasting|GC pause', x, re.I)]
    meta = dict(schema=2, server=telemetry.server_id, prefix=telemetry.prefix, timestamp=now,
        reason=reason, trigger=trigger, runtime=runtime, out_dir=str(out), status='complete',
        counts=dict(keep_up=len(keep), errors=len(error), gc=len(gc)), log_window_seconds=1200,
        log_tail_truncated=truncated, log_matches=[redact(x)[:1000] for x in (keep+error+gc)[-80:]],
        errors=errors, thread_dump='unavailable', spark=dict(status='unavailable', url=None))
    try:
        identity = probe.process()
        meta['process'] = {k:v for k,v in identity.items() if k != 'exe'}
        dump = probe.thread_dump(identity)
        (out/'thread-dump.txt').write_text(redact(dump), encoding='utf-8')
        meta['thread_dump'] = 'captured'
    except Exception as exc:
        errors.append('thread:'+type(exc).__name__)
    try:
        meta['spark'] = probe.spark(telemetry.server_dir/'logs/latest.log')
        if meta['spark'].get('status') in ('captured','captured_live') and hasattr(probe,'archive_spark'):
            meta['spark'] = probe.archive_spark(meta['spark'],out)
    except Exception as exc:
        meta['spark'] = dict(status='failed', url=None)
        errors.append('spark:'+type(exc).__name__)
    for source, target in [('perf-samples.jsonl','perf-tail.jsonl'),('error-fingerprints.jsonl','fingerprints-tail.jsonl')]:
        data, _ = tail(telemetry.root/source, 256*1024)
        (out/target).write_text(redact(data),encoding='utf-8')
    summary = format_capture(meta)
    atomic(out/'meta.json', meta)
    (out/'summary.txt').write_text(summary,encoding='utf-8')
    (out/'report.txt').write_text(summary+'\n\n'+'\n'.join(meta['log_matches'])+'\n\n'+'; '.join(errors),encoding='utf-8')
    atomic(telemetry.root/'lag-latest.json', meta)
    return meta


def format_capture(meta):
    if meta.get('status') in ('queued','busy','cooldown','failed','disabled'):
        return '%s 卡顿取证：%s%s' % (meta.get('prefix',''),meta.get('message',meta['status']),
            ('\n记录：'+meta['out_dir']) if meta.get('out_dir') else '')
    val = lambda x: '未知' if x is None else str(x)
    trigger, live = meta.get('trigger') or {}, meta.get('runtime') or {}
    lines = [meta.get('prefix','')+'【卡顿取证】', '原因：'+meta.get('reason','manual'),
        '触发：TPS %s · MSPT %s · 在线 %s' % tuple(val(trigger.get(k)) for k in ('tps','mspt','players')),
        '采集时：TPS %s · MSPT %s · 在线 %s' % tuple(val(live.get(k)) for k in ('tps','mspt','players'))]
    p=meta.get('process')
    if p:
        lines.append('进程：CPU %s%%（整机归一）· 内存 %s MB · PID %s' % (p.get('cpu'),p.get('memory_mb'),p.get('pid')))
    c=meta.get('counts') or {}
    lines.append("近20分钟 Can't keep up %s 条 · ERROR/FATAL %s 条 · 线程转储 %s" %
        (c.get('keep_up',0),c.get('errors',0),'有' if meta.get('thread_dump')=='captured' else '未取得'))
    if meta.get('log_tail_truncated'):
        lines.append('日志按有界尾部统计，以上数量可能不完整。')
    spark=meta.get('spark') or {}
    states={'unavailable':'命令不可用，请检查 Spark 加载状态',
            'busy_or_unknown':'采样状态变化，本次跳过', 'inactive':'当前没有运行中的采样',
            'no_new_report':'本次未取得新链接（状态回复或报告超时）',
            'upload_failed':'报告上传失败，详见包内错误记录',
            'saved_local':'已保存到完整包，上传或在线校验未完成',
            'archive_failed':'报告归档失败，详见包内错误记录',
            'failed':'采集失败，详见包内错误记录'}
    label = 'Spark 预览（链接会过期）：' if spark.get('status') == 'captured_live' else 'Spark 报告：'
    lines.append(label+(spark.get('url') or states.get(spark.get('status'),'未取得')))
    lines.append('完整包：'+meta.get('out_dir','未知'))
    return '\n'.join(lines)


class Coordinator:
    def __init__(self, telemetry):
        self.t = telemetry
        self.lock = threading.RLock()
        self.worker = None
        self.last_sample = 0
        self.streak = 0
        self.recovered = False

    def observe(self, sample):
        now = float(sample.get('timestamp') or time.time())
        with self.lock:
            if now <= self.last_sample:
                return
            if now-self.last_sample > 95:
                self.streak = 0
            tps, mspt = sample.get('tps'),sample.get('mspt')
            bad = ((tps is not None and tps < 18) or (mspt is not None and mspt >= 50))
            self.streak = self.streak+1 if bad else 0
            self.last_sample = now
            ready = self.streak >= 2
        if ready:
            return self.request('TPS跌破阈值' if tps is not None and tps < 18 else '连续MSPT超阈',sample, automatic=True)

    def request(self, reason='管理员手动取证', trigger=None, probe=None, automatic=False):
        t = self.t
        reply = dict(schema=2,prefix=t.prefix)
        if t.server_id != 'legacy':
            return dict(reply,status='disabled',message='此采集器仅接入当前怀旧服')
        with self.lock:
            if self.worker and self.worker.is_alive():
                return dict(reply,status='busy',message='已有取证进行中，不重复启动')
            # A process-owned byte lock survives Python thread scheduling and is
            # released by Windows if the bridge exits unexpectedly.
            import msvcrt
            t.root.mkdir(parents=True,exist_ok=True)
            handle=(t.root/'lag-capture.lock').open('a+b')
            handle.seek(0,2)
            if handle.tell()==0:
                handle.write(b'0');handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:
                handle.close()
                return dict(reply,status='busy',message='已有其他进程在取证')
            try:
                state=read_json(t.root/'lag-watch.json',{})
                now=time.time()
                cooldown = 7200 if automatic else 900
                if now-float(state.get('last_started',0)) < cooldown:
                    handle.close()
                    return dict(reply,status='cooldown',message=('取证冷却中（%d分钟），可用 !卡顿取证 最新 查看上次报告' % (cooldown//60)))
                if shutil.disk_usage(t.root).free < 1024**3:
                    raise OSError('insufficient disk')
                out=t.root/'lag'/(datetime.fromtimestamp(now).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
                out.mkdir(parents=True)
                atomic(t.root/'lag-watch.json',dict(last_started=now,out_dir=str(out)))
                queued=dict(reply,status='queued',message='已开始后台取证，完成后发到玩家群',out_dir=str(out))
                atomic(out/'meta.json',queued)
                atomic(t.root/'lag-latest.json',queued)
                def run():
                    try:
                        if probe is None:
                            from server_registry import resolve_server
                            actual=NativeProbe(resolve_server('legacy'))
                        else:
                            actual=probe
                        result=capture(t,out,trigger or {},reason,actual)
                        t.record_event('lag_evidence',format_capture(result),reason=reason)
                    except Exception as exc:
                        result=dict(reply,status='failed',out_dir=str(out),message='采集未完成：'+type(exc).__name__)
                        atomic(out/'meta.json',result)
                        atomic(t.root/'lag-latest.json',result)
                    finally:
                        handle.close()
                    atomic(t.root/'lag-pending'/(out.name+'.json'),dict(summary=format_capture(result)))
                self.worker=threading.Thread(target=run,name='legacy-lag-capture',daemon=True)
                self.worker.start()
                return queued
            except Exception:
                handle.close()
                raise

    def deliver(self, send):
        if not self.recovered:
            self.recover_interrupted()
        pending=self.t.root/'lag-pending'
        for path in sorted(pending.glob('*.json'))[:1]:
            item=read_json(path,{})
            if time.time()-float(item.get('last_attempt',0)) < 30:
                continue
            item['last_attempt'] = time.time()
            atomic(path,item)
            if send(item.get('summary','')):
                done=self.t.root/'lag-delivered'
                done.mkdir(exist_ok=True)
                os.replace(path,done/path.name)

    def recover_interrupted(self):
        import msvcrt
        with self.lock:
            if self.worker and self.worker.is_alive():
                return
            self.t.root.mkdir(parents=True,exist_ok=True)
            with (self.t.root/'lag-capture.lock').open('a+b') as handle:
                handle.seek(0,2)
                if handle.tell()==0:
                    handle.write(b'0');handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                except OSError:
                    return
                meta=read_json(self.t.root/'lag-latest.json',{}) or {}
                if meta.get('schema') == 2 and meta.get('out_dir'):
                    out=Path(meta['out_dir']).resolve()
                    if out.is_relative_to((self.t.root/'lag').resolve()):
                        if meta.get('status') == 'queued':
                            meta.update(status='failed',message='上次取证进程已中断，未自动重复执行')
                            atomic(out/'meta.json',meta)
                            atomic(self.t.root/'lag-latest.json',meta)
                        pending=self.t.root/'lag-pending'/(out.name+'.json')
                        done=self.t.root/'lag-delivered'/(out.name+'.json')
                        if not pending.exists() and not done.exists():
                            atomic(pending,dict(summary=format_capture(meta)))
                self.recovered = True


_COORDINATORS = {}
_LOCK = threading.Lock()
def coordinator(telemetry):
    with _LOCK:
        key=str(telemetry.root.resolve())
        if key not in _COORDINATORS:
            _COORDINATORS[key]=Coordinator(telemetry)
        return _COORDINATORS[key]


if __name__ == '__main__':
    # Internal child only. Fixed read/capture commands; no caller-supplied RCON.
    commands={'list':'list','ticks':'neoforge tps','spark-info':'spark profiler info',
              'spark-open':'spark profiler open',
              'spark-start':'spark profiler start --timeout 30'}
    if len(sys.argv)!=3 or sys.argv[1]!='--query' or sys.argv[2] not in commands:
        raise SystemExit(2)
    from server_registry import resolve_server,query_server
    from request_runtime import Budget,CURRENT
    CURRENT.set(Budget(9))
    try:
        print(query_server(resolve_server('legacy'),commands[sys.argv[2]]))
    except Exception:
        raise SystemExit(1)
