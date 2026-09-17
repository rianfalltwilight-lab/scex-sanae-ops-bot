"""Bounded callback intake and workers; no external services are started here."""
from collections import OrderedDict
from http.server import HTTPServer
from socketserver import ThreadingMixIn
import queue
import socket
import threading
import time


class WorkQueue:
    def __init__(self, workers=4, capacity=64, name='bridge-job'):
        self.queue = queue.Queue(maxsize=capacity)
        self.workers = workers
        self.name = name
        self._lock = threading.Lock()
        self._started = False

    def start(self):
        with self._lock:
            if self._started:
                return
            self._started = True
            for index in range(self.workers):
                threading.Thread(target=self._run, name=f'{self.name}-{index}', daemon=True).start()

    def submit(self, callback, *, block=False):
        self.start()
        try:
            self.queue.put(callback, block=block)
            return True
        except queue.Full:
            return False

    def _run(self):
        while True:
            callback = self.queue.get()
            try:
                callback()
            except Exception as exc:
                # Do not include event bodies, URLs, credentials or user identities.
                print(f'[{self.name}] task failed: {type(exc).__name__}', flush=True)
            finally:
                self.queue.task_done()


class EventInbox:
    def __init__(self, capacity=128, ttl=600, max_ids=5000, executor=None):
        self.executor = executor if executor is not None else WorkQueue(1, capacity, 'bridge-event')
        self.ttl = ttl
        self.max_ids = max_ids
        self._seen = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def key(event):
        mid = event.get('message_id')
        if event.get('post_type') != 'message' or mid is None or mid == '':
            return None
        return tuple(str(event.get(k, ''))[:128] for k in
                     ('self_id', 'message_type', 'group_id', 'user_id', 'message_id'))

    def submit(self, event, callback):
        key = self.key(event)
        now = time.monotonic()
        with self._lock:
            while self._seen and now - next(iter(self._seen.values())) >= self.ttl:
                self._seen.popitem(last=False)
            if key is not None and key in self._seen:
                return 'duplicate'
            if not self.executor.submit(callback):
                return 'full'
            if key is not None:
                self._seen[key] = now
                while len(self._seen) > self.max_ids:
                    self._seen.popitem(last=False)
            return 'accepted'


class CallbackServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def __init__(self, address, handler, *, source_ips=(), max_connections=8,
                 request_seconds=15, inbox=None):
        self.source_ips = frozenset(source_ips)
        self.request_seconds = request_seconds
        self.inbox = inbox if inbox is not None else EventInbox()
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def verify_request(self, request, address):
        return not self.source_ips or address[0] in self.source_ips

    @staticmethod
    def _expire(request):
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        request.close()

    def process_request(self, request, address):
        if not self._slots.acquire(blocking=False):
            try:
                request.settimeout(0.2)
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, address):
        deadline = threading.Timer(self.request_seconds, self._expire, (request,))
        deadline.daemon = True
        deadline.start()
        try:
            super().process_request_thread(request, address)
        finally:
            deadline.cancel()
            self._slots.release()

    def handle_error(self, request, address):
        print('[bridge-http] request failed', flush=True)


JOBS = WorkQueue()
AI_JOBS = WorkQueue(2, 16, 'bridge-ai')


def start_worker(target, args=()):
    # Called by the single event router or a social timer, never by pool workers.
    # Backpressure stays in the router; the bounded inbox returns HTTP 503 when full.
    return JOBS.submit(lambda: target(*args), block=True)


def start_ai_worker(target, args=()):
    return AI_JOBS.submit(lambda: target(*args), block=True)
