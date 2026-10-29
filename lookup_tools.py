"""Read-only lookups Sanae can make in a conversation (round 30, 10-27).

recipe_lookup   the server's own recipe index (state/ops/<id>/recipe-index.json, built from the
                installed mod jars and datapacks: 26828 recipes on 10-27) by result, by ingredient or
                by machine; KubeJS server scripts that mention the item are named, because script
                changes are not in the index
mod_info        a mod by name: installed here or not (the mirrored inventory), Modrinth (with the
                1.21.1 NeoForge versions), CurseForge and MC 百科 when those answer
bilibili_info   what a b23.tv / BV / live.bilibili.com link is: title, uploader, length, counts and
                the description from Bilibili's public API (direct connection: through the system
                proxy the API answers 412)

Everything returned is reference material; descriptions and titles come from other people and are
marked as such for the model.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_INDEX_CACHE = {}
# Titanium registers test recipes (dirt -> diamond) that no player can make.
SKIP_TYPES = frozenset({'titanium:test_serializer'})
TYPE_LABELS = {'minecraft:crafting_shaped': '工作台（有序）', 'minecraft:crafting_shapeless': '工作台（无序）',
               'minecraft:smelting': '熔炉', 'minecraft:blasting': '高炉', 'minecraft:smoking': '烟熏炉',
               'minecraft:campfire_cooking': '营火', 'minecraft:stonecutting': '切石机',
               'minecraft:smithing_transform': '锻造台', 'minecraft:smithing_trim': '锻造台（纹饰）',
               'create:mixing': '机械动力搅拌（工作盆 + 动力搅拌器）', 'create:compacting': '机械动力压块（工作盆 + 动力冲压机）',
               'create:pressing': '机械动力冲压（动力冲压机）', 'create:crushing': '机械动力粉碎轮',
               'create:milling': '机械动力石磨', 'create:cutting': '机械动力动力锯', 'create:deploying': '机械动力机械手',
               'create:filling': '机械动力注液器', 'create:emptying': '机械动力分液池',
               'create:splashing': '机械动力鼓风机洗涤（鼓风机吹水）', 'create:haunting': '机械动力鼓风机缠魂（鼓风机吹灵魂火）',
               'create:mechanical_crafting': '机械动力动力合成器', 'create:sequenced_assembly': '机械动力序列组装',
               'create:sandpaper_polishing': '砂纸打磨', 'create:item_application': '手持物品对方块右键'}
# Create's basin heat (from the recipe's heat_requirement). Lava and campfires only count as
# "smouldering", which no heated recipe accepts.
HEAT_TEXT = {'heated': '需要加热：工作盆正下方放烈焰人燃烧室并喂燃料（岩浆、营火不算）',
             'superheated': '需要超级加热：烈焰人燃烧室要喂烈焰蛋糕'}
# mode=machine (10-28): blocks whose IDs share no word with the recipe types they run. Checked on the
# server's index: 搅拌器/注液器/鼓风机/工作盆/魔力池 found nothing before, 熔炉 found only thermal:furnace.
MACHINE_TYPES = [
    (r'create(casing)?:(\w+_)?spout', ('create:filling',)),
    (r'create(casing)?:(\w+_)?mechanical_saw', ('create:cutting',)),
    (r'create(casing)?:(\w+_)?encased_fan', ('create:splashing', 'create:haunting')),
    (r'create(casing)?:(\w+_)?item_drain', ('create:emptying',)),
    (r'create:basin', ('create:mixing', 'create:compacting')),
    (r'create(casing)?:(\w+_)?mechanical_press', ('create:pressing', 'create:compacting')),
    (r'create(casing)?:(\w+_)?deployer', ('create:deploying', 'create:item_application')),
    (r'create:millstone', ('create:milling',)),
    (r'create(addition)?:\w*sand_?paper', ('create:sandpaper_polishing',)),
    (r'createaddition:tesla_coil', ('createaddition:charging',)),
    (r'createbigcannons:basin_foundry_lid', ('createbigcannons:melting',)),
    (r'botania:\w*mana_pool|appbot:fluix_mana_pool', ('botania:mana_infusion',)),
    (r'botania:alfheim_portal', ('botania:elven_trade',)),
    (r'botania:terra_plate', ('botania:terrestrial_agglomeration_plate',)),
    (r'astralsorcery:altar_\w+', ('astralsorcery:altar_crafting',)),
    (r'enderio:slice_and_splice', ('enderio:slicing',)),
    (r'enderio:vat', ('enderio:vat_fermenting',)),
    (r'minecraft:furnace', ('minecraft:smelting',)),
    (r'minecraft:blast_furnace', ('minecraft:blasting',)),
    (r'minecraft:smoker', ('minecraft:smoking',)),
    (r'minecraft:(soul_)?campfire', ('minecraft:campfire_cooking',)),
    (r'minecraft:stonecutter', ('minecraft:stonecutting',)),
    (r'minecraft:smithing_table', ('minecraft:smithing_transform', 'minecraft:smithing_trim')),
    (r'minecraft:crafting_table', ('minecraft:crafting_shaped', 'minecraft:crafting_shapeless')),
]
# What players call them besides the game's names: 辊压机 is the old name of 动力冲压机.
MACHINE_ALIASES = {'辊压机': 'create:mechanical_press', '动力辊压机': 'create:mechanical_press',
                   '冲压机': 'create:mechanical_press', '搅拌机': 'create:mechanical_mixer'}
# Add-ons whose blocks run the parent mod's types: createcasing's brass mixer runs create:mixing,
# extendedae's ex_charger runs ae2:charger.
_NS_FAMILY = {'createcasing': 'create', 'extendedae': 'ae2', 'enderio_evolution': 'enderio'}
# Words naming the recipe class, not the machine: ae2cs:circuit_etcher_recipe_serializer.
_TYPE_NOISE = frozenset({'recipe', 'recipes', 'serializer', 'serializers', 'type'})


# ===== recipes =====

def load_index(path):
    path = Path(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    hit = _INDEX_CACHE.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        index = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    _INDEX_CACHE[str(path)] = (mtime, index)
    return index


def _label(index, item):
    names = (index.get('names') or {}).get(item) or {}
    return names.get('zh') or names.get('en') or item


def _ingredient_label(index, value):
    parts = [part.strip() for part in str(value or '').split(' | ') if part.strip()]
    labels = [part if part.startswith('#') else _label(index, part) for part in parts[:3]]
    return '/'.join(labels) + ('…' if len(parts) > 3 else '')


def _matching_items(index, query):
    """(exact, partial) item IDs whose ID or Chinese/English name matches the query."""
    needle = re.sub(r'\s+', ' ', str(query or '')).strip().casefold()
    if not needle:
        return [], []
    exact, partial = [], []
    for item, names in (index.get('names') or {}).items():
        names = names if isinstance(names, dict) else {}
        values = [item.casefold(), str(names.get('zh') or '').casefold(), str(names.get('en') or '').casefold()]
        if needle in values or item.casefold().endswith(':' + needle):
            exact.append(item)
        elif any(needle in value for value in values if value):
            partial.append(item)
    return sorted(exact, key=lambda item: (not item.startswith('minecraft:'), len(item))), sorted(partial, key=len)


def _tag_holds(tag, item):
    """Whether a common material tag names this item: #c:gems/diamond holds minecraft:diamond,
    #c:ingots/iron holds iron_ingot, #c:storage_blocks/iron holds iron_block (and not the gem)."""
    body = tag.lstrip('#').split(':', 1)[-1]
    if '/' not in body:
        return False
    category, material = body.rsplit('/', 1)
    category = category.rsplit('/', 1)[-1]
    one = {'storage_blocks': 'block', 'raw_materials': 'raw'}.get(category, category[:-1] if category.endswith('s') else category)
    path = item.split(':', 1)[-1]
    forms = {f'{material}_{one}', f'{one}_{material}'}
    if category == 'gems':
        forms.add(material)
    return path in forms


def _stem(word):
    """mixer/mixing -> mix, smelter/smelting -> smelt, infuser/infusion -> infus; press stays press."""
    for suffix in ('ing', 'ion', 'er', 'or', 's'):
        cut = len(word) - len(suffix)
        if word.endswith(suffix) and cut >= (4 if suffix == 'ion' else 3) and not (suffix == 's' and word.endswith('ss')):
            return word[:cut]
    return word


def _stems(name, noise=frozenset()):
    return {_stem(word) for word in re.split(r'[\s_/.\-]+', str(name).split(':', 1)[-1].casefold())
            if word and word not in noise}


def _type_key(kind):
    """Datapacks may write "blasting" for minecraft:blasting."""
    kind = str(kind or '')
    return kind if not kind or ':' in kind else 'minecraft:' + kind


def _family(namespace):
    return _NS_FAMILY.get(namespace, namespace)


def _machine_types(item, kinds):
    """Recipe types this block runs (kinds: type -> word stems): the list above, then shared words.
    Its own mod's type may be one word of the ID (create:mechanical_mixer runs create:mixing);
    another mod's needs every word of a longer name (enderio_evolution's basic_sag_mill runs
    enderio:sag_milling): one shared word is not enough, so a 高炉 is not thermal:furnace and AE2's
    充能器 is not createaddition:charging. Vanilla blocks only run the vanilla types listed."""
    found = [kind for pattern, listed in MACHINE_TYPES if re.fullmatch(pattern, item)
             for kind in listed if kind in kinds]
    namespace = item.split(':', 1)[0] if ':' in item else ''
    if namespace == 'minecraft':
        return found
    words = _stems(item)
    for kind, need in sorted(kinds.items()):
        if not need or kind in found or kind.startswith('minecraft:'):
            continue
        same = bool(namespace) and _family(namespace) == _family(kind.split(':', 1)[0])
        if need <= words and (same or len(need) > 1):
            found.append(kind)
    return found


def _interleave(groups):
    """One row of each type in turn, so 石磨 shows Create's and the kitchen mods' millstones."""
    rows = []
    for i in range(max((len(group) for group in groups), default=0)):
        rows += [group[i] for group in groups if i < len(group)]
    return rows


def _format_row(index, row, machines=None):
    head = f'{_label(index, row.get("result"))} ×{row.get("count", 1)}（{row.get("result")}）'
    key = _type_key(row.get('type'))
    kind = TYPE_LABELS.get(key) or (f'{machines[key]}（{key}）' if machines and key in machines else row.get('type'))
    parts = [head, f'方式：{kind}']
    if row.get('pattern'):
        key = {k: _ingredient_label(index, v) for k, v in (row.get('key') or {}).items()}
        parts.append('形状：' + ' / '.join(row['pattern']) + '，' + '，'.join(f'{k}={v}' for k, v in key.items()))
    elif row.get('ingredients'):
        parts.append('材料：' + '、'.join(_ingredient_label(index, item) for item in row['ingredients'][:12]))
    if row.get('heat') in HEAT_TEXT:
        parts.append(HEAT_TEXT[row['heat']])
    if row.get('chance'):
        parts.append(f'产出概率 {row["chance"] * 100:g}%')
    if row.get('outputs'):
        parts.append('副产物：' + '、'.join(
            f'{_label(index, out[0])} ×{out[1]}' + (f'（{out[2] * 100:g}%）' if len(out) > 2 else '')
            for out in row['outputs'][:5]))
    parts.append(f'来自 {row.get("source")}')
    return '；'.join(parts)


def _kubejs_mentions(kubejs_dir, items):
    """Server scripts naming any of these item IDs: script changes are not in the index."""
    found = {}
    root = Path(kubejs_dir) if kubejs_dir else None
    if not root or not root.is_dir() or not items:
        return found
    pattern = re.compile('|'.join(re.escape(item) for item in items[:6]))
    for path in root.rglob('*.js'):
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        hits = len(pattern.findall(text))
        if hits:
            found[path.name] = hits
    return found


def recipe_lookup(index_path, query, mode='make', kubejs_dir=None, limit=5, names=None):
    """names: item names with the vanilla ones filled in (world_records.item_names); the index itself
    lists vanilla items with empty names, so "钻石" alone would not find minecraft:diamond."""
    index = load_index(index_path)
    if not index or not index.get('recipes'):
        return '本服配方索引还没生成，稍后再查。'
    if names:
        index = dict(index, names=names)
    query = str(query or '').strip()
    if not query:
        return '要查哪个物品的配方？给个名字或物品 ID。'
    exact, partial = _matching_items(index, query)
    items = exact + partial
    # "钻石" is the gem, not every 钻石剑: an exact name wins over names that only contain it.
    focus = exact or partial
    recipes = [row for row in index['recipes'] if row.get('type') not in SKIP_TYPES]
    tag_uses = 0
    if mode == 'uses':
        wanted = set(focus[:5])
        tags = {}

        def uses(part):
            if part in wanted:
                return 'item'
            if part.startswith('#'):
                if part not in tags:
                    tags[part] = any(_tag_holds(part, item) for item in wanted)
                return 'tag' if tags[part] else ''
            return ''
        rows = []
        for row in recipes:
            kinds = {uses(part.strip()) for value in list(row.get('ingredients') or []) + list((row.get('key') or {}).values())
                     for part in str(value).split(' | ')}
            if 'item' in kinds or 'tag' in kinds:
                rows.append(row)
                tag_uses += 'item' not in kinds
        title = f'用到「{_label(index, focus[0]) if focus else query}」的配方'
    elif mode == 'machine':
        groups = {}
        for row in recipes:
            groups.setdefault(_type_key(row.get('type')), []).append(row)
        kinds = {kind: _stems(kind, _TYPE_NOISE) for kind in groups if kind}
        alias = MACHINE_ALIASES.get(re.sub(r'\s+', '', query).casefold())
        # 搅拌器 is the whisk (an ingredient); only when the exact names run nothing do the longer
        # names (动力搅拌器, 黄铜动力搅拌器) count.
        types, machine_of = [], {}
        for group in (([alias] if alias else []) + exact, partial[:8]):
            for item in group:
                for kind in _machine_types(item, kinds):
                    machine_of.setdefault(kind, item)
                    if kind not in types:
                        types.append(kind)
            if types:
                break
        if not types:
            words = _stems(query)
            types = [kind for kind, need in sorted(kinds.items()) if need and not kind.startswith('minecraft:')
                     and (need == words or (len(need) > 1 and need <= words))]
        machines = {kind: _label(index, item) for kind, item in machine_of.items()}
        # The machine's own mod's products first: the 10-28 replay of 搅拌器 opened with another mod's
        # Time Steel instead of brass or andesite alloy.
        rows = _interleave([sorted(groups[kind], key=lambda row, own=kind.split(':', 1)[0]: (
            {own: 0, 'minecraft': 1}.get(str(row.get('result') or '').split(':', 1)[0], 2))) for kind in types])
        named = '、'.join(f'{kind}（{TYPE_LABELS.get(kind) or machines.get(kind) or kind}）{len(groups[kind])} 条'
                         for kind in types[:6]) + ('等' if len(types) > 6 else '')
        title = f'「{query}」能做的配方（配方类型：{named or "没对上"}）'
    else:
        wanted = focus[:5]
        rows = [row for row in recipes if row.get('result') in wanted]
        rows.sort(key=lambda row: (wanted.index(row['result']), row.get('type') != 'minecraft:crafting_shaped'))
        title = f'「{_label(index, wanted[0]) if len(wanted) == 1 else query}」的配方'
    vanilla = (any(kind.startswith('minecraft:') for kind in types) if mode == 'machine'
               else any(item.startswith('minecraft:') for item in focus[:5]))
    vanilla_note = ('原版自己的配方（工作台、熔炉等）不在这份索引里，这里只有模组和数据包加的；原版怎么合成按原版来。'
                    if vanilla else '')
    if not rows:
        hint = ('、'.join(f'{_label(index, item)}（{item}）' for item in items[:5]) if items else '')
        return (f'本服配方索引里没找到{title}。' + (f'名字对得上的物品有：{hint}。' if hint else '')
                + (vanilla_note or '可能是名字不对、是战利品或世界生成，或者由 KubeJS 脚本另加的。'))
    lines = [f'{title}：共 {len(rows)} 条' + (f'（其中 {tag_uses} 条是通过矿辞标签用到的）' if tag_uses else '')
             + f'，列出前 {min(limit, len(rows))} 条']
    lines += [f'{i}. {_format_row(index, row, machines if mode == "machine" else None)}'
              for i, row in enumerate(rows[:limit], 1)]
    mentions = _kubejs_mentions(kubejs_dir, sorted({row['result'] for row in rows[:limit]}) if mode != 'uses' else items[:3])
    if mentions:
        lines.append('注意：KubeJS 脚本里提到了它（' + '、'.join(f'{name} {n} 处' for name, n in sorted(mentions.items())[:4])
                     + '），配方可能被脚本改过或删掉，以游戏里 JEI 为准。')
    if vanilla_note:
        lines.append(vanilla_note)
    lines.append(f'索引生成于 {datetime.fromtimestamp(index.get("generated_at") or 0, TZ):%m-%d %H:%M}，来自模组和数据包里的配方文件；'
                 '要看配方图可以发 !配方 物品名。')
    return '\n'.join(lines)


# ===== mods =====

def _json(url, headers=None, timeout=10, direct=False):
    request = urllib.request.Request(url, headers=dict({'User-Agent': UA}, **(headers or {})))
    opener = _DIRECT if direct else urllib.request.build_opener()
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8', errors='replace'))


def _installed(inventory, keyword):
    """Inventory rows naming the keyword; "Create" itself before 0World2Create and Applied Create."""
    needle = keyword.casefold()
    rows = []
    for row in (inventory or {}).get('mods') or []:
        haystack = ' '.join([str(row.get('name') or ''), str(row.get('file') or ''), ' '.join(map(str, row.get('ids') or []))]).casefold()
        if needle in haystack:
            rows.append(row)
    exact = lambda row: (str(row.get('name') or '').casefold() == needle
                         or needle in [str(value).casefold() for value in row.get('ids') or []])
    return sorted(rows, key=lambda row: (not exact(row), len(str(row.get('name') or ''))))


def _same_mod(a, b):
    """CurseForge's search ranks by popularity: "Create" brings back Xaero's Minimap."""
    a, b = (re.sub(r'[^0-9a-z]+', '', str(value or '').casefold()) for value in (a, b))
    return bool(a and b and (a in b or b in a))


def _clean_intro(text):
    text = re.sub(r'\[h\d=[^\]]*\]|←\s*竖排目录，以获得最佳阅读体验', ' ', str(text or ''))
    return re.sub(r'\s+', ' ', text).strip()


def _alt_names(title):
    """English names in a MC 百科 title: "[AE2]应用能源2 (Applied Energistics 2)" -> AE2, Applied Energistics 2."""
    values = re.findall(r'[（(]([^()（）]+)[)）]', str(title or '')) + re.findall(r'\[([^\]]+)\]', str(title or ''))
    return [value.strip() for value in values if value.strip() and re.search(r'[A-Za-z]', value) and value.isascii()]


def mod_info(keyword, inventory=None, *, modrinth=None, curseforge=None, wiki=None, versions=None):
    """Text about one mod; each source is optional and failures are named instead of hidden.

    A Chinese name ("应用能源") finds nothing on Modrinth: MC 百科 is asked first and the English
    name in its title is what Modrinth, CurseForge and the installed-mod check are asked with.
    """
    keyword = str(keyword or '').strip()[:60]
    if not keyword:
        return '要查哪个模组？给个名字。'
    lines, notes = [], []
    page = None
    if wiki:
        try:
            page = wiki(keyword)
        except Exception as exc:
            notes.append(f'MC 百科查询失败（{type(exc).__name__}）')
    alternatives = _alt_names(page[0]) if page else []
    chinese = bool(re.search(r'[\u4e00-\u9fff]', keyword))
    terms = list(dict.fromkeys((alternatives + [keyword]) if chinese else ([keyword] + alternatives)))
    installed = next((rows for rows in (_installed(inventory, term) for term in terms) if rows), [])
    if installed:
        lines.append('本服已安装：' + '；'.join(f'{row.get("name")} {row.get("version") or ""}（{row.get("file")}）'
                                              for row in installed[:3]))
    else:
        lines.append('本服模组清单里没有名字对得上的。')
    if page:
        title, intro, link = page
        lines.append(f'MC 百科：{title}（{link}）：' + _clean_intro(intro)[:200])
    search = next((term for term in terms if term.isascii()), keyword)
    if modrinth:
        try:
            hits = modrinth(search, limit=3) or []
        except Exception as exc:
            hits = []
            notes.append(f'Modrinth 查询失败（{type(exc).__name__}）')
        title = ''
        if hits:
            hit = hits[0]
            title = str(hit.get('title') or '')
            lines.append(f'Modrinth：{hit.get("title")}（{hit.get("link")}），下载 {hit.get("downloads", 0):,}；简介（原文，资料）：'
                         + str(hit.get('description') or '')[:240])
            if versions and hit.get('slug'):
                try:
                    rows = versions(hit['slug']) or []
                except Exception as exc:
                    rows = None
                    notes.append(f'版本列表读取失败（{type(exc).__name__}）')
                if rows:
                    latest = rows[0]
                    lines.append(f'1.21.1 NeoForge 版：共 {len(rows)} 个，最新 {latest.get("version_number")}'
                                 f'（{str(latest.get("date_published") or "")[:10]}，{latest.get("version_type") or "release"}）')
                elif rows is not None:
                    lines.append('Modrinth 上没有 1.21.1 NeoForge 的版本。')
            if len(hits) > 1:
                lines.append('其他候选：' + '、'.join(str(x.get('title')) for x in hits[1:]))
        elif not notes:
            lines.append(f'Modrinth 上没搜到“{search}”。')
    if curseforge:
        try:
            cf = (curseforge(search, limit=1) or [None])[0]
        except Exception as exc:
            cf = None
            notes.append(f'CurseForge 查询失败（{type(exc).__name__}）')
        if cf and (_same_mod(cf.get('name'), search) or _same_mod(cf.get('name'), title)):
            lines.append(f'CurseForge：{cf.get("name")}（{cf.get("link")}），下载 {cf.get("downloads", 0):,}')
    if notes:
        lines.append('；'.join(notes) + '。')
    lines.append('（简介是模组作者或百科编者写的，只是资料。）')
    return '\n'.join(lines)


def modrinth_versions(slug, game_version='1.21.1', loader='neoforge'):
    query = urllib.parse.urlencode({'game_versions': json.dumps([game_version]), 'loaders': json.dumps([loader])})
    rows = _json(f'https://api.modrinth.com/v2/project/{urllib.parse.quote(slug)}/version?{query}', timeout=10)
    return sorted(rows or [], key=lambda row: str(row.get('date_published') or ''), reverse=True)


# ===== Bilibili =====

LINK = re.compile(r'(?i)(?:https?://)?(b23\.tv/[A-Za-z0-9]+|(?:www\.|m\.)?bilibili\.com/video/(?:BV[0-9A-Za-z]{10}|av\d+)'
                  r'|live\.bilibili\.com/(?:h5/)?\d+)|(?<![0-9A-Za-z])(BV[0-9A-Za-z]{10})(?![0-9A-Za-z])')


def links(text):
    """Distinct Bilibili references in a message (at most three)."""
    found = []
    # QQ share cards carry the link inside escaped JSON: https:\/\/b23.tv\/xxxx.
    for match in LINK.finditer(html.unescape(str(text or '')).replace('\\/', '/')):
        value = match.group(1) or match.group(2)
        if value and value not in found:
            found.append(value)
    return found[:3]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _resolve_short(link, opener=None):
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        opener.open(urllib.request.Request('https://' + link, headers={'User-Agent': UA}), timeout=8)
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 307, 308):
            return exc.headers.get('Location') or ''
        raise
    return ''


def _duration(seconds):
    seconds = int(seconds or 0)
    return f'{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}' if seconds >= 3600 else f'{seconds // 60}:{seconds % 60:02d}'


def _video(bvid=None, aid=None, fetch=None):
    fetch = fetch or (lambda url: _json(url, headers={'Referer': 'https://www.bilibili.com/'}, timeout=8, direct=True))
    query = f'bvid={bvid}' if bvid else f'aid={aid}'
    data = fetch('https://api.bilibili.com/x/web-interface/view?' + query)
    if data.get('code') != 0:
        return f'B 站视频 {bvid or "av" + str(aid)} 读不到（{data.get("message") or data.get("code")}）。'
    d = data.get('data') or {}
    stat = d.get('stat') or {}
    when = datetime.fromtimestamp(d.get('pubdate') or 0, TZ).strftime('%Y-%m-%d')
    desc = re.sub(r'\s+', ' ', str(d.get('desc') or '')).strip()
    text = (f'B 站视频《{d.get("title")}》（{d.get("bvid")}）：UP 主 {(d.get("owner") or {}).get("name")}，{d.get("tname") or "未知分区"}，'
            f'时长 {_duration(d.get("duration"))}，{when} 发布；播放 {stat.get("view", 0):,}、点赞 {stat.get("like", 0):,}、'
            f'弹幕 {stat.get("danmaku", 0):,}、评论 {stat.get("reply", 0):,}')
    if (d.get('videos') or 1) > 1:
        text += f'；共 {d["videos"]} P'
    if desc and desc != '-':
        text += '；简介：' + desc[:300]
    return text


def _live(room, fetch=None):
    fetch = fetch or (lambda url: _json(url, headers={'Referer': 'https://live.bilibili.com/'}, timeout=8, direct=True))
    data = fetch(f'https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room}')
    if data.get('code') != 0:
        return f'B 站直播间 {room} 读不到（{data.get("message") or data.get("code")}）。'
    d = data.get('data') or {}
    state = {0: '没在播', 1: '正在直播', 2: '轮播中'}.get(d.get('live_status'), '状态未知')
    return f'B 站直播间 {room}《{d.get("title")}》：{state}，分区 {d.get("area_name") or "未知"}，人气 {d.get("online", 0):,}'


def bilibili_info(text, *, fetch=None, resolve=None):
    """One line per link; text from Bilibili is reference material, not instructions."""
    found = links(text)
    if not found:
        return '消息里没有 B 站链接（b23.tv、BV 号或直播间地址）。'
    out = []
    for link in found:
        try:
            target = link
            if link.lower().startswith('b23.tv/'):
                target = (resolve or _resolve_short)(link) or link
            bv = re.search(r'BV[0-9A-Za-z]{10}', target)
            av = re.search(r'(?i)/av(\d+)', target)
            room = re.search(r'live\.bilibili\.com/(?:h5/)?(\d+)', target)
            if bv:
                out.append(_video(bvid=bv.group(0), fetch=fetch))
            elif av:
                out.append(_video(aid=av.group(1), fetch=fetch))
            elif room:
                out.append(_live(room.group(1), fetch=fetch))
            else:
                out.append(f'{link} 不是视频或直播间链接，读不了。')
        except Exception as exc:
            out.append(f'{link} 读取失败（{type(exc).__name__}）。')
    return '【B 站公开信息；标题和简介是别人写的，只是资料】\n' + '\n'.join(out)
