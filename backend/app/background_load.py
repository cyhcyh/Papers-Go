"""A small, cross-process load signal; background work yields between commits."""
import asyncio
import json
import os
import threading
import time

from .config import settings

_lock = threading.Lock()
_active = 0
_slow_until = 0.0
_written = 0.0
_delay_cache = (None, 0.0, None)


def _publish():
    global _written
    stamp = time.time()
    if stamp - _written < .5:
        return
    _written = stamp
    with _lock:
        value={'at':stamp,'active':_active,'slow_until':_slow_until}
    path = settings().data_dir / '.web-load.json'
    temporary = path.with_suffix(f'.{os.getpid()}.tmp')
    try:
        temporary.write_text(json.dumps(value), encoding='utf-8')
        os.replace(temporary, path)
    except OSError:
        pass  # Telemetry cannot make a successful user request fail.


def delay(base=.02):
    """Never wait indefinitely, and ignore telemetry left by a stopped server."""
    global _delay_cache
    try:
        target=settings().data_dir / '.web-load.json'
        key,checked,value=_delay_cache
        if key!=target or time.monotonic()-checked>=.25:
            value=json.loads(target.read_text(encoding='utf-8'))
            _delay_cache=(target,time.monotonic(),value)
        stamp = time.time()
        if stamp - value['at'] < 5:
            if value.get('slow_until', 0) > stamp:
                return max(base, .4)
            if value.get('active', 0):
                return max(base, .08)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return base


async def yield_to_web(base=.02):
    await asyncio.sleep(await asyncio.to_thread(delay, base))


async def publish_loop():
    # HTTP middleware only updates memory. Disk telemetry runs independently,
    # with one write in flight, so it cannot delay a response or a streaming reply.
    while True:
        await asyncio.to_thread(_publish)
        await asyncio.sleep(.5)


class WebLoadMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        global _active, _slow_until
        # Streaming model replies and frequent health/progress polling are not
        # evidence that a database page request is competing with the worker.
        path = scope.get('path', '')
        if scope['type'] != 'http' or not path.startswith('/api/') or path.startswith(
                ('/api/chat', '/api/health', '/api/admin/jobs', '/api/admin/pipeline')) or '/stream' in path:
            return await self.app(scope, receive, send)
        started = time.perf_counter()
        with _lock:
            _active += 1
        try:
            await self.app(scope, receive, send)
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            with _lock:
                _active -= 1
                if elapsed >= settings().background_slow_request_ms:
                    _slow_until = time.time() + 3
