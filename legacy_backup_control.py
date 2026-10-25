"""Manual Legacy backup adapter for an explicitly configured backup pipeline."""
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

from onebot_utf8 import json_bytes, onebot_headers, onebot_success_utf8

PYTHON = os.environ.get('SANAE_LEGACY_BACKUP_PYTHON', sys.executable)
PIPELINE = os.environ.get('SANAE_LEGACY_BACKUP_PIPELINE', '').strip()
HERE = Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def read_status():
    if not PIPELINE:
        return {'enabled': False, 'state': 'disabled'}
    result = subprocess.run([PYTHON, PIPELINE, '--status'], capture_output=True,
                            timeout=10, creationflags=NO_WINDOW)
    if result.returncode:
        raise RuntimeError('备份状态接口读取失败')
    return json.loads(result.stdout.decode('utf-8-sig'))


def format_status(data):
    state = data.get('state', 'unknown')
    labels = {'disabled': '尚未启用', 'busy': '正在备份', 'idle': '等待首次备份',
              'complete': '最近一次备份完成', 'failed': '最近一次备份失败',
              'interrupted': '上次备份中断'}
    lines = ['[怀旧][备份状态] ' + labels.get(state, str(state))]
    if data.get('updated_at'):
        lines.append('记录时间：' + str(data['updated_at']))
    last = data.get('last_run') or {}
    if last.get('archive'):
        lines.append('文件：' + Path(last['archive']).name)
    if last.get('bytes'):
        lines.append('大小：%.1f MiB，保存在配置的备份目录。' % (last['bytes'] / 1048576))
    if state in ('failed', 'interrupted') and data.get('message'):
        lines.append(str(data['message'])[:300])
    return '\n'.join(lines)


def status_text():
    try:
        return format_status(read_status())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        return '[怀旧][备份状态] 暂不可用：' + type(error).__name__


def start():
    try:
        status = read_status()
        if not status.get('enabled'):
            return '[怀旧][备份] 备份流水线尚未启用，本次未执行。'
        if status.get('state') == 'busy':
            return '[怀旧][备份] 已有备份正在进行，请用 !backup 状态查看进度。'
        directory = HERE / 'logs'
        directory.mkdir(exist_ok=True)
        with (directory / 'legacy-mac-backup-launch.log').open('ab') as log:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--run'],
                             cwd=str(HERE), stdout=log, stderr=subprocess.STDOUT,
                             creationflags=NO_WINDOW, close_fds=True)
        return ('[怀旧][备份] 已提交手动备份：保存世界后，将快照传到配置的备份目录压缩并校验。\n'
                '可用 !backup 状态查看进度；完成或失败后会回群通知。')
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        return '[怀旧][备份] 无法启动备份：' + type(error).__name__


def completion_text(returncode, data):
    state = data.get('state')
    if returncode == 0 and state == 'complete':
        return '[怀旧][备份完成] 已保存到配置的备份目录并通过完整性校验。大小 %.1f MiB。' % (data.get('bytes', 0) / 1048576)
    if state == 'busy':
        return '[怀旧][备份] 已有备份正在进行，本次没有重复创建。'
    if state == 'disabled':
        return '[怀旧][备份] 备份尚未启用，本次未执行。'
    reason = str(data.get('message') or data.get('error') or '请查看 !backup 状态')[:300]
    return '[怀旧][备份失败] ' + reason


def notify(text):
    groups = tuple(dict.fromkeys(int(value.strip()) for value in
        os.environ.get('LEGACY_MONITOR_GROUPS', os.environ.get('LLBOT_GROUP', '0')).split(',')
        if value.strip().isdigit() and int(value.strip()) > 0))
    api = os.environ.get('LLBOT_API', 'http://127.0.0.1:3000')
    delivered = bool(groups)
    for group in groups:
        req = urllib.request.Request(api + '/send_group_msg',
            data=json_bytes({'group_id': group, 'message': text}),
            headers=onebot_headers(), method='POST')
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                ok, _ = onebot_success_utf8(response.read())
            delivered = delivered and ok
            print('Backup notification', group, 'sent' if ok else 'rejected', flush=True)
        except (OSError, ValueError) as error:
            delivered = False
            print('Backup notification', group, type(error).__name__, flush=True)
    return delivered


def run():
    if not PIPELINE:
        print('[怀旧][备份] 未配置备份流水线，本次未执行。', flush=True)
        return
    try:
        result = subprocess.run([PYTHON, PIPELINE, '--manual'], capture_output=True,
                                creationflags=NO_WINDOW)
        raw = result.stdout if result.returncode in (0, 3) else result.stderr
        data = json.loads(raw.decode('utf-8-sig'))
        text = completion_text(result.returncode, data)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        text = '[怀旧][备份失败] ' + type(error).__name__ + '；请查看本地备份日志。'
    print(text, flush=True)
    notify(text)


if __name__ == '__main__' and sys.argv[1:] == ['--run']:
    run()
