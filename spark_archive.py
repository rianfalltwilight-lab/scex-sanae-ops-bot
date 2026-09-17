"""Archive Spark snapshots without stopping the active profiler.

Uses the public spark-usercontent endpoint and Spark's SamplerData schema.
Only field 8 (live viewer channel_info) is removed; all sample bytes remain.
See https://spark.lucko.me/docs/misc/Raw-spark-data and SparkSamplerProtos.
"""
from __future__ import annotations
import gzip
import hashlib
import json
import os
import re
import sys
import urllib.request
import zlib
from pathlib import Path

CONTENT = 'https://spark-usercontent.lucko.me/'
VIEWER = 'https://spark.lucko.me/'
MIME = 'application/x-spark-sampler'
MAX_WIRE = 32*1024*1024
MAX_PROFILE = 256*1024*1024


def profile_key(url):
    match = re.fullmatch(r'https://spark\.lucko\.me/([A-Za-z0-9]{1,128})', str(url))
    if not match:
        raise ValueError('invalid Spark report URL')
    return match[1]


def varint(data, position):
    value = 0
    for shift in range(0, 70, 7):
        if position >= len(data):
            raise ValueError('truncated protobuf varint')
        byte = data[position]
        position += 1
        if shift == 63 and byte > 1:
            raise ValueError('protobuf varint overflow')
        value |= (byte & 127) << shift
        if byte < 128:
            return value, position
    raise ValueError('protobuf varint overflow')


def fields(data):
    data = memoryview(data)
    position = 0
    count = 0
    while position < len(data):
        start = position
        tag, position = varint(data, position)
        number, wire = tag >> 3, tag & 7
        if not 0 < number < 2**29:
            raise ValueError('invalid protobuf field')
        value_start = position
        if wire == 0:
            value, position = varint(data, position)
        elif wire in (1, 5):
            position += 8 if wire == 1 else 4
            value = data[value_start:position]
        elif wire == 2:
            size, position = varint(data, position)
            value_start = position
            position += size
            value = data[value_start:position]
        else:
            raise ValueError('unsupported protobuf wire type')
        if position > len(data):
            raise ValueError('truncated protobuf field')
        count += 1
        if count > 200000:
            raise ValueError('protobuf field bound')
        yield number, wire, value, data[start:position]


def static_profile(data):
    if not data or len(data) > MAX_PROFILE:
        raise ValueError('profile size bound')
    chunks = []
    metadata = []
    thread_count = channels = 0
    for number, wire, value, chunk in fields(data):
        if number == 1 and wire == 2:
            metadata.append(value)
        if number == 2 and wire == 2:
            thread_count += 1
        if number == 8:
            if wire != 2:
                raise ValueError('invalid channel field')
            channels += 1
        else:
            chunks.append(chunk)
    if len(metadata) != 1 or not thread_count:
        raise ValueError('not a Spark sampler profile')
    times = {n:v for n,w,v,_ in fields(metadata[0]) if n in (2,11) and w == 0}
    if times.get(2,0) <= 0 or times.get(11,0) < times.get(2,0):
        raise ValueError('invalid sample timestamps')
    return b''.join(chunks), dict(sample_start=times[2],sample_end=times[11],
                                 thread_count=thread_count,removed_channels=channels)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Spark endpoint redirected')


def decode_body(body):
    if body.startswith(b'\x1f\x8b'):
        decoder = zlib.decompressobj(16+zlib.MAX_WBITS)
        data = decoder.decompress(body, MAX_PROFILE+1)
        if len(data) > MAX_PROFILE or decoder.unconsumed_tail or decoder.unused_data or not decoder.eof:
            raise ValueError('compressed profile bound or invalid gzip')
        return data
    if len(body) > MAX_PROFILE:
        raise ValueError('profile size bound')
    return body


def download(key, opener):
    if not re.fullmatch(r'[A-Za-z0-9]{1,128}', key):
        raise ValueError('invalid content key')
    request = urllib.request.Request(CONTENT+key, headers={'User-Agent':'spark-plugin','Accept':MIME,'Accept-Encoding':'gzip'})
    with opener.open(request, timeout=10) as response:
        if response.headers.get_content_type() != MIME:
            raise ValueError('not a Spark sampler response')
        body = response.read(MAX_WIRE+1)
        if len(body) > MAX_WIRE:
            raise ValueError('download size bound')
    return decode_body(body)


def archive(url, directory, opener=None):
    key = profile_key(url)
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError('capture directory missing')
    opener = opener or urllib.request.build_opener(NoRedirect())
    data, metadata = static_profile(download(key, opener))
    compressed = gzip.compress(data, compresslevel=3, mtime=0)
    if len(compressed) > MAX_WIRE:
        raise ValueError('compressed archive size bound')
    path = directory/'profile.sparkprofile.gz'
    # Each capture has a new directory. Never overwrite an unrelated archive.
    with path.open('xb') as stream:
        stream.write(compressed)
    digest = hashlib.sha256(data).hexdigest()
    result = dict(status='saved_local',url=None,temporary=False,profile_file=str(path),
                  profile_sha256=digest,profile_bytes=len(data),archive_bytes=len(compressed),
                  archive_sha256=hashlib.sha256(compressed).hexdigest(),encoding='gzip',**metadata)
    receipt = directory/'archive-local.json'
    temp = receipt.with_suffix('.tmp')
    temp.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
    os.replace(temp,receipt)
    if metadata['removed_channels'] == 0:
        return dict(result,status='archived',url=url)
    try:
        request = urllib.request.Request(CONTENT+'post', data=compressed,
            headers={'Content-Type':MIME,'Content-Encoding':'gzip','User-Agent':'spark-plugin'}, method='POST')
        with opener.open(request, timeout=10) as response:
            posted = response.headers.get('Location','')
            if not re.fullmatch(r'[A-Za-z0-9]{1,128}', posted):
                raise ValueError('invalid posted content key')
            response.read(4096)
        if hashlib.sha256(download(posted, opener)).hexdigest() != digest:
            raise ValueError('uploaded profile hash mismatch')
        result.update(status='archived',url=VIEWER+posted)
    except Exception as exc:
        # Preserve the saved snapshot if upload or verification fails.
        result['archive_error'] = type(exc).__name__
    return result


def saved_profile(directory):
    directory = Path(directory)
    receipt = json.loads((directory/'archive-local.json').read_text(encoding='utf-8'))
    path = directory/'profile.sparkprofile.gz'
    if path.stat().st_size != receipt['archive_bytes'] or path.stat().st_size > MAX_WIRE:
        raise ValueError('archive size mismatch')
    compressed = path.read_bytes()
    if hashlib.sha256(compressed).hexdigest() != receipt['archive_sha256']:
        raise ValueError('compressed archive hash mismatch')
    data = decode_body(compressed)
    if len(data) != receipt['profile_bytes']:
        raise ValueError('profile size mismatch')
    if hashlib.sha256(data).hexdigest() != receipt['profile_sha256']:
        raise ValueError('archive hash mismatch')
    _, metadata = static_profile(data)
    if metadata['removed_channels']:
        raise ValueError('archive still has a live channel')
    return dict(receipt,status='saved_local',url=None,profile_file=str(path))


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(2)
    try:
        print(json.dumps(archive(sys.argv[1],sys.argv[2]),ensure_ascii=False))
    except Exception as exc:
        print(json.dumps(dict(status='archive_failed',url=None,archive_error=type(exc).__name__)))
        # Failure is structured output for NativeProbe; no secret-bearing traceback.
