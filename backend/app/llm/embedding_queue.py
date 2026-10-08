"""Cross-process embedding permits with interactive priority and renewable leases."""
import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from ..db import connect
from ..config import settings

_priority=ContextVar('embedding_priority',default='interactive')
SCHEMA='''CREATE TABLE IF NOT EXISTS embedding_permits (
 id TEXT PRIMARY KEY,pool TEXT NOT NULL,priority TEXT NOT NULL,pid INTEGER NOT NULL,expires REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_embedding_permits_pool ON embedding_permits(pool,expires);
CREATE TABLE IF NOT EXISTS embedding_waiters (
 id TEXT PRIMARY KEY,pool TEXT NOT NULL,priority TEXT NOT NULL,expires REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_embedding_waiters_pool ON embedding_waiters(pool,priority);'''


def limits(binding,config):
    total=config.get('embedding_local_concurrency',1) if binding['kind']=='ollama' else config.get('embedding_cloud_concurrency',4)
    return total,max(1,total-1)


def claim(binding,config,token,priority):
    pool=binding['kind']+':'+binding['base_url'].rstrip('/')
    total,background=limits(binding,config);stamp=time.time()
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM embedding_permits WHERE expires<?',(stamp,))
        db.execute('DELETE FROM embedding_waiters WHERE expires<?',(stamp,))
        db.execute('INSERT OR REPLACE INTO embedding_waiters VALUES(?,?,?,?)',(token,pool,priority,stamp+30))
        active=db.execute('SELECT priority FROM embedding_permits WHERE pool=?',(pool,)).fetchall()
        if len(active)>=total:return False
        if priority=='background':
            if sum(r['priority']=='background' for r in active)>=background:return False
            if db.execute("SELECT 1 FROM embedding_waiters WHERE pool=? AND priority='interactive' LIMIT 1",(pool,)).fetchone():return False
        db.execute('DELETE FROM embedding_waiters WHERE id=?',(token,))
        db.execute('INSERT INTO embedding_permits VALUES(?,?,?,?,?)',(token,pool,priority,os.getpid(),stamp+max(60,settings().llm_timeout+30)))
    return True


async def heartbeat(token):
    while True:
        await asyncio.sleep(15)
        with connect() as db:db.execute('UPDATE embedding_permits SET expires=? WHERE id=?',(time.time()+max(60,settings().llm_timeout+30),token))


@asynccontextmanager
async def permit(binding,config):
    token=uuid.uuid4().hex;renew=None
    try:
        while not claim(binding,config,token,_priority.get()):await asyncio.sleep(.1)
        renew=asyncio.create_task(heartbeat(token))
        yield
    finally:
        if renew:
            renew.cancel();await asyncio.gather(renew,return_exceptions=True)
        with connect() as db:
            db.execute('DELETE FROM embedding_permits WHERE id=?',(token,))
            db.execute('DELETE FROM embedding_waiters WHERE id=?',(token,))


@asynccontextmanager
async def background():
    token=_priority.set('background')
    try:yield
    finally:_priority.reset(token)
