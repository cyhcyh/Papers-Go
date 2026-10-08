"""A crash-releasing GPU slot shared by the web process and pipeline worker."""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from ..config import settings, now
from ..db import one, execute


@asynccontextmanager
async def local_slot():
    from .runtime import current_binding
    foreground = (current_binding() or {}).get('feature')=='chat'
    background = os.environ.get('PIPELINE_WORKER')=='1'
    path=settings().data_dir/'local-model.lock'
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0);handle.write(b'0');handle.flush()
        try:
            while True:
                if foreground:
                    execute("INSERT INTO app_settings(name,value,updated_at) VALUES('local_chat_waiting','1',?) ON CONFLICT(name) DO UPDATE SET value='1',updated_at=excluded.updated_at",(now(),))
                pending=one("SELECT value,updated_at FROM app_settings WHERE name='local_chat_waiting'") if background else None
                if pending and pending['value']=='1' and datetime.now(timezone.utc)-datetime.fromisoformat(pending['updated_at'])<timedelta(seconds=3):
                    await asyncio.sleep(.15);continue
                try:
                    if os.name=='nt':
                        import msvcrt
                        handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                    else:
                        import fcntl
                        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except (BlockingIOError,OSError):
                    await asyncio.sleep(.15)
            if foreground: execute("UPDATE app_settings SET value='0' WHERE name='local_chat_waiting'")
            yield
        finally:
            if foreground: execute("UPDATE app_settings SET value='0' WHERE name='local_chat_waiting'")
            # Closing the handle also releases the OS lock after a crash or cancellation.
