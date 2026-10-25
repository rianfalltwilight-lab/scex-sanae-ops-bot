#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minimal Minecraft Java Server List Ping client.

This module deliberately implements the read-only status protocol only.  It
does not expose a command channel and must not be used as an RCON substitute.
"""
from __future__ import annotations

import json
import re
import socket
import struct


MAX_PACKET = 65536
MAX_VARINT_BYTES = 5


class StatusError(ConnectionError):
    """Raised when the public Minecraft status endpoint cannot be queried."""


def _encode_varint(value: int) -> bytes:
    value = int(value)
    if value < 0:
        value &= 0xFFFFFFFF
    result = bytearray()
    while True:
        part = value & 0x7F
        value >>= 7
        if value:
            result.append(part | 0x80)
        else:
            result.append(part)
            return bytes(result)


def _read_exact(sock: socket.socket, size: int) -> bytes:
    if size < 0 or size > MAX_PACKET:
        raise StatusError("状态响应长度异常")
    chunks = bytearray()
    while len(chunks) < size:
        try:
            chunk = sock.recv(size - len(chunks))
        except socket.timeout as exc:
            raise StatusError("状态响应超时") from exc
        except OSError as exc:
            raise StatusError(f"状态连接失败：{exc}") from exc
        if not chunk:
            raise StatusError("状态连接提前关闭")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_varint(sock: socket.socket) -> int:
    value = 0
    shift = 0
    for _ in range(MAX_VARINT_BYTES):
        raw = _read_exact(sock, 1)[0]
        value |= (raw & 0x7F) << shift
        if not raw & 0x80:
            return value
        shift += 7
    raise StatusError("状态响应 VarInt 异常")


def _string_payload(value: str) -> bytes:
    encoded = str(value).encode("utf-8")
    return _encode_varint(len(encoded)) + encoded


def _flatten_text(value) -> str:
    """Flatten vanilla text components without trusting arbitrary fields."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "".join(_flatten_text(item) for item in value)
    if isinstance(value, dict):
        pieces = []
        if value.get("text") is not None:
            pieces.append(_flatten_text(value.get("text")))
        for key in ("extra", "with"):
            if value.get(key) is not None:
                pieces.append(_flatten_text(value.get(key)))
        return "".join(pieces)
    return ""


def clean_description(value) -> str:
    """Return a compact human-readable MOTD with Minecraft colour codes removed."""
    text = re.sub(r"[\r\n]+", " ", _flatten_text(value)).strip()
    text = re.sub(r"§[0-9A-FK-ORa-fk-or]", "", text)
    return re.sub(r"[ \t]+", " ", text)


def query(host: str, port: int, timeout: float = 5.0) -> dict:
    """Query a Java server's status response and return its decoded object."""
    host = str(host or "").strip()
    if not host:
        raise StatusError("未配置状态主机")
    try:
        port = int(port)
    except (TypeError, ValueError) as exc:
        raise StatusError("状态端口配置无效") from exc
    if not 1 <= port <= 65535:
        raise StatusError("状态端口配置无效")
    timeout = max(0.5, min(float(timeout), 15.0))
    # 767 is the protocol used by the current 1.21.1 endpoint.  Status
    # handshakes accept a compatible protocol value on proxies as well.
    address = host.encode("utf-8")
    handshake = (b"\x00" + _encode_varint(767) + _encode_varint(len(address))
                 + address + struct.pack(">H", port) + _encode_varint(1))
    request = b"\x00"
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(_encode_varint(len(handshake)) + handshake)
            sock.sendall(_encode_varint(len(request)) + request)
            frame_length = _read_varint(sock)
            frame = _read_exact(sock, frame_length)
    except StatusError:
        raise
    except (OSError, socket.timeout) as exc:
        raise StatusError(f"状态连接失败：{exc}") from exc
    if not frame:
        raise StatusError("状态响应为空")
    packet_id = frame[0]
    if packet_id != 0:
        raise StatusError(f"状态响应类型异常：{packet_id}")
    # The status packet is a String prefixed by a VarInt.  Decode the VarInt
    # locally so a malformed response cannot make us parse arbitrary bytes.
    value = 0
    shift = 0
    index = 1
    for _ in range(MAX_VARINT_BYTES):
        if index >= len(frame):
            raise StatusError("状态 JSON 不完整")
        raw = frame[index]
        index += 1
        value |= (raw & 0x7F) << shift
        if not raw & 0x80:
            break
        shift += 7
    else:
        raise StatusError("状态 JSON 长度异常")
    if value < 0 or value > MAX_PACKET or index + value > len(frame):
        raise StatusError("状态 JSON 长度异常")
    try:
        payload = json.loads(frame[index:index + value].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StatusError("状态 JSON 无法解析") from exc
    if not isinstance(payload, dict):
        raise StatusError("状态响应格式异常")
    return payload


def player_names(status: dict) -> list[str]:
    players = status.get("players") if isinstance(status, dict) else None
    sample = players.get("sample") if isinstance(players, dict) else None
    names = []
    for item in sample if isinstance(sample, list) else []:
        name = clean_description(item.get("name") if isinstance(item, dict) else item)
        if name:
            names.append(name[:32])
    return names[:80]


def to_list_output(status: dict) -> str:
    players = status.get("players") if isinstance(status, dict) else {}
    players = players if isinstance(players, dict) else {}
    try:
        online = max(0, int(players.get("online", 0)))
    except (TypeError, ValueError):
        online = 0
    try:
        maximum = max(online, int(players.get("max", online)))
    except (TypeError, ValueError):
        maximum = online
    names = ", ".join(player_names(status))
    return f"There are {online} of a max of {maximum} players online: {names}"


def to_version_output(status: dict) -> str:
    version = status.get("version") if isinstance(status, dict) else {}
    version = version if isinstance(version, dict) else {}
    name = clean_description(version.get("name")) or "未知"
    protocol = version.get("protocol")
    return f"{name} (protocol {protocol})" if protocol is not None else name


def to_summary(status: dict) -> str:
    """Compact status text for logs or future dashboard use."""
    return f"{to_version_output(status)}; {to_list_output(status)}"
