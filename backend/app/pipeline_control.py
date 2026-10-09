"""Shared task switches and commands between the web service and one worker."""
import json
import threading
import asyncio
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from .config import now, settings
from .db import connect, one, execute, dumps

_heartbeat_lock = threading.Lock()
_stop_requested = ContextVar('pipeline_stop_requested', default=None)


@contextmanager
def cancellation_scope(requested):
    token=_stop_requested.set(requested)
    try:yield
    finally:_stop_requested.reset(token)


def check_cancelled():
    """Do not start or publish more work if a transport absorbed Task.cancel()."""
    requested=_stop_requested.get()
    if requested is not None and requested.is_set():
        raise asyncio.CancelledError
    task=asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


def publish_heartbeat(state):
    # Independent of SQLite's writer lock during the final index switch.
    path=settings().data_dir / '.pipeline-heartbeat.json'
    temporary=path.with_suffix('.tmp')
    with _heartbeat_lock:
        temporary.write_text(dumps({'value':state,'updated_at':now()}),encoding='utf-8')
        temporary.replace(path)


def enabled_jobs():
    saved = one("SELECT value FROM app_settings WHERE name='pipeline_enabled'")
    return json.loads(saved['value']) if saved else {}


def job_enabled(name):
    return enabled_jobs().get(name, True)


def set_job_enabled(name, enabled):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        saved = db.execute("SELECT value FROM app_settings WHERE name='pipeline_enabled'").fetchone()
        values = json.loads(saved['value']) if saved else {}
        values[name] = enabled
        db.execute("INSERT INTO app_settings(name,value,updated_at) VALUES('pipeline_enabled',?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (dumps(values), now()))
        if name=='author_impact' and not enabled:
            from .pipeline import author_runs
            author_runs.cancel(db,'disabled')


def publish_state(state):
    publish_heartbeat(state)
    execute("INSERT INTO app_settings(name,value,updated_at) VALUES('pipeline_worker',?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (dumps(state), now()))


def worker_state(db=None):
    found = db.execute("SELECT value,updated_at FROM app_settings WHERE name='pipeline_worker'").fetchone() if db else one("SELECT value,updated_at FROM app_settings WHERE name='pipeline_worker'")
    try:
        heartbeat=json.loads((settings().data_dir / '.pipeline-heartbeat.json').read_text(encoding='utf-8'))
        if not found or heartbeat['updated_at']>=found['updated_at']:
            found={'value':dumps(heartbeat['value']),'updated_at':heartbeat['updated_at']}
    except (OSError,ValueError,KeyError,TypeError):pass
    available = bool(found and json.loads(found['value']).get('pid') and datetime.now(timezone.utc)-datetime.fromisoformat(found['updated_at']) < timedelta(seconds=20))
    state = json.loads(found['value']) if available else {'busy':False, 'active':[], 'queued':[], 'pipeline':False, 'stopping':False, 'stopping_names':[], 'stopping_pipeline':False}
    pending = (db.execute("SELECT name,action FROM pipeline_commands WHERE status='queued'").fetchall() if db else [])
    for command in pending:
        if command['action'] in ('start','redo'):
            state['busy'] = True
            if command['name']=='pipeline': state['pipeline'] = True
            elif command['name'] not in state['queued']: state['queued'].append(command['name'])
    return {**state, 'worker_available':available, 'execution':'process'}


def public_state():
    if settings().pipeline_mode=='inline':
        from .scheduler import job_state
        return {**job_state(), 'worker_available':True, 'execution':'inline'}
    with connect() as db:
        return worker_state(db)


def queue_command(action, name, redo_id=None, *, db=None):
    own_connection=db is None
    with (connect() if own_connection else nullcontext(db)) as db:
        if own_connection:db.execute('BEGIN IMMEDIATE')
        state = worker_state(db)
        if not state['worker_available']: raise HTTPException(503,'后台工作进程尚未就绪，请稍后重试')
        if action in ('start','redo') and state['busy']: raise HTTPException(409,'当前有任务运行，请先停止或稍后再试')
        ident = db.execute('INSERT INTO pipeline_commands(action,name,created_at,redo_id) VALUES(?,?,?,?)', (action,name,now(),redo_id)).lastrowid
    return ident
