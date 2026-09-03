#!/usr/bin/env python3
"""读取 mc-ops 本地凭据。secrets.json 必须保持 0600 且不进入版本控制。"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

SECRETS_PATH = Path(__file__).with_name("secrets.json")


@lru_cache(maxsize=1)
def load_secrets() -> dict:
    with SECRETS_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError("mc-ops/secrets.json 格式错误")
    return data


def require_secret(name: str) -> str:
    if os.environ.get("SCE_BOT_TEST_MODE") == "1":
        return "test-placeholder"
    value = load_secrets().get(name)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"mc-ops/secrets.json 缺少 {name}")
    return value
