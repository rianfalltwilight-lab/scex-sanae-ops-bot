#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Server-local CLI for building and validating read-only ops artifacts."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ops_telemetry import OpsTelemetry, publish_snapshot


def _properties_query(server_dir):
    from rcon_client import RconClient
    props = {}
    with open(os.path.join(server_dir, "server.properties"), encoding="utf-8-sig") as stream:
        for line in stream:
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.rstrip("\r\n").split("=", 1)
                props[key] = value

    def query(command):
        password = props.get("rcon.password", "")
        try:
            with RconClient("127.0.0.1", int(props.get("rcon.port", "25575")), password) as client:
                return client.exec(command)
        finally:
            password = None
    return query


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("bootstrap", "refresh", "verify", "reports", "lag", "snapshot", "export"))
    parser.add_argument("--server", required=True, help="configured server id")
    parser.add_argument("--server-dir")
    parser.add_argument("--prefix")
    parser.add_argument("--state-base")
    parser.add_argument("--publish-url")
    parser.add_argument("--export-path")
    parser.add_argument("--deep", action="store_true")
    parser.add_argument("--count", type=int, default=1)
    args = parser.parse_args()

    if args.server_dir:
        server_dir = args.server_dir
        prefix = args.prefix or ("[怀旧]" if args.server == "legacy" else f"[{args.server}]")
        query = _properties_query(server_dir)
    else:
        from server_registry import query_server, resolve_server, server_prefix
        server = resolve_server(args.server)
        if not server or not server.get("server_dir"):
            parser.error("server-dir is required for this server")
        server_dir, prefix = server["server_dir"], server_prefix(server)
        query = lambda command: query_server(server, command)

    telemetry = OpsTelemetry(args.server, prefix, server_dir,
                             state_base=args.state_base, query_fn=query)
    result = {}
    if args.action in ("bootstrap", "refresh"):
        result["indexes"] = telemetry.refresh_indexes(force=args.action == "bootstrap")
    if args.action in ("bootstrap", "verify"):
        result["backup"] = telemetry.verify_backups(count=max(1, min(5, args.count)), deep=args.deep)
    if args.action in ("bootstrap", "reports"):
        result["timeline"] = telemetry.timeline("24h")
        result["postmortem"] = telemetry.incident_postmortem("24h")
        result["weekly"] = telemetry.weekly_report()
    if args.action == "lag":
        result["lag"] = telemetry.collect_lag_evidence("cli-readonly")
    if args.action == "snapshot" or args.publish_url:
        if not args.publish_url:
            parser.error("--publish-url is required for snapshot")
        result["published"] = publish_snapshot(args.publish_url, telemetry)
    if args.action == "export" or args.export_path:
        if not args.export_path:
            parser.error("--export-path is required for export")
        from ops_telemetry import write_snapshot_export
        result["export"] = write_snapshot_export(telemetry, args.export_path)
    print(json.dumps({"ok": True, "server": args.server,
                      "actions": sorted(result.keys())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
