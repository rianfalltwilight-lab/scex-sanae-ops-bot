#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Portable read-only recipe index and compact PNG renderer.

The indexing model follows minecraft-server-ops-kit's recipe lookup: recipes
are discovered from installed JAR data packs instead of a hard-coded list.
This version deliberately keeps the renderer small and cross-platform.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import zipfile
from pathlib import Path


_RECIPE_PATH = re.compile(r"^data/([^/]+)/(?:recipes|recipe)/(.+)\.json$", re.I)
_LANG_PATH = re.compile(r"^assets/([^/]+)/lang/(zh_cn|en_us)\.json$", re.I)


def recipe_signature(server_dir):
    root = Path(server_dir)
    digest = hashlib.sha256()
    paths = list((root / "mods").glob("*.jar")) + list((root / "datapacks").glob("**/*.zip"))
    for path in sorted(paths, key=lambda item: str(item).casefold()):
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(path.name.encode("utf-8", "replace"))
        digest.update(("%d:%d" % (stat.st_size, int(stat.st_mtime_ns))).encode("ascii"))
    return digest.hexdigest()


def _item_id(value):
    if isinstance(value, str):
        return value if ":" in value else ""
    if isinstance(value, dict):
        for key in ("id", "item", "value", "name"):
            found = value.get(key)
            if isinstance(found, str) and ":" in found:
                return found
        if isinstance(value.get("item"), dict):
            return _item_id(value["item"])
    return ""


def _count(value):
    if isinstance(value, dict):
        for key in ("count", "Count", "amount"):
            try:
                return max(1, int(value.get(key, 1)))
            except (TypeError, ValueError):
                pass
    return 1


def _ingredient(value):
    if isinstance(value, list):
        options = [_ingredient(item) for item in value]
        return " | ".join(item for item in options if item)[:240]
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    if value.get("tag"):
        return "#" + str(value["tag"])
    found = _item_id(value)
    if found:
        return found
    for key in ("ingredient", "base", "addition", "input"):
        if key in value:
            found = _ingredient(value[key])
            if found:
                return found
    return ""


def _parse_recipe(recipe_id, raw, source):
    recipe_type = str(raw.get("type") or "unknown")[:160]
    result_value = raw.get("result")
    if result_value is None:
        for key in ("output", "result_item", "primaryOutput"):
            if key in raw:
                result_value = raw[key]
                break
    result = _item_id(result_value)
    result_count = _count(result_value)
    pattern = raw.get("pattern") if isinstance(raw.get("pattern"), list) else []
    key_map = {}
    if isinstance(raw.get("key"), dict):
        for key, value in raw["key"].items():
            key_map[str(key)[:4]] = _ingredient(value)
    ingredients = []
    for field in ("ingredients", "ingredient", "input", "inputs", "base", "addition"):
        value = raw.get(field)
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            rendered = _ingredient(item)
            if rendered and rendered not in ingredients:
                ingredients.append(rendered)
    for item in key_map.values():
        if item and item not in ingredients:
            ingredients.append(item)
    if not result:
        return None
    return {
        "id": recipe_id, "type": recipe_type, "result": result,
        "count": result_count, "pattern": [str(row)[:9] for row in pattern[:9]],
        "key": key_map, "ingredients": ingredients[:24], "source": source,
    }


def _merge_lang(target, raw):
    if not isinstance(raw, dict):
        return
    for key, value in raw.items():
        if isinstance(value, str) and len(value) <= 160:
            target[str(key)] = value


def _item_names(item_ids, zh, en):
    names = {}
    for item_id in item_ids:
        if ":" not in item_id:
            continue
        namespace, path = item_id.split(":", 1)
        candidates = ("item.%s.%s" % (namespace, path.replace("/", ".")),
                      "block.%s.%s" % (namespace, path.replace("/", ".")))
        cn = next((zh[key] for key in candidates if key in zh), "")
        english = next((en[key] for key in candidates if key in en), "")
        names[item_id] = {"zh": cn, "en": english}
    return names


def build_recipe_index(server_dir, server_id="unknown", prefix="[未知服]", max_recipes=100000):
    root = Path(server_dir)
    sources = list((root / "mods").glob("*.jar")) + list((root / "datapacks").glob("**/*.zip"))
    recipes = []
    zh, en = {}, {}
    errors = []
    for source in sorted(sources, key=lambda item: str(item).casefold()):
        try:
            with zipfile.ZipFile(str(source)) as archive:
                for name in archive.namelist():
                    lang_match = _LANG_PATH.match(name)
                    if lang_match:
                        try:
                            data = json.loads(archive.read(name).decode("utf-8-sig", "replace"))
                            _merge_lang(zh if lang_match.group(2).casefold() == "zh_cn" else en, data)
                        except (ValueError, KeyError):
                            pass
                for name in archive.namelist():
                    match = _RECIPE_PATH.match(name)
                    if not match or len(recipes) >= max_recipes:
                        continue
                    try:
                        raw = json.loads(archive.read(name).decode("utf-8-sig", "replace"))
                        parsed = _parse_recipe(match.group(1) + ":" + match.group(2), raw, source.name)
                        if parsed:
                            recipes.append(parsed)
                    except (ValueError, KeyError, UnicodeError):
                        continue
        except (OSError, zipfile.BadZipFile) as exc:
            if len(errors) < 20:
                errors.append("%s: %s" % (source.name, type(exc).__name__))
    item_ids = {row["result"] for row in recipes}
    for row in recipes:
        item_ids.update(value for value in row.get("ingredients", []) if ":" in value and " | " not in value)
        item_ids.update(value for value in row.get("key", {}).values() if ":" in value and " | " not in value)
    return {
        "schema": 1, "server": server_id, "prefix": prefix,
        "generated_at": time.time(), "signature": recipe_signature(root),
        "recipe_count": len(recipes), "source_count": len(sources),
        "truncated": len(recipes) >= max_recipes, "errors": errors,
        "names": _item_names(item_ids, zh, en), "recipes": recipes,
    }


def item_label(index, item_id):
    names = (index.get("names") or {}).get(item_id) or {}
    return names.get("zh") or names.get("en") or item_id


def query_recipes(index, query, limit=4):
    needle = re.sub(r"\s+", " ", str(query or "")).strip().casefold()
    if not needle:
        return []
    scored = []
    for row in index.get("recipes") or []:
        result = str(row.get("result") or "")
        label = item_label(index, result)
        haystack = " ".join((result, label, str(row.get("id") or ""))).casefold()
        if needle not in haystack:
            continue
        score = 100 if needle == result.casefold() or needle == label.casefold() else 60
        if result.casefold().endswith(":" + needle):
            score += 20
        scored.append((score, str(row.get("id") or ""), row))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [row for _, _, row in scored[:max(1, int(limit))]]


def format_recipe(index, row, prefix=None):
    prefix = prefix or index.get("prefix") or "[未知服]"
    result = row.get("result") or "unknown"
    lines = ["%s 配方：%s ×%s" % (prefix, item_label(index, result), row.get("count", 1)),
             "ID：%s" % result, "类型：%s" % row.get("type", "unknown")]
    if row.get("pattern"):
        lines.append("形状：" + " / ".join(row["pattern"]))
    if row.get("ingredients"):
        labels = [item_label(index, item) if not item.startswith("#") and " | " not in item else item
                  for item in row["ingredients"]]
        lines.append("材料：" + "、".join(labels[:16]))
    return "\n".join(lines)


def _font(size, bold=False):
    from PIL import ImageFont
    candidates = (
        "/mnt/c/Windows/Fonts/msyhbd.ttc" if bold else "/mnt/c/Windows/Fonts/msyh.ttc",
        str(Path(os.environ.get("WINDIR", "Windows")) / "Fonts" / "msyhbd.ttc") if bold else str(Path(os.environ.get("WINDIR", "Windows")) / "Fonts" / "msyh.ttc"),
        "/System/Library/Fonts/PingFang.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for path in candidates:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_recipe_png(index, row, output_path):
    from PIL import Image, ImageDraw
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 940, 500
    image = Image.new("RGB", (width, height), "#171b26")
    draw = ImageDraw.Draw(image)
    title_font, body_font, small_font = _font(30, True), _font(22), _font(17)
    accent, panel, text, muted = "#79d4ff", "#242b3a", "#f5f7fb", "#aab4c5"
    draw.rounded_rectangle((18, 18, width - 18, height - 18), radius=18,
                           fill=panel, outline="#45516b", width=2)
    result = row.get("result") or "unknown"
    draw.text((42, 38), "%s  %s ×%s" %
              (index.get("prefix") or "[未知服]", item_label(index, result), row.get("count", 1)),
              font=title_font, fill=accent)
    draw.text((44, 88), "%s · %s" % (result, row.get("type", "unknown")),
              font=small_font, fill=muted)
    pattern = list(row.get("pattern") or [])
    key_map = row.get("key") or {}
    if pattern:
        cell, left, top = 100, 55, 142
        for y in range(3):
            line = pattern[y] if y < len(pattern) else ""
            for x in range(3):
                rect = (left + x * cell, top + y * cell, left + (x + 1) * cell - 8,
                        top + (y + 1) * cell - 8)
                draw.rounded_rectangle(rect, radius=10, fill="#111722", outline="#526079", width=2)
                symbol = line[x] if x < len(line) else " "
                item = key_map.get(symbol, "") if symbol != " " else ""
                if item:
                    label = item_label(index, item).replace("#", "#")
                    if len(label) > 8:
                        label = label[:7] + "…"
                    draw.multiline_text((rect[0] + 8, rect[1] + 26), label, font=small_font,
                                        fill=text, spacing=2, align="center")
        text_left = 390
    else:
        text_left = 55
    draw.text((text_left, 142), "材料", font=body_font, fill=accent)
    y = 182
    ingredients = row.get("ingredients") or []
    for item in ingredients[:10]:
        label = item_label(index, item) if not item.startswith("#") and " | " not in item else item
        draw.text((text_left, y), "• " + label[:38], font=body_font, fill=text)
        y += 32
    if not ingredients:
        draw.text((text_left, y), "该配方未暴露标准材料字段", font=body_font, fill=muted)
    draw.text((44, height - 50), "索引来源：本服已安装 JAR；不调用外部百科或 RCON",
              font=small_font, fill=muted)
    image.save(str(output_path), format="PNG", optimize=True)
    return str(output_path)


def atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(str(temp), str(path))

