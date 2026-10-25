import http.client
import json
import socket
import threading
import time
import types
import unittest

from llbot_bridge import BridgeHandler
from bridge_runtime import CallbackServer, EventInbox


class Executor:
    def __init__(self):self.items=[];self.full=False
    def submit(self, callback):
        if self.full:return False
        self.items.append(callback);return True


class CallbackHTTPTests(unittest.TestCase):
    def setUp(self):
        self.executor=Executor()
        self.server=CallbackServer(('127.0.0.1',0),BridgeHandler,source_ips={'127.0.0.1'},
                                   max_connections=2,request_seconds=0.5,
                                   inbox=EventInbox(executor=self.executor))
        self.port=self.server.server_address[1]
        self.endpoint=('127.0.0.1',self.port)
        if hasattr(socket, '_sanae_test_endpoints'):
            socket._sanae_test_endpoints.add(self.endpoint)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':0.02},daemon=True)
        self.thread.start()

    def tearDown(self):
        try:
            self.server.shutdown();self.server.server_close();self.thread.join(2)
        finally:
            if hasattr(socket, '_sanae_test_endpoints'):
                socket._sanae_test_endpoints.discard(self.endpoint)

    def post(self, body=b'{"post_type":"meta_event"}'):
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=2)
        try:
            connection.request('POST','/',body=body,headers={'Content-Type':'application/json'})
            response=connection.getresponse();response.read();return response.status
        finally:connection.close()

    def test_slow_incomplete_request_does_not_block_next_request(self):
        slow=socket.create_connection(('127.0.0.1',self.port),timeout=2)
        try:
            slow.sendall(b'POST / HTTP/1.1\r\nHost: fixture\r\nContent-Length: 100\r\n\r\nx')
            time.sleep(0.03)
            started=time.monotonic()
            self.assertEqual(self.post(),200)
            self.assertLess(time.monotonic()-started,0.4)
            # The absolute request deadline closes even a partially received body.
            while slow.recv(4096):pass
            self.assertLess(time.monotonic()-started,1.5)
        finally:slow.close()

    def test_full_queue_is_retryable_and_bad_json_is_rejected(self):
        self.executor.full=True
        self.assertEqual(self.post(),503)
        self.executor.full=False
        self.assertEqual(self.post(b'[]'),400)
        self.assertEqual(self.post(),200)

    def test_connection_cap_rejects_third_connection(self):
        connections=[]
        try:
            for _ in range(2):
                client=socket.create_connection(('127.0.0.1',self.port),timeout=2)
                client.sendall(b'POST / HTTP/1.1\r\n')
                connections.append(client)
            time.sleep(0.03)
            try:
                self.assertEqual(self.post(),503)
            except (ConnectionResetError, ConnectionAbortedError, http.client.RemoteDisconnected):
                # Windows may discard the 503 when closing a socket with unread input.
                pass
            self.assertEqual(self.executor.items, [])
        finally:
            for client in connections:client.close()
