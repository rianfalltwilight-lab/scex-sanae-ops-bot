"""User-visible failure labels: short and non-sensitive.

Group replies only carry a coarse class such as "HTTP 503" or "连接超时";
the raw exception (which may contain hosts, paths or proxy details) stays in
the process logs.
"""
import socket
import urllib.error


def public_failure_label(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f'HTTP {exc.code}'
    if type(exc).__name__ == 'RconError':
        # rcon_client raises fixed Chinese phrases intended for operators.
        return str(exc).strip()[:40] or 'RCON 错误'
    text = str(exc)
    lowered = text.lower()
    if isinstance(exc, ConnectionRefusedError) or '10061' in text or 'refused' in lowered:
        return '连接被拒绝'
    if isinstance(exc, (socket.timeout, TimeoutError)) or 'timed out' in lowered:
        return '连接超时'
    if 'SSL' in text or 'EOF' in text:
        return '连接中断'
    if isinstance(exc, (urllib.error.URLError, ConnectionError)):
        return '网络连接失败'
    if isinstance(exc, FileNotFoundError):
        return '文件不存在'
    if isinstance(exc, PermissionError):
        return '没有访问权限'
    if isinstance(exc, ValueError):  # includes json.JSONDecodeError
        return '数据格式错误'
    return '内部错误'
