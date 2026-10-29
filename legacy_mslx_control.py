"""Confirmed Legacy controls use the existing audited MSLX fleet entry point."""
from pathlib import Path
import base64
import os
import subprocess
import sys

from legacy_backup_control import notify

HERE = Path(__file__).resolve().parent
POWERSHELL = os.environ.get('SANAE_MSLX_POWERSHELL', 'powershell.exe')
FLEET = os.environ.get('SANAE_MSLX_CONTROL_SCRIPT', '').strip()
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
LABELS = {'restart': '重启', 'stop': '停服'}
# A restart includes stop and start waits; bound the external controller's total runtime.
RUN_TIMEOUT_SECONDS = 35 * 60


def control_args(action, uid):
    if action not in LABELS:
        raise ValueError('Unsupported Legacy action')
    if not FLEET:
        raise ValueError('SANAE_MSLX_CONTROL_SCRIPT is not configured')
    def literal(value):
        return "'" + str(value).replace("'", "''") + "'"
    # Windows PowerShell 5 -File treats an explicit switch value as a string.
    # Invoke within PowerShell so Confirm receives the actual Boolean $false.
    code = ("$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); & "
            + literal(FLEET) + " -Target Legacy -Action " + literal(action)
            + " -ApprovalReference " + literal('Sanae confirmed !%s by QQ %s' % (action, uid))
            + " -ApprovePlayerImpact -NoticeSeconds 60 -TimeoutSeconds 900 -Confirm:$false")
    return [POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-EncodedCommand', base64.b64encode(code.encode('utf-16-le')).decode('ascii')]


def start(action, uid):
    if not FLEET:
        return '[控制台] 尚未配置 MSLX 控制入口，本次未执行。'
    control_args(action, uid)
    directory = HERE / 'logs'
    directory.mkdir(exist_ok=True)
    with (directory / 'legacy-mslx-control.log').open('ab') as log:
        subprocess.Popen([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()),
                          '--run', action, str(uid)], cwd=str(HERE),
                         stdout=log, stderr=subprocess.STDOUT,
                         creationflags=NO_WINDOW, close_fds=True)
    return ('[控制台] 已提交怀旧服%s到 MSLX；有人在线时先公告并等待 60 秒，再保存停服。'
            '\n完成或失败后会回群通知。') % LABELS[action]


def server_state():
    """One line on whether Legacy answers its console right now; never raises."""
    try:
        import server_registry
        server = server_registry.resolve_server('legacy')
        if not server:
            return '查不到怀旧服的配置'
        players = (server_registry.parse_player_list(server_registry.query_server(server, 'list')) or {}).get('players')
        return '怀旧服现在能正常响应' + ('，在线 %d 人' % len(players) if players is not None else '')
    except Exception as error:
        return '怀旧服现在没有响应（%s）' % type(error).__name__


def _stop_tree(process):
    try:
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(process.pid)], capture_output=True,
                       timeout=30, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        process.kill()


def run(action, uid):
    label = LABELS[action]
    try:
        process = subprocess.Popen(control_args(action, uid), cwd=str(HERE), creationflags=NO_WINDOW)
        try:
            returncode = process.wait(timeout=RUN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _stop_tree(process)
            returncode = None
        if returncode == 0:
            text = ('[怀旧][控制台] MSLX 已重新启动怀旧服，正在加载。'
                    if action == 'restart' else '[怀旧][控制台] MSLX 已停止怀旧服。')
        elif returncode is None:
            text = ('[怀旧][控制台] %s在 %d 分钟内没有完成（MSLX 控制一直没有回音），已停止等待；%s。'
                    '请管理员确认 MSLX 状态。' % (label, RUN_TIMEOUT_SECONDS // 60, server_state()))
        else:
            text = '[怀旧][控制台] %s失败；%s。请检查 MSLX 状态和本地控制日志。' % (label, server_state())
    except (OSError, subprocess.SubprocessError) as error:
        text = '[怀旧][控制台] %s失败：%s；请检查本地控制日志。' % (label, type(error).__name__)
    print(text, flush=True)
    notify(text)


if __name__ == '__main__' and len(sys.argv) == 4 and sys.argv[1] == '--run':
    run(sys.argv[2], sys.argv[3])
