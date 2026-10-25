"""Local SimpleBackups source and append-only NAS delivery for Sanae."""
import hashlib
import os
from pathlib import Path
import shutil

from backup_verify import verify_backup_zip


def enabled():
    return bool(os.environ.get('SANAE_BACKUP_LOCAL_DIR'))


def source(name):
    if Path(name).name != name or name in ('', '.', '..'):
        raise ValueError('Expected a backup file name')
    return Path(os.environ['SANAE_BACKUP_LOCAL_DIR']) / name


def backups():
    root = Path(os.environ['SANAE_BACKUP_LOCAL_DIR'])
    if not root.is_dir():
        raise FileNotFoundError(str(root))
    return [{'name': p.name, 'size': p.stat().st_size, 'mtime': int(p.stat().st_mtime), 'dir': False}
            for p in root.glob('*.zip') if p.is_file()]


def verify(name):
    result = verify_backup_zip(source(name))
    return dict(result, entryCount=result['entries'], regionCount=result['region_count'],
                playerdataCount=result['playerdata_count'])


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def sync(name):
    src = source(name)
    before = src.stat()
    dest_dir = Path(os.environ['SANAE_BACKUP_NAS_DIR'])
    # Read capacity on the actual share; drive mappings are session-specific.
    cursor = dest_dir
    while not cursor.exists() and cursor.parent != cursor:
        cursor = cursor.parent
    if shutil.disk_usage(cursor).free < 20 * 1024**3 + 2 * before.st_size:
        raise OSError('NAS capacity below 20 GiB + twice backup size')
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    if dest.exists():
        if dest.stat().st_size != before.st_size or digest(dest) != digest(src):
            raise FileExistsError('Existing NAS backup differs; preserved without overwrite: ' + name)
        prune(dest_dir)
        return {'state': 'success', 'file': name, 'localSize': before.st_size,
                'remoteSize': dest.stat().st_size, 'sha256': digest(src), 'destination': str(dest)}
    partial = dest_dir / (name + '.partial')
    h = hashlib.sha256()
    with src.open('rb') as inp, partial.open('wb') as out:
        for block in iter(lambda: inp.read(8 * 1024 * 1024), b''):
            h.update(block)
            out.write(block)
    after = src.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError('Source backup changed during copy')
    if partial.stat().st_size != before.st_size or digest(partial) != h.hexdigest():
        raise RuntimeError('NAS copy hash mismatch')
    # Windows rename refuses to overwrite an existing production backup.
    partial.rename(dest)
    receipt = {'state': 'success', 'file': name, 'localSize': before.st_size,
               'remoteSize': dest.stat().st_size, 'sha256': h.hexdigest(), 'destination': str(dest)}
    import json
    dest.with_suffix('.zip.sha256.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    prune(dest_dir)
    return receipt


def prune(dest_dir):
    """Rotate only verified backups when retention is explicitly configured."""
    keep = int(os.environ.get("SANAE_BACKUP_KEEP_COMPLETE", "0"))
    if keep <= 0:
        return
    import json
    root = Path(dest_dir).resolve()
    completed = []
    for archive in root.glob('world_*.zip'):
        if archive.is_symlink() or archive.resolve().parent != root:
            continue
        receipt = archive.with_suffix('.zip.sha256.json')
        if not receipt.is_file() or receipt.is_symlink():
            continue
        try:
            data = json.loads(receipt.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            continue
        if (data.get('state') == 'success' and data.get('file') == archive.name
                and data.get('remoteSize') == archive.stat().st_size
                and len(data.get('sha256', '')) == 64):
            completed.append((archive, receipt))
    completed.sort(key=lambda pair: pair[0].name, reverse=True)
    for archive, receipt in completed[keep:]:
        archive.unlink()
        receipt.unlink()


def lock(fp):
    if os.name == 'nt':
        import msvcrt
        fp.seek(0)
        try:
            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError('Backup pipeline is already running') from exc
    else:
        import fcntl
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock(fp):
    if os.name == 'nt':
        import msvcrt
        fp.seek(0)
        msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)