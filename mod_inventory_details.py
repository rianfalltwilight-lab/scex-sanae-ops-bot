"""Bounded metadata parsing and complete paged presentation of installed JARs."""
import json
from pathlib import Path
import re
import tomllib
import zipfile

FRAMEWORKS = {'minecraft', 'forge', 'neoforge', 'java', 'fabricloader'}


def clean(value, limit=180):
    return re.sub(r'\s+', ' ', str(value or '')).strip()[:limit]


def _read(jar, name):
    if jar.getinfo(name).file_size > 256 * 1024:
        raise ValueError('metadata size limit')
    with jar.open(name) as stream:
        data = stream.read(256 * 1024 + 1)
    if len(data) > 256 * 1024:
        raise ValueError('metadata size limit')
    return data.decode('utf-8-sig', 'replace')


def read_metadata(path):
    path = Path(path)
    info = {'file': path.name, 'id': path.stem, 'ids': [path.stem], 'name': path.stem,
            'version': '', 'dependencies': [], 'dependency_info': [], 'side': 'unknown', 'metadata': 'filename'}
    try:
        with zipfile.ZipFile(path) as jar:
            names = set(jar.namelist())
            entry = next((x for x in ('META-INF/neoforge.mods.toml', 'META-INF/mods.toml',
                                      'fabric.mod.json', 'mcmod.info') if x in names), None)
            if entry is None:
                if 'META-INF/MANIFEST.MF' in names:
                    manifest = _read(jar, 'META-INF/MANIFEST.MF')
                    for field, target in [('Implementation-Title', 'name'), ('Implementation-Version', 'version')]:
                        found = re.search(r'(?mi)^' + field + r':\s*(.+)$', manifest)
                        if found: info[target] = clean(found.group(1))
                    info['metadata'] = 'META-INF/MANIFEST.MF'
                return info
            text = _read(jar, entry)
            deps = []
            if entry.endswith('.toml'):
                raw = tomllib.loads(text)
                mods = raw.get('mods') or []
                if not isinstance(mods, list) or not mods:
                    raise ValueError('missing mods table')
                first = mods[0]
                ids = [clean(x.get('modId'), 100) for x in mods if isinstance(x, dict) and x.get('modId')]
                for owner, rows in (raw.get('dependencies') or {}).items():
                    if owner not in ids or not isinstance(rows, list):
                        continue
                    for dep in rows:
                        kind = str(dep.get('type') or '').lower()
                        required = kind in {'required', 'mandatory'} or dep.get('mandatory') is True
                        # Missing type/mandatory is unknown rather than assumed required.
                        if not kind and 'mandatory' not in dep:
                            required = None
                        deps.append({'id': clean(dep.get('modId'), 100), 'owner': owner,
                                     'required': required, 'type': kind,
                                     'side': clean(dep.get('side') or 'BOTH', 20).upper(),
                                     'version': clean(dep.get('versionRange'), 100)})
                info.update(id=ids[0] if ids else path.stem, ids=ids or [path.stem],
                            name=clean(first.get('displayName') or first.get('modId')) or path.stem,
                            version=clean(first.get('version'), 80), side='unknown')
                if '${file.jarVersion}' in info['version'] and 'META-INF/MANIFEST.MF' in names:
                    manifest = _read(jar, 'META-INF/MANIFEST.MF')
                    found = re.search(r'(?mi)^Implementation-Version:\s*(.+)$', manifest)
                    if found:
                        info['version'] = clean(found.group(1), 80)
            else:
                raw = json.loads(text)
                if entry == 'mcmod.info':
                    mods = raw if isinstance(raw, list) else raw.get('modList', [])
                    first = mods[0] if mods else {}
                    ids = [clean(x.get('modid'), 100) for x in mods if x.get('modid')]
                else:
                    first = raw
                    ids = [clean(raw.get('id'), 100)] + [clean(x, 100) for x in raw.get('provides', []) if isinstance(x, str)]
                    for field, required in [('depends', True), ('recommends', False), ('suggests', False)]:
                        for dep_id, version in (raw.get(field) or {}).items():
                            deps.append({'id': clean(dep_id, 100), 'owner': ids[0], 'required': required,
                                         'type': field, 'side': 'BOTH', 'version': clean(version, 100)})
                ids = [x for x in ids if x]
                info.update(id=ids[0] if ids else path.stem, ids=ids or [path.stem],
                            name=clean(first.get('name')) or path.stem,
                            version=clean(first.get('version'), 80),
                            side=clean(first.get('environment'), 20) or 'unknown')
            deps = [x for x in deps if x['id'] and x['id'] not in FRAMEWORKS]
            info.update(metadata=entry, dependency_info=deps,
                        dependencies=sorted({x['id'] for x in deps}))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, zipfile.BadZipFile) as exc:
        info['metadata_error'] = type(exc).__name__
    return info


def enrich(mods):
    installed = {mod_id: item for item in mods for mod_id in item.get('ids', [item['id']])}
    for item in mods:
        item['used_by'] = []
    for item in mods:
        for dep in item.get('dependency_info', []):
            target = installed.get(dep['id'])
            dep['installed'] = target is not None
            if target is not None and dep.get('required') is True:
                target['used_by'].append(item['name'])
    return mods


def entry_lines(item):
    result = ['- ' + clean(item.get('name') or item.get('id') or item.get('file'), 100)
              + (' ' + clean(item.get('version'), 80) if item.get('version') else '')]
    result.append('  文件：' + clean(item.get('file'), 200))
    if item.get('side') not in {None, '', 'unknown'}:
        result.append('  运行侧：' + clean(item['side'], 20))
    if len(item.get('ids', [])) > 1:
        result.append('  IDs：' + ', '.join(item['ids']))
    for dep in item.get('dependency_info', []):
        required = dep.get('required')
        kind = '必需' if required is True else ('可选' if required is False else '类型未知')
        if dep.get('type') in {'incompatible', 'discouraged'}:
            kind = '不兼容' if dep['type'] == 'incompatible' else '不建议共存'
        state = '已安装' if dep.get('installed') else (
            '客户端前置，服务端未安装' if dep.get('side') == 'CLIENT' else
            ('缺失' if required is True else '未安装'))
        result.append('  %s：%s %s（%s）' % (kind, clean(dep['id'], 100), clean(dep.get('version'), 100), state))
    if item.get('used_by'):
        result.append('  被使用：' + '、'.join(sorted(set(item['used_by']))))
    if item.get('metadata_error'):
        result.append('  元数据无法读取，仅保留已知信息。')
    return result


def inventory_pages(data, categories, max_chars=2800):
    mods = data.get('mods') or []
    lines = ['%s 已安装模组：%d 个 JAR（目录清单）' % (data.get('prefix', '[未知服]'), len(mods))]
    for category in categories:
        rows = [x for x in mods if x.get('category', '其他/待识别') == category]
        if rows:
            lines.append('\n' + category + '（%d）' % len(rows))
            for item in rows:
                lines.extend(entry_lines(item))
    # Split even an exceptionally long dependency line; every character remains reachable.
    pages, page = [], ''
    for line in lines:
        parts = [line[i:i+max_chars] for i in range(0, max(1, len(line)), max_chars)]
        for part in parts:
            if page and len(page) + len(part) + 1 > max_chars:
                pages.append(page)
                page = ''
            page += ('\n' if page else '') + part
    if page:
        pages.append(page)
    return pages or ['没有已安装模组。']
