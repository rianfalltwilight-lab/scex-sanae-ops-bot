"""Bounded asynchronous QQ entry for the upstream-derived network diagnostic."""
import math
import re
import threading
import time
import unicodedata

from bridge_runtime import WorkQueue
from network_probe import collect_network, render_network


def parse_ping(raw):
    text = unicodedata.normalize('NFKC', str(raw or ''))
    text = re.sub(r'\[CQ:[^\]]*\]', '', text).strip()
    if not text.startswith('!'):
        return None
    words = text[1:].strip().casefold().split()
    if not words or words[0] not in ('ping', '测速', '网络', 'nettest'):
        return None
    if len(words) == 1:
        return 'normal'
    if len(words) == 2 and words[1] in ('full', '带宽', 'speed', '详细', 'all'):
        return 'full'
    return 'invalid'


def _start_thread(callback):
    threading.Thread(target=callback, name='sanae-network-probe', daemon=True).start()


class PingCommands:
    def __init__(self, *, collector=collect_network, renderer=render_network,
                 clock=time.monotonic, starter=_start_thread, replies=None):
        self.collector, self.renderer = collector, renderer
        self.clock, self.starter = clock, starter
        self.replies = replies if replies is not None else WorkQueue(1, 8, 'network-reply')
        self.lock = threading.Lock()
        self.busy = False
        self.last_started = None

    @staticmethod
    def _send(send, text):
        try:
            send(text)
        except Exception as exc:
            print(f'[network] reply failed: {type(exc).__name__}', flush=True)

    def _reply(self, send, text):
        # A burst of commands cannot fill the main event/command queue.
        self.replies.submit(lambda: self._send(send, text), block=False)

    def handle(self, raw, send):
        mode = parse_ping(raw)
        if mode is None:
            return False
        if mode == 'invalid':
            self._reply(send, '[网络] 用法：!ping 或 !ping full（加强检测）；不接受自定义测速地址。')
            return True
        with self.lock:
            now = self.clock()
            if self.busy:
                status = '[网络] 正在检测中，请稍候…'
            elif self.last_started is not None and now - self.last_started < 90:
                remaining = math.ceil(90 - (now - self.last_started))
                status = f'[网络] 测速冷却中，请 {remaining} 秒后再试。'
            else:
                status = None
                previous_started = self.last_started
                self.busy = True
                self.last_started = now
        if status:
            self._reply(send, status)
            return True

        def run():
            try:
                suffix = '（加强档，最多 100 秒）' if mode == 'full' else '（最多 55 秒）'
                self._send(send, '[网络] 正在测运营商参考节点延迟与上传带宽' + suffix + '…')
                self._send(send, self.renderer(self.collector(mode=mode)))
            except Exception as exc:
                print(f'[network] probe failed: {type(exc).__name__}', flush=True)
                self._send(send, '[网络] 检测失败，未取得有效结果。')
            finally:
                with self.lock:
                    self.busy = False

        try:
            self.starter(run)
        except Exception as exc:
            with self.lock:
                self.busy = False
                self.last_started = previous_started
            print(f'[network] worker unavailable: {type(exc).__name__}', flush=True)
            self._reply(send, '[网络] 检测任务未启动，请稍后重试。')
        return True


_commands = PingCommands()


def handle(raw, send):
    return _commands.handle(raw, send)
