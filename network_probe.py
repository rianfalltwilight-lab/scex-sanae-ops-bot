"""Bounded Windows ICMP and real upload probes for Sanae's network command.

Behavior adapted from i0czf/minecraft-server-ops-kit, QQConsoleBridge.java,
revision 19de94de25efe149d9364ed6572cd634e8138112, lines 15422-15662.
See UPSTREAM-NOTICE.md for the upstream PolyForm Noncommercial 1.0.0 notice.
Only the fixed reference nodes below are probed; no user-supplied target or
file is accepted. Call collect_network from a background worker, not an event
thread. This module neither sends messages nor starts a persistent service.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from types import MappingProxyType

UPSTREAM_REVISION = "19de94de25efe149d9364ed6572cd634e8138112"
REFERENCE_NODES = (
    ("电信", "202.96.128.86"),
    # DNS servers can rate-limit ping themselves. 10-27 from the game host: 221.5.88.88 lost 30-42 % by
    # day and 90 % at 20:20, 210.21.196.6 lost 0 % by day but 63 % at 20:20, while the Unicom routers
    # in front of each lost 0-7 %. 202.99.160.68 lost 0 of 180 at both times.
    # APNIC RDAP: UNICOM-HE, "China Unicom Hebei province network" (checked 2026-10-27).
    ("联通", "202.99.160.68"),
    ("移动", "211.136.20.203"),
    # Replaces upstream's incorrectly labelled China Mobile Hubei address.
    # APNIC RDAP identifies 103.27.24.1 as GCableNET / Guangdong Cable.
    # https://rdap.apnic.net/ip/103.27.24.1 (checked 2026-09-20)
    ("广电（广东有线参考节点）", "103.27.24.1"),
)
# A second node per carrier for the game-host check: loss that only one of the two shows is that
# node limiting ping, not the path (both checked with APNIC RDAP on 2026-10-27).
BACKUP_NODES = {"电信": "202.96.134.133",     # shenzhen12012 (China Telecom Shenzhen)
                "联通": "119.6.6.6",          # UNICOM-SC, China Unicom Sichuan
                "移动": "211.136.192.6"}      # CMNET-guangdong
UPLOAD_URL = "https://speed.cloudflare.com/__up"
UPLOAD_HOST = "speed.cloudflare.com"
MODE_LIMITS = MappingProxyType({
    "normal": MappingProxyType({"total_seconds": 55, "rounds": 1, "bytes_per_round": 48_000_000,
                               "max_upload_bytes": 48_000_000}),
    "full": MappingProxyType({"total_seconds": 100, "rounds": 3, "bytes_per_round": 25_000_000,
                             "max_upload_bytes": 75_000_000}),
})
PING_COUNT = 4
PING_REPLY_TIMEOUT_MS = 2000
PING_PROCESS_SECONDS = 12
UPLOAD_ROUND_SECONDS = 28
REAP_SECONDS = 2
MAX_RESPONSE_BYTES = 65_536
CHUNK_BYTES = 65_536


def _unknown(reason: str, **fields) -> dict:
    return {"status": "unknown", "reason": reason, **fields}


def _decode_ping(output: bytes) -> str:
    try:
        return output.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return output.decode("gb18030", errors="replace")


def _parse_ping(output: bytes) -> dict:
    """Parse actual Windows echo statistics, preserving sub-ms upper bounds."""
    text = _decode_ping(output)
    replies = re.findall(r"(?i)(?:时间|time)\s*([=<])\s*(\d+(?:\.\d+)?)\s*(?:ms|毫秒)", text)
    average = re.search(r"(?i)(?:平均|Average)\s*=\s*(\d+(?:\.\d+)?)\s*(?:ms|毫秒)", text)
    lost = re.search(r"[（(]\s*(\d+(?:\.\d+)?)\s*%\s*(?:丢失|loss)\s*[)）]", text, re.I)
    loss_percent = float(lost.group(1)) if lost else None
    if average or replies:
        values = [float(value) for _operator, value in replies]
        sub_ms = bool(replies) and all(operator == "<" and float(value) <= 1 for operator, value in replies)
        avg_ms = float(average.group(1)) if average else sum(values) / len(values)
        return {"status": "ok", "avg_ms": avg_ms, "less_than_1ms": sub_ms,
                "replies": len(replies) if replies else None, "loss_percent": loss_percent}
    return _unknown("no_icmp_reply", avg_ms=None, less_than_1ms=False, replies=0,
                    loss_percent=loss_percent)


def _system_binary(name: str) -> str:
    if os.name != "nt":
        raise OSError("Windows required")
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / name)


def _run_process(arguments: list[str], timeout: float) -> tuple[int, bytes]:
    """No shell, hidden Windows window, bounded wait, kill and reap on timeout."""
    if timeout <= 0:
        raise TimeoutError()
    process = subprocess.Popen(
        arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), shell=False,
    )
    try:
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=REAP_SECONDS)
            raise TimeoutError() from None
        return process.returncode, output
    finally:
        # Also handles cancellation or unexpected communication errors.
        if process.poll() is None:
            process.kill()
            process.wait(timeout=REAP_SECONDS)
        if process.stdout is not None:
            process.stdout.close()


def _measure_icmp(label: str, address: str, deadline: float) -> dict:
    base = {"label": label, "address": address, "method": "ICMP echo", "sent": PING_COUNT}
    remaining = deadline - time.monotonic() - REAP_SECONDS
    if remaining <= 0:
        return _unknown("deadline", **base, avg_ms=None)
    try:
        arguments = [_system_binary("ping.exe"), "-n", str(PING_COUNT), "-w",
                     str(PING_REPLY_TIMEOUT_MS), address]
        _code, output = _run_process(arguments, min(PING_PROCESS_SECONDS, remaining))
        return {**base, **_parse_ping(output)}
    except TimeoutError:
        return _unknown("icmp_timeout", **base, avg_ms=None)
    except Exception as error:
        return _unknown(type(error).__name__, **base, avg_ms=None)


def _make_payload(size: int, deadline: float) -> Path:
    """Generate only random bytes, bounded to one sample; never read user data."""
    handle = tempfile.NamedTemporaryFile(prefix="sanae-netprobe-", suffix=".bin", delete=False)
    path = Path(handle.name)
    try:
        with handle:
            remaining = size
            while remaining:
                if time.monotonic() >= deadline - REAP_SECONDS:
                    raise TimeoutError()
                amount = min(CHUNK_BYTES, remaining)
                handle.write(os.urandom(amount))
                remaining -= amount
        return path
    except BaseException:
        handle.close()
        path.unlink(missing_ok=True)
        raise


def _upload_arguments(binary: str, path: Path, size: int, timeout: float) -> list[str]:
    # curl knows the generated file's exact size; no redirects/retries and no
    # external curlrc/proxy settings. System TUN routing remains outside this
    # application's control and is not claimed to be bypassed.
    return [binary, "--disable", "--silent", "--show-error", "--fail", "--noproxy", "*",
            "--proxy", "", "--proto", "=https", "--max-redirs", "0", "--retry", "0",
            "--connect-timeout", str(min(8, timeout)), "--max-time", str(timeout),
            "--max-filesize", str(MAX_RESPONSE_BYTES), "--http1.1", "--request", "POST",
            "--header", "Content-Type: application/octet-stream", "--header", "Expect:",
            "--user-agent", "Sanae-Network-Probe/1.0", "--upload-file", str(path),
            "--output", os.devnull, "--write-out",
            "%{http_code}\t%{size_upload}\t%{time_total}\t%{size_download}", UPLOAD_URL]


def _parse_upload(code: int, output: bytes, expected_size: int) -> dict:
    if code != 0:
        return _unknown("curl_failed", exit_code=code)
    try:
        http_text, sent_text, duration_text, downloaded_text = output.decode("ascii").strip().split("\t")
        http_code = int(http_text)
        sent, seconds, downloaded = float(sent_text), float(duration_text), float(downloaded_text)
        if not all(math.isfinite(value) for value in (sent, seconds, downloaded)):
            raise ValueError()
        if not 200 <= http_code < 300 or sent != expected_size or seconds <= 0 or not 0 <= downloaded <= MAX_RESPONSE_BYTES:
            raise ValueError()
    except (ValueError, UnicodeError):
        return _unknown("invalid_or_incomplete_upload")
    return {"status": "ok", "bytes": int(sent), "seconds": seconds,
            "mbps": sent * 8 / seconds / 1_000_000, "http_status": http_code}


def _measure_upload(size: int, deadline: float) -> dict:
    result_base = {"source": UPLOAD_HOST, "method": "HTTPS POST", "requested_bytes": size}
    path = None
    try:
        if deadline - time.monotonic() <= REAP_SECONDS:
            return _unknown("deadline", **result_base)
        binary = _system_binary("curl.exe")
        path = _make_payload(size, deadline)
        remaining = deadline - time.monotonic() - REAP_SECONDS
        if remaining <= 0:
            return _unknown("deadline", **result_base)
        timeout = min(UPLOAD_ROUND_SECONDS, remaining)
        code, output = _run_process(_upload_arguments(binary, path, size, timeout), timeout)
        return {**result_base, **_parse_upload(code, output, size)}
    except TimeoutError:
        return _unknown("upload_timeout", **result_base)
    except Exception as error:
        return _unknown(type(error).__name__, **result_base)
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def collect_network(mode: str = "normal") -> dict:
    """Collect fixed-target probes within MODE_LIMITS; invalid modes do no I/O.

    Application upload payload is capped at 48 MB / 75 MB (decimal), excluding
    transport overhead. No retry/fallback duplicates a payload. Each successful
    sample measures uploaded bytes / complete HTTPS request duration, including
    connection/TLS/response time, rather than claiming raw line capacity.
    """
    if not isinstance(mode, str) or mode not in MODE_LIMITS:
        raise ValueError("mode must be normal or full")
    limits = MODE_LIMITS[mode]
    started = time.monotonic()
    deadline = started + limits["total_seconds"]
    with ThreadPoolExecutor(max_workers=len(REFERENCE_NODES), thread_name_prefix="sanae-icmp") as pool:
        futures = [pool.submit(_measure_icmp, label, address, deadline) for label, address in REFERENCE_NODES]
        latency = [future.result() for future in futures]
    samples = []
    for _ in range(limits["rounds"]):
        if time.monotonic() >= deadline - REAP_SECONDS:
            break
        samples.append(_measure_upload(limits["bytes_per_round"], deadline))
    successful = [sample for sample in samples if sample["status"] == "ok"]
    speeds = [sample["mbps"] for sample in successful]
    upload = {
        "status": "ok" if successful else "unknown", "source": UPLOAD_HOST, "method": "HTTPS POST",
        "mbps": sum(speeds) / len(speeds) if speeds else None,
        "min_mbps": min(speeds) if speeds else None, "max_mbps": max(speeds) if speeds else None,
        "successful_rounds": len(successful), "planned_rounds": limits["rounds"], "samples": samples,
        "bytes": sum(sample["bytes"] for sample in successful),
        "seconds": sum(sample["seconds"] for sample in successful),
        "max_upload_bytes": limits["max_upload_bytes"],
    }
    return {"mode": mode, "latency": latency, "upload": upload,
            "elapsed_seconds": time.monotonic() - started, "total_limit_seconds": limits["total_seconds"],
            "upstream_revision": UPSTREAM_REVISION}


def render_network(result: dict) -> str:
    lines = ["[网络]" + (" 加强档" if result["mode"] == "full" else ""),
             "▶ 运营商参考节点（机器人主机 ICMP；服务器主机实测请问早苗“服务器网络怎么样”）"]
    for node in result["latency"]:
        if node["status"] == "ok":
            value = "<1 ms" if node.get("less_than_1ms") else f"{node['avg_ms']:.1f} ms"
            loss = node.get("loss_percent")
            if loss is not None:
                value += f"，丢包 {loss:g}%"
        else:
            value = "未知（无有效 ICMP 回应）"
        lines.append(f"{node['label']}：{value}")
    upload = result["upload"]
    lines.append("\n▶ 上传带宽（机器人主机上行）")
    if upload["status"] == "ok":
        lines.append(f"上传：约 {upload['mbps']:.1f} Mbps")
        lines.append(f"有效轮次：{upload['successful_rounds']}/{upload['planned_rounds']}"
                     + (f"，区间 {upload['min_mbps']:.1f}–{upload['max_mbps']:.1f} Mbps"
                        if upload["successful_rounds"] > 1 else ""))
        lines.append(f"有效样本：{upload['bytes'] / 1_000_000:.1f} MB / {upload['seconds']:.1f} 秒")
        speed = upload["mbps"]
        state = "充裕" if speed >= 35 else "尚可" if speed >= 20 else "偏紧" if speed >= 8 else "偏弱"
        lines.append(f"状态：上行{state}（参考）")
    else:
        lines.append("上传：未知（未完成有效上传样本）")
    lines.extend([f"测速源：{UPLOAD_HOST}（HTTPS 上传）",
                  f"最大上传量：{upload['max_upload_bytes'] / 1_000_000:.0f} MB（不含协议开销）",
                  "仅供参考，不代表玩家实际链路。",
                  f"\n检测耗时：{result['elapsed_seconds']:.1f} 秒"])
    if result["mode"] == "normal":
        lines.append("提示：!ping full 可加测三轮。")
    return "\n".join(lines)
