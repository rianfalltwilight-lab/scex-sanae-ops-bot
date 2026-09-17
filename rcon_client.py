#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RCON 客户端（参考腐竹工具包 FastRcon / rcon-command.ps1 实现）
- 直连 socket，连接复用（快路径）
- 认证兼容多包响应
- 4096 截断 + 哨兵包收全技巧
- exec 阶段报错不重试（避免副作用命令执行两次）
"""
import socket
import struct
import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rcon-config.json')

from request_runtime import remaining_timeout, check_active

class RconError(Exception):
    pass

class RconClient:
    PACKET_CHARS = 4096
    MAX_BODY_CHARS = 512 * 1024

    def __init__(self, host, port, password, timeout=5):
        self.host = host
        self.port = int(port)
        self.password = password
        self.timeout = timeout
        self._sock = None
        self._next_id = 1

    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=remaining_timeout(self.timeout))
        s.settimeout(remaining_timeout(self.timeout))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = s
        self._authenticate()

    def _pack(self, id_, type_, body):
        payload = body.encode('utf-8')
        length = 4 + 4 + len(payload) + 2
        return struct.pack('<iii', length, id_, type_) + payload + b'\x00\x00'

    def _read_packet(self):
        header = self._recv_exact(4)
        length = struct.unpack('<i', header)[0]
        data = self._recv_exact(length)
        id_, type_ = struct.unpack('<ii', data[:8])
        body = data[8:length - 2].decode('utf-8', errors='replace')
        return id_, type_, body

    def _recv_exact(self, n):
        buf = b''
        while len(buf) < n:
            self._sock.settimeout(remaining_timeout(self.timeout))
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise RconError('RCON 连接断开')
            buf += chunk
        return buf

    def _send(self, id_, type_, body):
        check_active()
        self._sock.settimeout(remaining_timeout(self.timeout))
        self._sock.sendall(self._pack(id_, type_, body))

    def _authenticate(self):
        self._send(self._next_id, 3, self.password)
        self._next_id += 1
        p1 = self._read_packet()
        if p1[0] == -1:
            raise RconError('RCON 认证失败（密码错误？）')
        if p1[0] != self._next_id - 1:
            # 部分实现先回一个包再回认证响应
            p2 = self._read_packet()
            if p2[0] == -1:
                raise RconError('RCON 认证失败')

    def exec(self, command):
        """执行命令并读回完整正文（自动拼接多包响应）。"""
        if self._sock is None:
            self._connect()
        cmd_id = self._next_id
        self._next_id += 1
        if self._next_id > 1000000:
            self._next_id = 1
        self._send(cmd_id, 2, command)  # SERVERDATA_EXECCOMMAND

        deadline = self.timeout
        self._sock.settimeout(remaining_timeout(deadline))
        first = self._read_packet()
        if first[0] == -1:
            raise RconError('RCON 会话失效')
        head = first[2]
        if len(head) < self.PACKET_CHARS:
            return head

        # 顶到单包上限：发哨兵包收全剩余分段
        sentinel_id = self._next_id
        self._next_id += 1
        if self._next_id > 1000000:
            self._next_id = 1
        self._send(sentinel_id, 0, '')
        body = head
        try:
            while True:
                pk = self._read_packet()
                if pk[0] == sentinel_id:
                    break
                if pk[0] == -1:
                    raise RconError('RCON 会话失效')
                if pk[0] == cmd_id and len(body) < self.MAX_BODY_CHARS:
                    body += pk[2]
        except socket.timeout:
            pass  # 非原版实现可能不回哨兵：返回已收到部分
        return body

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def __enter__(self):
        self._connect()
        return self

    def __exit__(self, *args):
        self.close()


def load_config():
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)


def query(command):
    """便捷函数：读配置 + 执行一条 RCON 命令，返回输出。"""
    cfg = load_config()
    with RconClient(cfg['host'], cfg['port'], cfg['password']) as r:
        return r.exec(command)


def query_tps():
    """NeoForge TPS 查询（适配 neoforge tps 输出）。"""
    out = query('neoforge tps')
    # NeoForge 格式: "Overall: 20.000 TPS (1.700 ms/tick)" 或 "Overworld: ..."
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    parts = []
    for line in lines:
        if ':' in line and 'TPS' in line:
            name, rest = line.split(':', 1)
            name = name.strip()
            import re
            m = re.search(r'([\d.]+) TPS \(([\d.]+) ms/tick\)', rest)
            if m:
                tps = float(m.group(1))
                ms = float(m.group(2))
                parts.append(f"{name}: {tps:.1f} TPS ({ms:.1f}ms)")
    if not parts:
        return out.strip()
    return '\n'.join(parts)


def query_list():
    """在线玩家列表。"""
    out = query('list')
    return out.strip()


if __name__ == '__main__':
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'list'
    try:
        if cmd == 'tps':
            print(query_tps())
        elif cmd == 'list':
            print(query_list())
        else:
            print(query(cmd))
    except Exception as e:
        print(f'[RCON错误] {e}')
        sys.exit(1)
