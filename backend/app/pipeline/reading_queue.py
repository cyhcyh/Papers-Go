"""A persistent FIFO for shared reading cards, consumed by the background worker."""
import asyncio
import json
from fastapi import HTTPException
from ..config import now, settings
from ..db import connect, one, dumps
from ..llm import runtime as models
from ..logs import event
from ..pipeline_control import cancellation_scope

_stop_requests = {}


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS reading_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        paper_id INTEGER NOT NULL UNIQUE REFERENCES reading_cards(paper_id) ON DELETE CASCADE,
        level TEXT NOT NULL, kind TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'user',
        status TEXT NOT NULL DEFAULT 'queued', queued_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_reading_queue ON reading_jobs(status,kind,id);''')


def recover():
    # Waiting jobs survive a restart. An interrupted model call needs an explicit retry.
    with connect() as db:
        db.execute("UPDATE reading_cards SET status='failed',error='生成被中断，请重试' WHERE status='pending' AND (paper_id IN (SELECT paper_id FROM reading_jobs WHERE status='running') OR paper_id NOT IN (SELECT paper_id FROM reading_jobs))")
        db.execute("DELETE FROM reading_jobs WHERE status='running'")


def enqueue(paper_id, level='L2', retry=False, regenerate=False, source='user',allow_regenerate=True):
    feature='reading_l3' if level=='L3' else 'reading_l2'
    if regenerate and not allow_regenerate:raise HTTPException(403,'只有管理员可以重新生成精读卡')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        cached=db.execute('SELECT * FROM reading_cards WHERE paper_id=?',(paper_id,)).fetchone()
        if cached and cached['card_json'] and not allow_regenerate:
            level_change=level=='L3' and json.loads(cached['card_json']).get('reading_level')!='L3'
            if retry or level_change:raise HTTPException(403,'已有精读卡的重做仅限管理员')
        job=db.execute('SELECT * FROM reading_jobs WHERE paper_id=?',(paper_id,)).fetchone()
        if job:
            if source=='user':db.execute("UPDATE reading_jobs SET source='user' WHERE id=?",(job['id'],))
            return dict(cached)
        if cached and cached['status']=='ready' and not regenerate:
            if level!='L3' or json.loads(cached['card_json']).get('reading_level')=='L3':return dict(cached)
        if cached and cached['status']=='failed' and not (retry or regenerate):return dict(cached)
        # Compatibility with an already running call from the previous application version.
        if cached and cached['status']=='pending':return dict(cached)
        kind='ollama' if models.selected(feature)['kind']=='ollama' else 'cloud'
        queued_at=now()
        progress={'stage':'queued','queued_at':queued_at,'level':level}
        db.execute("INSERT INTO reading_cards(paper_id,status,progress_json,created_at) VALUES(?,'pending',?,?) ON CONFLICT(paper_id) DO UPDATE SET status='pending',error=NULL,progress_json=excluded.progress_json,created_at=excluded.created_at",(paper_id,dumps(progress),queued_at))
        db.execute('INSERT INTO reading_jobs(paper_id,level,kind,source,queued_at) VALUES(?,?,?,?,?)',(paper_id,level,kind,source,queued_at))
        return dict(db.execute('SELECT * FROM reading_cards WHERE paper_id=?',(paper_id,)).fetchone())


def queue_info(paper_id):
    return one("""SELECT j.status,j.queued_at,
        (SELECT COUNT(*) FROM reading_jobs ahead WHERE ahead.status='queued' AND ahead.kind=j.kind AND ahead.id<j.id) AS queue_ahead
        FROM reading_jobs j WHERE j.paper_id=? AND j.status='queued'""",(paper_id,))


async def _run(job):
    from . import read
    requested=asyncio.Event()
    task=asyncio.current_task()
    _stop_requests[task]=requested
    try:
        # Shared user cards outlive the pipeline that first queued a preread.
        with cancellation_scope(requested):
            await read.run_card(job['paper_id'],job['level'])
    finally:
        _stop_requests.pop(task,None)
        with connect() as db:db.execute('DELETE FROM reading_jobs WHERE id=?',(job['id'],))
        from .fulltext_cache import safe_cleanup
        safe_cleanup()
        if read._tasks.get(job['paper_id']) is asyncio.current_task():read._tasks.pop(job['paper_id'],None)
        if settings().pipeline_mode=='inline' and not asyncio.current_task().cancelling():dispatch()


def dispatch():
    from . import read
    if not one("SELECT id FROM reading_jobs WHERE status='queued' LIMIT 1"):
        return
    config=models.configuration()
    limits={'cloud':config.get('reading_cloud_concurrency',settings().reading_cloud_concurrency),
            'ollama':config.get('reading_local_concurrency',settings().reading_local_concurrency)}
    claimed=[]
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        # Waiting cards use the current routing; running cards keep their model snapshot.
        for level in ('L2','L3'):
            choice=config['routes']['reading_l3' if level=='L3' else 'reading_l2']['primary']
            kind='ollama' if models.resolve(choice,config)['kind']=='ollama' else 'cloud'
            db.execute("UPDATE reading_jobs SET kind=? WHERE level=? AND status='queued' AND kind!=?",(kind,level,kind))
        for kind,limit in limits.items():
            running=db.execute("SELECT COUNT(*) FROM reading_jobs WHERE kind=? AND status='running'",(kind,)).fetchone()[0]
            jobs=db.execute("SELECT * FROM reading_jobs WHERE kind=? AND status='queued' ORDER BY id LIMIT ?",(kind,max(0,limit-running))).fetchall()
            for job in jobs:
                db.execute("UPDATE reading_jobs SET status='running' WHERE id=?",(job['id'],))
                db.execute('UPDATE reading_cards SET progress_json=? WHERE paper_id=?',(dumps({'stage':'parsing','started_at':now(),'queued_at':job['queued_at'],'level':job['level']}),job['paper_id']))
                claimed.append(dict(job))
    for job in claimed:read._tasks[job['paper_id']]=asyncio.create_task(_run(job))


async def serve(*, poll_seconds=.5, retry_seconds=5):
    from . import read
    recovered=False
    try:
        while True:
            try:
                if not recovered:
                    recover()
                    recovered=True
                dispatch()
            except Exception as error:
                # A transient database/configuration error must not silently kill
                # the consumer or cancel cards that are already generating.
                event('reading','精读队列调度异常，将自动重试',level='error',job='reading_queue',
                      error_type=type(error).__name__,error=models.safe_error(error))
                await asyncio.sleep(retry_seconds)
            else:
                await asyncio.sleep(poll_seconds)
    finally:
        tasks=list(read._tasks.values())
        for task in tasks:
            if requested:=_stop_requests.get(task):requested.set()
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


async def stop_preread(paper_id):
    from . import read
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        job=db.execute("SELECT * FROM reading_jobs WHERE paper_id=? AND source='preread'",(paper_id,)).fetchone()
        if not job:return  # A user's shared request must continue after stopping the pipeline.
        if job['status']=='queued':
            db.execute("UPDATE reading_cards SET status='failed',error='预读已停止，可手动重试' WHERE paper_id=?",(paper_id,))
            db.execute('DELETE FROM reading_jobs WHERE id=?',(job['id'],))
            return
        task=read._tasks.get(paper_id)
        if task:
            if requested:=_stop_requests.get(task):requested.set()
            task.cancel()
    if task:await asyncio.gather(task,return_exceptions=True)
