#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Localize advancement notifications from installed server resources.

Adapted for the dual-server bridge from minecraft-server-ops-kit's
``discord-watch.ps1`` language-pairing design. See ``UPSTREAM-NOTICE.md``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
import zipfile
from pathlib import Path


_ADVANCEMENT_KEY = re.compile(r"(^|\.)(?:adv|advancements?|achievement)\.", re.I)
_DESCRIPTION_SUFFIX = re.compile(r"\.(?:description|desc)$", re.I)
_COLOR = re.compile(r"(?:\u00a7|&)[0-9A-FK-OR]", re.I)
_LANG_PATH = re.compile(r"(?:^|/)assets/[^/]+/lang/(en_us|zh_cn)\.json$", re.I)
_ADVANCEMENT_PATH = re.compile(r"(?:^|/)data/[^/]+/advancements?/.+\.json$", re.I)
_PACKAGED_LANGUAGE_ROOT = Path(__file__).with_name("resources")
_CJK = re.compile(r"[\u3400-\u9fff]")

# Upstream's built-in vanilla fallback is needed on dedicated servers because
# the official zh_cn client asset is normally absent there. Installed resource
# translations still override these values.
BUILTIN_TITLES = {
    "Minecraft": "Minecraft", "Stone Age": "石器时代", "Getting an Upgrade": "获得升级",
    "Acquire Hardware": "来硬的", "Suit Up": "整装上阵", "Hot Stuff": "热腾腾的东西",
    "Isn't It Iron Pick": "这不是铁镐么", "Not Today, Thank You": "今天不行，谢谢",
    "Ice Bucket Challenge": "冰桶挑战", "Diamonds!": "钻石！",
    "We Need to Go Deeper": "我们需要再深入些", "Cover Me With Diamonds": "钻石护体",
    "Enchanter": "附魔师", "Zombie Doctor": "僵尸科医生", "Eye Spy": "隔墙有眼",
    "The End?": "结束了？", "Nether": "下界", "Return to Sender": "见鬼去吧",
    "Those Were the Days": "那些年的日子", "Hidden in the Depths": "深藏不露",
    "Subspace Bubble": "子空间泡泡", "A Terrible Fortress": "阴森的要塞",
    "Who is Cutting Onions?": "谁在切洋葱？", "Oh Shiny": "哦，闪亮亮！",
    "This Boat Has Legs": "这船有腿", "Uneasy Alliance": "不稳定的同盟",
    "War Pigs": "战猪", "Country Lode, Take Me Home": "脉络带我回家",
    "Cover Me in Debris": "残骸裹身", "Spooky Scary Skeleton": "诡异又可怕的骷髅",
    "Into Fire": "与火共舞", "Not Quite \"Nine\" Lives": "还没到九条命",
    "Hot Tourist Destinations": "热门景点", "Withering Heights": "凋零山庄",
    "Local Brewery": "本地的酿造厂", "Bring Home the Beacon": "带信标回家",
    "A Furious Cocktail": "狂乱的鸡尾酒", "Beaconator": "信标工程师",
    "How Did We Get Here?": "为什么会变成这样呢？", "The End": "末地",
    "Free the End": "解放末地", "The Next Generation": "下一世代",
    "Remote Getaway": "远程折跃", "The End... Again...": "结束了……再一次……",
    "You Need a Mint": "你需要来点薄荷", "The City at the End of the Game": "游戏尽头的城市",
    "Sky's the Limit": "天空即为极限", "Great View From Up Here": "这上面的风景真不错",
    "Adventure": "冒险", "Voluntary Exile": "自我放逐", "Is It a Bird?": "那是鸟吗？",
    "Monster Hunter": "怪物猎人", "What a Deal!": "这交易不错！",
    "Sticky Situation": "胶着状态", "Ol' Betsy": "老贝琪", "Sweet Dreams": "甜蜜的梦",
    "Hero of the Village": "村庄英雄", "Is It a Balloon?": "那是气球吗？",
    "A Throwaway Joke": "轻飘飘的笑话", "Take Aim": "瞄准目标",
    "Monsters Hunted": "怪物狩猎完成", "Postmortal": "超越生死",
    "Hired Help": "招兵买马", "Star Trader": "星际商人",
    "Two Birds, One Arrow": "一箭双雕", "Who's the Pillager Now?": "现在谁才是掠夺者？",
    "Arbalistic": "劲弩手", "Adventuring Time": "探索的时光",
    "Sound of Music": "音乐之声", "Light as a Rabbit": "轻盈如兔",
    "Is It a Plane?": "那是飞机吗？", "Very Very Frightening": "非常非常可怕",
    "Sniper Duel": "狙击手的对决", "Bullseye": "正中靶心", "Husbandry": "农牧业",
    "Bee Our Guest": "宾至如归", "The Parrots and the Bats": "我从哪儿来？",
    "Best Friends Forever": "永远的好朋友", "Fishy Business": "腥味十足的生意",
    "Total Beelocation": "举巢搬迁", "A Seedy Place": "播种之地", "Two by Two": "成双成对",
    "A Complete Catalogue": "完整的目录", "Tactical Fishing": "战术性钓鱼",
    "A Balanced Diet": "均衡饮食", "Serious Dedication": "终极奉献",
    "Bukkit Bukkit": "桶桶桶桶", "Wax On": "涂蜡", "Wax Off": "脱蜡",
    "The Cutest Predator": "最萌捕食者", "The Healing Power of Friendship!": "友谊的治愈力！",
    "Glow and Behold!": "眼前一亮！", "Whatever Floats Your Goat!": "羊帆起航！",
    "Birthday Song": "生日歌", "You've Got a Friend in Me": "你有我这个朋友",
    "With Our Powers Combined!": "我们聚力同行！", "Planting the Past": "种下过去",
    "Careful Restoration": "小心修复", "Crafting a New Look": "打造新外观",
    "Smithing with Style": "锻造有型", "The Power of Books": "书籍的力量",
    "It Spreads": "它在蔓延", "Minecraft: Trial(s) Edition": "Minecraft：试炼版",
    "Sneak 100": "潜行 100", "Surge Protector": "避雷针",
    "Under Lock and Key": "珍藏密敛", "Revaulting": "宝经磨炼", "Lighten Up": "铜光焕发",
    "Over-Overkill": "天赐良击", "Who Needs Rockets?": "还要啥火箭啊？",
    "Crafters Crafting Crafters": "合成器合成合成器",
}

BUILTIN_DESCRIPTIONS = {
    "Sweet Dreams": "在床上睡觉以改变你的重生点",
    "Cover Me With Diamonds": "钻石盔甲能救人",
    "The Parrots and the Bats": "繁殖一对动物",
    "Getting an Upgrade": "制作一把比木镐更好的镐",
    "Isn't It Iron Pick": "获得一把铁镐",
    "Enchanter": "在附魔台上附魔一件物品",
    "Into Fire": "获得一根烈焰棒",
}


def _clean_title(value):
    value = _COLOR.sub("", str(value or ""))
    return re.sub(r"[ \t]{2,}", " ", value).strip()


def _lookup_key(value):
    """Normalize harmless display differences before title lookup.

    Server log titles are display text, not translation keys.  Mods and even
    vanilla releases sometimes change only capitalization or punctuation, so
    exact dictionary lookup is too brittle for notifications.
    """
    value = unicodedata.normalize("NFKC", _clean_title(value))
    value = (value.replace("’", "'").replace("‘", "'")
             .replace("…", "...").replace("–", "-").replace("—", "-"))
    return re.sub(r"\s+", " ", value).strip().casefold()


def _has_cjk(value):
    return bool(_CJK.search(str(value or "")))


def _normalized_index(mapping):
    """Build a normalized alias index, preferring localized Chinese values."""
    result = {}
    for raw, value in mapping.items():
        key = _lookup_key(raw)
        clean_value = _clean_title(value)
        if not key or not clean_value:
            continue
        current = result.get(key)
        if current is None or (_has_cjk(clean_value) and not _has_cjk(current)):
            result[key] = clean_value
    return result


def _clean_description(value):
    value = _COLOR.sub("", str(value or ""))
    value = re.sub(r"%(?:\d+\$)?[a-zA-Z]", "…", value)
    value = re.sub(r"[\r\n]+", "；", value)
    value = re.sub(r"[ \t]{2,}", " ", value).strip(" ；")
    return value[:180]


def _is_advancement_key(key):
    return bool(_ADVANCEMENT_KEY.search(str(key or ""))) and ".config." not in str(key).casefold()


def _base_key(key):
    return re.sub(r"\.(?:title|description|desc)$", "", str(key or ""), flags=re.I)


def _read_json_bytes(value, maximum):
    if len(value) > maximum:
        raise ValueError("resource json too large")
    result = json.loads(value.decode("utf-8-sig"))
    return result if isinstance(result, dict) else {}


def _translation_component(value):
    if isinstance(value, str):
        return {"text": value}
    if isinstance(value, dict):
        if value.get("translate"):
            return {"key": str(value["translate"])}
        if value.get("text"):
            return {"text": str(value["text"])}
    return {}


class AdvancementLocalizer:
    """Build and persist a bounded English→Chinese advancement index."""

    SCHEMA = 1

    def __init__(self, server_dir, cache_path, manual_titles=None, extra_roots=None):
        self.server_dir = Path(server_dir)
        self.cache_path = Path(cache_path)
        self.manual_titles = dict(manual_titles or {})
        self.extra_roots = [Path(item) for item in (extra_roots or []) if item]
        self._signature_value = None
        self._titles = {}
        self._descriptions = {}
        self._title_index = {}
        self._description_index = {}
        self._last_check = 0.0

    def _resource_files(self):
        roots = [self.server_dir / "mods", self.server_dir / "resourcepacks",
                 self.server_dir / "datapacks", self.server_dir / "world" / "datapacks",
                 self.server_dir / "kubejs", self.server_dir / "config" / "openloader",
                 self.server_dir / "config" / "global_packs",
                 self.server_dir / "config" / "yes_steve_model",
                 self.server_dir / "moonlight-global-datapacks",
                 self.server_dir / "tacz", self.server_dir / "tlm_custom_pack",
                 _PACKAGED_LANGUAGE_ROOT]
        for extra in self.extra_roots:
            roots.extend((extra / "mods", extra / "resourcepacks", extra / "datapacks",
                          extra / "kubejs", extra / "config" / "openloader",
                          extra / "config" / "global_packs", extra / "config" / "yes_steve_model",
                          extra / "moonlight-global-datapacks", extra / "tacz",
                          extra / "tlm_custom_pack"))
        archives, loose = [], []
        seen = set()
        for root in roots:
            if not root.is_dir():
                continue
            try:
                candidates = root.rglob("*")
                for path in candidates:
                    if not path.is_file():
                        continue
                    resolved = str(path)
                    if resolved in seen:
                        continue
                    suffix = path.suffix.casefold()
                    if suffix in (".jar", ".zip"):
                        archives.append(path)
                        seen.add(resolved)
                    elif path.name.casefold() in ("en_us.json", "zh_cn.json"):
                        loose.append(path)
                        seen.add(resolved)
            except OSError:
                continue
        return sorted(archives), sorted(loose)

    def _signature(self, archives, loose):
        digest = hashlib.sha256()
        for path in archives + loose:
            try:
                stat = path.stat()
            except OSError:
                continue
            digest.update(str(path).encode("utf-8", "surrogatepass"))
            digest.update(("|%d|%d;" % (stat.st_size, stat.st_mtime_ns)).encode("ascii"))
        digest.update(json.dumps(self.manual_titles, ensure_ascii=False,
                                 sort_keys=True, separators=(",", ":")).encode("utf-8"))
        return digest.hexdigest()

    @staticmethod
    def _merge_language(target, payload):
        for key, value in payload.items():
            if isinstance(value, str) and value.strip():
                target[str(key)] = value

    def _scan(self, archives, loose):
        english, chinese, pairs = {}, {}, []
        errors = 0
        for path in archives:
            try:
                with zipfile.ZipFile(path) as archive:
                    for info in archive.infolist():
                        normalized = info.filename.replace("\\", "/")
                        lang = _LANG_PATH.search(normalized)
                        try:
                            if lang:
                                if info.file_size > 16 * 1024 * 1024:
                                    raise ValueError("language json too large")
                                payload = _read_json_bytes(archive.read(info), 16 * 1024 * 1024)
                                self._merge_language(
                                    english if lang.group(1).casefold() == "en_us" else chinese,
                                    payload)
                            elif (_ADVANCEMENT_PATH.search(normalized) and
                                  info.file_size <= 2 * 1024 * 1024):
                                payload = _read_json_bytes(archive.read(info), 2 * 1024 * 1024)
                                display = payload.get("display") or {}
                                title = _translation_component(display.get("title"))
                                description = _translation_component(display.get("description"))
                                if title and description:
                                    pairs.append((title, description))
                        except (OSError, ValueError, KeyError, RuntimeError):
                            errors += 1
                            continue
            except (OSError, zipfile.BadZipFile, RuntimeError):
                errors += 1
        for path in loose:
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(payload, dict):
                    self._merge_language(english if path.name.casefold() == "en_us.json" else chinese,
                                         payload)
            except (OSError, ValueError):
                errors += 1
        return english, chinese, pairs, errors

    @staticmethod
    def _description_aliases(descriptions, aliases, value):
        desc = _clean_description(value)
        if not desc:
            return
        for alias in aliases:
            raw = _clean_title(alias)
            if raw and raw != desc:
                descriptions[raw] = desc

    def _build(self, signature, archives, loose):
        english, chinese, pairs, errors = self._scan(archives, loose)
        titles, descriptions = dict(BUILTIN_TITLES), dict(BUILTIN_DESCRIPTIONS)

        for key in set(english) | set(chinese):
            if not _is_advancement_key(key) or _DESCRIPTION_SUFFIX.search(key):
                continue
            translated = _clean_title(chinese.get(key) or english.get(key) or key)
            if not translated:
                continue
            for alias in (key, english.get(key), chinese.get(key)):
                clean = _clean_title(alias)
                if clean:
                    titles[clean] = translated

        for key in set(english) | set(chinese):
            if not _is_advancement_key(key) or not _DESCRIPTION_SUFFIX.search(key):
                continue
            value = chinese.get(key) or english.get(key)
            base = _base_key(key)
            aliases = [key, base, base + ".title"]
            for title_key in (base, base + ".title"):
                aliases.extend((english.get(title_key), chinese.get(title_key)))
            self._description_aliases(descriptions, aliases, value)

        for title_component, desc_component in pairs:
            title_key = title_component.get("key")
            desc_key = desc_component.get("key")
            raw_title = title_component.get("text") or english.get(title_key)
            translated = chinese.get(title_key) or raw_title or title_key
            if translated:
                for alias in (title_key, raw_title, chinese.get(title_key)):
                    clean = _clean_title(alias)
                    if clean:
                        titles[clean] = _clean_title(translated)
            desc = desc_component.get("text") or chinese.get(desc_key) or english.get(desc_key)
            self._description_aliases(descriptions,
                                      (title_key, raw_title, translated, chinese.get(title_key)), desc)

        # Existing hand-curated/generated map remains highest priority.
        for raw, translated in self.manual_titles.items():
            clean_raw, clean_translated = _clean_title(raw), _clean_title(translated)
            if clean_raw and clean_translated:
                titles[clean_raw] = clean_translated

        return {"schema": self.SCHEMA, "signature": signature, "generated_at": time.time(),
                "source_archives": len(archives), "source_languages": len(loose),
                "errors": errors, "titles": titles, "descriptions": descriptions}

    def _load_cache(self, signature):
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8-sig"))
            if payload.get("schema") != self.SCHEMA or payload.get("signature") != signature:
                return None
            if not isinstance(payload.get("titles"), dict) or not isinstance(payload.get("descriptions"), dict):
                return None
            return payload
        except (OSError, ValueError):
            return None

    def _save_cache(self, payload):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
        os.replace(str(temp), str(self.cache_path))

    def refresh(self, force=False):
        now = time.time()
        if not force and self._signature_value and now - self._last_check < 60:
            return
        archives, loose = self._resource_files()
        signature = self._signature(archives, loose)
        self._last_check = now
        if not force and signature == self._signature_value:
            return
        payload = None if force else self._load_cache(signature)
        if payload is None:
            payload = self._build(signature, archives, loose)
            self._save_cache(payload)
        self._signature_value = signature
        self._titles = dict(payload.get("titles") or {})
        self._descriptions = dict(payload.get("descriptions") or {})
        self._title_index = _normalized_index(self._titles)
        self._description_index = _normalized_index(self._descriptions)

    def localize(self, raw_title):
        self.refresh()
        clean = _clean_title(raw_title)
        title = self._titles.get(clean) or self._title_index.get(_lookup_key(clean)) or clean
        description = ""
        for candidate in (clean, title):
            description = (self._descriptions.get(candidate) or
                           self._description_index.get(_lookup_key(candidate)) or "")
            if description:
                break
        return title, description

    def format_event(self, prefix, player, kind, raw_title):
        title, description = self.localize(raw_title)
        verbs = {"progress": "达成进度", "goal": "达成目标", "challenge": "完成挑战"}
        display_title = title if _has_cjk(title) or title == "Minecraft" else "未收录的模组进度"
        head = "%s 玩家 %s %s【%s】" % (
            prefix, player, verbs.get(kind, verbs["progress"]), display_title)
        if description and _has_cjk(description):
            return head + "\n获取方式：" + description
        if description:
            return head + "\n获取方式：服务器资源仅提供外文条件，请在游戏内进度界面查看"
        return head + "\n获取方式：请在游戏内进度界面查看该项具体条件"
