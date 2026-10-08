"""Bounded shared text cache; downloaded PDFs are disposable parsing inputs."""
import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from ..config import settings, now
from ..db import connect, dumps
from ..site_settings import configuration
from ..logs import event


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS fulltext_cache (
        paper_id INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
        cached_at TEXT NOT NULL, accessed_at TEXT NOT NULL, bytes INTEGER NOT NULL,
        source_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS fulltext_cache_pins (
        token TEXT PRIMARY KEY, paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
        expires_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_fulltext_pins_paper ON fulltext_cache_pins(paper_id);''')
    db.execute('''INSERT OR IGNORE INTO fulltext_cache(paper_id,cached_at,accessed_at,bytes)
        SELECT id,?,?,LENGTH(CAST(fulltext AS BLOB)) FROM papers WHERE fulltext IS NOT NULL''',(now(),now()))


def recover_pins():
    # Called by the sole pipeline worker at startup, never by the web process.
    with connect() as db:db.execute('DELETE FROM fulltext_cache_pins')


def get(paper_id):
    policy=configuration()
    if not policy['fulltext_cache_days'] or not policy['fulltext_cache_mb']:return None
    with connect() as db:
        row=db.execute('''SELECT p.fulltext,c.cached_at,c.source_json FROM papers p
            LEFT JOIN fulltext_cache c ON c.paper_id=p.id WHERE p.id=?''',(paper_id,)).fetchone()
        if not row or not row['fulltext']:return None
        cutoff=(datetime.now(timezone.utc)-timedelta(days=policy['fulltext_cache_days'])).isoformat()
        if row['cached_at'] and row['cached_at']<cutoff:return None
        try:data=json.loads(row['fulltext'])
        except (ValueError,TypeError):return None
        if row['source_json'] and json.loads(row['source_json']):data['_source']=json.loads(row['source_json'])
        stamp=now()
        db.execute('''INSERT INTO fulltext_cache VALUES(?,?,?,?,?) ON CONFLICT(paper_id)
            DO UPDATE SET accessed_at=excluded.accessed_at''',
            (paper_id,stamp,stamp,len(row['fulltext'].encode('utf-8')),dumps(data.get('_source',{}))))
        return data


def store(paper_id,data):
    raw=dumps(data);stamp=now()
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('UPDATE papers SET fulltext=?,pdf_path=NULL WHERE id=?',(raw,paper_id)).rowcount:
            raise RuntimeError('论文已删除，停止保存全文')
        db.execute('''INSERT INTO fulltext_cache VALUES(?,?,?,?,?) ON CONFLICT(paper_id)
            DO UPDATE SET cached_at=excluded.cached_at,accessed_at=excluded.accessed_at,
            bytes=excluded.bytes,source_json=excluded.source_json''',
            (paper_id,stamp,stamp,len(raw.encode('utf-8')),dumps(data.get('_source',{}))))


def using_fulltext(function):
    @wraps(function)
    async def wrapped(paper,*args,**kwargs):
        paper_id=paper['id'] if isinstance(paper,dict) else paper
        token=uuid.uuid4().hex
        with connect() as db:
            db.execute('INSERT INTO fulltext_cache_pins VALUES(?,?,?)',
                       (token,paper_id,(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()))
        try:return await function(paper,*args,**kwargs)
        finally:
            with connect() as db:db.execute('DELETE FROM fulltext_cache_pins WHERE token=?',(token,))
            safe_cleanup()
    return wrapped


def cleanup():
    policy=configuration();stamp=now()
    cutoff=(datetime.now(timezone.utc)-timedelta(days=policy['fulltext_cache_days'])).isoformat()
    limit=policy['fulltext_cache_mb']*1024*1024
    removed=[]
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM fulltext_cache_pins WHERE expires_at<?',(stamp,))
        pinned={r[0] for r in db.execute("SELECT paper_id FROM fulltext_cache_pins UNION SELECT paper_id FROM reading_jobs WHERE status='running'")}
        db.execute('DELETE FROM fulltext_cache WHERE paper_id IN (SELECT id FROM papers WHERE fulltext IS NULL)')
        entries=db.execute('SELECT * FROM fulltext_cache ORDER BY accessed_at,paper_id').fetchall()
        total=sum(row['bytes'] for row in entries)
        for row in entries:
            if row['paper_id'] in pinned:continue
            if row['cached_at']<cutoff or total>limit or not policy['fulltext_cache_days'] or not limit:
                db.execute('UPDATE papers SET fulltext=NULL,pdf_path=NULL WHERE id=?',(row['paper_id'],))
                db.execute('DELETE FROM fulltext_cache WHERE paper_id=?',(row['paper_id'],))
                total-=row['bytes'];removed.append(row['paper_id'])
        # Successful PDF files from older versions are no longer retained.
        db.execute('UPDATE papers SET pdf_path=NULL WHERE pdf_path IS NOT NULL')
    folder=(settings().data_dir/'pdf').resolve();files=0
    if folder.is_dir():
        for path in folder.iterdir():
            match=re.fullmatch(r'(\d+)(?:\.[a-f0-9]+)?\.(pdf|part)',path.name)
            if not match or int(match[1]) in pinned or not path.resolve().is_relative_to(folder):continue
            try:
                if path.is_file() and (match[2]=='pdf' or datetime.now().timestamp()-path.stat().st_mtime>3600):
                    path.unlink();files+=1
            except OSError:
                continue
    if removed or files:event('reading','全文缓存清理完成',job='fulltext_cache',removed_texts=len(removed),removed_pdf_files=files,remaining_bytes=total)
    return {'removed_texts':len(removed),'removed_pdf_files':files,'remaining_bytes':total}


async def maintain():
    while True:
        await asyncio.to_thread(safe_cleanup)
        await asyncio.sleep(60)


def safe_cleanup():
    try:return cleanup()
    except Exception as error:
        event('reading','全文缓存清理失败',level='error',job='fulltext_cache',error_type=type(error).__name__)
