"""Durable continuation for author batches; only the existing executor claims work."""
import json
from datetime import datetime, timedelta, timezone

from ..config import now
from ..db import connect, dumps, one

KEY = 'author_run'
RETRY_SECONDS = 600
YIELD_SECONDS = 5
COUNTERS = ('processed', 'matched', 'authors_updated')
WAITING = ('waiting', 'retry')


def timestamp(delay=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()


def read(db=None):
    row = db.execute('SELECT value FROM app_settings WHERE name=?', (KEY,)).fetchone() if db else one('SELECT value FROM app_settings WHERE name=?', (KEY,))
    return json.loads(row['value']) if row else None


def write(db, value):
    db.execute('INSERT INTO app_settings(name,value,updated_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at', (KEY, dumps(value), now()))


def progress(db):
    row = db.execute("SELECT progress FROM source_status WHERE name='author_impact'").fetchone()
    return json.loads(row['progress'] or '{}') if row else {}


def settle(value, batch):
    for key in COUNTERS:
        value[key] = value.get(key, 0) + batch.get(key, 0)
    value['pending'] = batch.get('pending', value.get('pending', 0))


def begin():
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = read(db) or {}
        value = {'generation': previous.get('generation', 0) + 1, 'phase': 'running',
                 **{key: 0 for key in COUNTERS}, 'pending': 0, 'retried': False,
                 'next_run': None, 'error': None}
        write(db, value)
        return value['generation']


def finish(generation, *, error=None):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        value = read(db)
        if not value or value['generation'] != generation or value['phase'] != 'running':
            return value
        settle(value, progress(db))
        value['error'] = error
        if error:
            value['phase'] = 'failed' if value['retried'] else 'retry'
            value['next_run'] = None if value['retried'] else timestamp(RETRY_SECONDS)
            value['retried'] = True
        else:
            value['phase'] = 'waiting' if value['pending'] else 'complete'
            value['next_run'] = timestamp(YIELD_SECONDS) if value['pending'] else None
            value['retried'] = False
        write(db, value)
        return value


def cancel(db=None, phase='stopped'):
    if db is None:
        with connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            return cancel(connection, phase)
    value = read(db)
    if not value or value['phase'] not in ('running', *WAITING):
        return False
    if value['phase'] == 'running':
        settle(value, progress(db))
    value.update(generation=value['generation'] + 1, phase=phase, next_run=None)
    write(db, value)
    return True


def recover():
    # A worker restart continues a partial batch, preserving a used retry.
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        value = read(db)
        if value and value['phase'] == 'running':
            settle(value, progress(db))
            value.update(phase='waiting', next_run=timestamp(YIELD_SECONDS))
            write(db, value)


def claim():
    value = read()
    if not value or value['phase'] not in WAITING or datetime.fromisoformat(value['next_run']) > datetime.now(timezone.utc):
        return None
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        latest = read(db)
        if latest != value or db.execute("SELECT 1 FROM pipeline_commands WHERE status='queued' LIMIT 1").fetchone():
            return None
        switches = db.execute("SELECT value FROM app_settings WHERE name='pipeline_enabled'").fetchone()
        if switches and not json.loads(switches['value']).get('author_impact', True):
            cancel(db, 'disabled')
            return None
        latest.update(phase='running', next_run=None)
        db.execute("UPDATE source_status SET progress='{}' WHERE name='author_impact'")
        write(db, latest)
        return latest['generation']


def snapshot():
    with connect() as db:
        db.execute('BEGIN')
        value = read(db)
        if value and value['phase'] == 'running':
            settle(value, progress(db))
        return value
