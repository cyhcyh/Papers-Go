"""Metadata-only administrator logs. Credentials never belong in this table."""
import json
import re
import asyncio
import time
from datetime import datetime, timedelta, timezone
from .config import now
from .db import connect

PRIVATE = re.compile(r'authorization|api.?key|password|secret|access_token|refresh_token|headers|messages|prompt|content', re.I)


def safe(value):
    if isinstance(value, dict):
        return {str(k): safe(v) for k, v in value.items() if not PRIVATE.search(str(k))}
    if isinstance(value, (tuple, list)):
        return [safe(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'(?i)Bearer\s+\S+', '[凭证已隐藏]', value)
        value = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[凭证已隐藏]', value)
        value = re.sub(r'(?i)(api[_-]?key|authorization|password|secret)\s*[:=]\s*[^\s,;]+', r'\1=[已隐藏]', value)
        return value[:2000]
    return value


def event(kind, message, *, level='info', job=None, model=None, user_id=None, **detail):
    # Logs must not make an otherwise successful request fail.
    try:
        with connect() as db:
            db.execute('INSERT INTO app_logs(kind,level,job,model,message,detail,user_id,created_at) VALUES(?,?,?,?,?,?,?,?)',
                       (kind, level, job, model, safe(message), json.dumps(safe(detail), ensure_ascii=False), user_id, now()))
    except Exception:
        import logging
        logging.getLogger(__name__).warning('Unable to save application log')


def cleanup(*, batch_size=2000, max_batches=5):
    """Short transactions, oldest first; use the existing timestamp index."""
    from .site_settings import LOG_DEFAULTS
    deleted=0;excess=None;expired=True;more=False;started=time.monotonic()
    with connect() as db:
        saved=db.execute("SELECT value FROM app_settings WHERE name='site'").fetchone()
        policy={**LOG_DEFAULTS,**(json.loads(saved['value']) if saved else {})}
        cutoff=(datetime.now(timezone.utc)-timedelta(days=policy['log_retention_days'])).isoformat()
        for _ in range(max_batches):
            changed=0
            if expired:
                changed=db.execute('DELETE FROM app_logs WHERE id IN (SELECT id FROM app_logs WHERE created_at<? ORDER BY created_at,id LIMIT ?)',(cutoff,batch_size)).rowcount
                expired=changed==batch_size
            if not changed and not expired:
                if excess is None:
                    count=db.execute('SELECT COUNT(*) FROM app_logs').fetchone()[0]
                    excess=max(0,count-policy['log_max_entries'])
                if excess:
                    changed=db.execute('DELETE FROM app_logs WHERE id IN (SELECT id FROM app_logs ORDER BY created_at,id LIMIT ?)',(min(batch_size,excess),)).rowcount
                    excess-=changed
            db.commit()
            deleted+=changed
            more=expired or bool(excess)
            # A final age batch can be short while the count limit still needs a check.
            if changed and excess is None:more=True
            if not changed or not more:break
            if time.monotonic()-started>=.25:break
    return {'deleted':deleted,'more':more}


def safe_cleanup():
    try:return cleanup()
    except Exception:
        import logging
        logging.getLogger(__name__).warning('Unable to clean application logs')
        return {'deleted':0,'more':False}


async def maintain():
    # The web process owns this loop; inserts in either process only write one row.
    delay=30
    while True:
        await asyncio.sleep(delay)
        task=asyncio.create_task(asyncio.to_thread(safe_cleanup))
        try:result=await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        delay=1 if result['more'] else 30
