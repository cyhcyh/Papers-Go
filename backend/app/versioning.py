"""Installed release metadata and two bounded, shared remote metadata caches."""
import asyncio
import re
import time
from collections import OrderedDict
from datetime import date, datetime, timezone
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[2]
CURRENT_VERSION = (ROOT / 'VERSION.txt').read_text(encoding='utf-8-sig').strip()
REPOSITORY = 'https://github.com/cyhcyh/Papers-Go'
REMOTE_ROOT = 'https://raw.githubusercontent.com/cyhcyh/Papers-Go/main/'
CACHE_SECONDS = 6 * 60 * 60
CACHE_LIMIT = 2
LOG_LIMIT = 10
_cache = OrderedDict()
_locks = {kind: asyncio.Lock() for kind in ('version', 'changelog')}


def version_parts(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?\d+\.\d+\.\d+', value.strip()):
        raise ValueError('版本号格式无效')
    return tuple(int(part) for part in value.strip().removeprefix('v').split('.'))


def parse_changelog(content):
    entries = []
    current, section = None, None
    headings = {'新增': 'added', '优化': 'changed', '修复': 'fixed',
                'Added': 'added', 'Changed': 'changed', 'Fixed': 'fixed'}
    for line in content.splitlines() if isinstance(content, str) else content:
        line = line.strip()
        if line.startswith('## '):
            if len(entries) >= LOG_LIMIT:
                break
            current, section = None, None
            match = re.fullmatch(r'## \[(v?\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})', line)
            if not match:
                continue
            version, stamp = match.groups()
            try:
                date.fromisoformat(stamp)
            except ValueError:
                continue
            version = version.removeprefix('v')
            if any(item['version'] == version for item in entries):
                continue
            current = {'version': version, 'date': stamp, 'added': [], 'changed': [], 'fixed': []}
            entries.append(current)
        elif line.startswith('### '):
            section = headings.get(line[4:].strip())
        elif current and section and line.startswith(('- ', '* ')):
            current[section].append(line[2:])
    return entries


with (ROOT / 'CHANGELOG').open(encoding='utf-8-sig') as _source:
    LOCAL_CHANGELOG = parse_changelog(_source)


async def _download(kind):
    filename, limit = ('VERSION.txt', 64) if kind == 'version' else ('CHANGELOG', 512 * 1024)
    async with httpx.AsyncClient(timeout=5) as client:
        async with client.stream('GET', REMOTE_ROOT + filename,
                                 headers={'Accept': 'text/plain', 'User-Agent': 'Papers-Go/' + CURRENT_VERSION}) as response:
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > limit:
                    raise ValueError('版本文件超过读取上限')
                content.extend(chunk)
    return content.decode('utf-8-sig')


async def _remote(kind, force=False):
    started = time.monotonic()
    saved = _cache.get(kind)
    if saved and not force and started - saved['cached_at'] < CACHE_SECONDS:
        return saved
    # Concurrent page loads and manual checks share the same in-flight request.
    async with _locks[kind]:
        saved = _cache.get(kind)
        if saved and (saved['cached_at'] >= started or
                      (not force and time.monotonic() - saved['cached_at'] < CACHE_SECONDS)):
            return saved
        try:
            content = await asyncio.wait_for(_download(kind), timeout=5)
            if kind == 'version':
                version_parts(content)
                value = content.strip().removeprefix('v')
            else:
                value = parse_changelog(content)
                if not value:
                    raise ValueError('更新日志格式无效')
            record = {'value': value, 'status': 'ok'}
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError, UnicodeError):
            # A failed refresh retains the last valid value in this same slot.
            record = {'value': saved['value'] if saved else None, 'status': 'failed'}
        record.update(cached_at=time.monotonic(), checked_at=datetime.now(timezone.utc).isoformat())
        _cache[kind] = record
        _cache.move_to_end(kind)
        while len(_cache) > CACHE_LIMIT:
            _cache.popitem(last=False)
        return record


def local_metadata():
    return {'current_version': CURRENT_VERSION, 'changelog': LOCAL_CHANGELOG, 'repository': REPOSITORY}


async def check_version(force=False):
    record = await _remote('version', force)
    latest = record['value']
    return {'current_version': CURRENT_VERSION, 'latest_version': latest,
            'has_update': bool(latest and version_parts(latest) > version_parts(CURRENT_VERSION)),
            'status': record['status'], 'checked_at': record['checked_at']}


async def remote_changelog(force=False):
    record = await _remote('changelog', force)
    return {'changelog': record['value'] or [], 'status': record['status'], 'checked_at': record['checked_at']}
