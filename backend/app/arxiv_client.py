"""One paced arXiv connection shared by the web and pipeline processes."""
import asyncio
import errno
import json
import os
import time
from contextlib import asynccontextmanager
from email.utils import parsedate_to_datetime

import httpx

from .config import settings
from .logs import event

INTERVAL = 3.2


def _try_lock(handle):
    handle.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as error:
        if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            raise
        return False


def _unlock(handle):
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_UN)


def _save(handle, state):
    handle.seek(0)
    handle.write(json.dumps(state).encode('ascii'))
    handle.truncate()
    handle.flush()


@asynccontextmanager
async def _slot():
    directory = settings().data_dir
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory / 'arxiv-request.lock', os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, 'r+b') as handle:
        locked = False
        try:
            while not _try_lock(handle):
                await asyncio.sleep(.1)
            locked = True
            handle.seek(0)
            try:
                state = json.loads(handle.read() or b'{}')
            except (ValueError, UnicodeDecodeError):
                state = {}
            yield handle, state
        finally:
            if locked:
                _unlock(handle)


def _retry_delay(response, attempt):
    value = response.headers.get('Retry-After', '')
    try:
        return max(0., float(value))
    except ValueError:
        try:
            return max(0., parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 30. * 2 ** attempt


async def _get(client, url, *, retries=0, **kwargs):
    for attempt in range(retries + 1):
        async with _slot() as (handle, state):
            if state.get('blocked_until', 0) > time.time():
                response = httpx.Response(403, request=httpx.Request('GET', url))
                raise httpx.HTTPStatusError('arXiv 拒绝访问，冷却后再试', request=response.request, response=response)
            await asyncio.sleep(max(0., state.get('next_request_at', 0) - time.time()))
            state['next_request_at'] = time.time() + INTERVAL
            _save(handle, state)
            response = await client.get(url, **{**kwargs, 'follow_redirects':False,
                'headers':{**kwargs.get('headers', {}), 'Connection':'close'}})
            if response.status_code in (429, 503):
                delay = max(INTERVAL, _retry_delay(response, attempt))
                state['next_request_at'] = max(state['next_request_at'], time.time() + delay)
                _save(handle, state)
                event('source', 'arXiv 要求等待，暂停后重试', job='arxiv_access', status=response.status_code,
                      wait_seconds=round(delay, 1), attempt=attempt + 1)
            elif response.status_code == 403:
                state['blocked_until'] = time.time() + 3600
                _save(handle, state)
                event('source', 'arXiv 拒绝访问，停止请求并冷却一小时', job='arxiv_access', level='error', status=403)
        if response.status_code not in (429, 503) or attempt == retries:
            if not response.is_redirect:
                response.raise_for_status()
            return response


async def get(client, url, *, retries=0, **kwargs):
    # Redirects are separate HTTP requests and must also use the shared interval.
    for _ in range(6):
        response = await _get(client, url, retries=retries, **kwargs)
        if response.next_request is None:
            response.raise_for_status()
            return response
        url = str(response.next_request.url)
        kwargs = {k:v for k,v in kwargs.items() if k != 'params'}
    raise httpx.TooManyRedirects('arXiv 重定向次数过多', request=response.request)
