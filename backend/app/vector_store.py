"""Independent, versioned paper vectors. Business transactions contain no vector index writes.

papers.embedding is a readiness marker after migration; user profile vectors
remain transactional with feedback/undo. Existing blobs are readable until the
first independent space has been validated and activated.
"""
import atexit
import json
import re
import sqlite3
import threading
import uuid
import os
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

from .config import settings, now

READY = b'\x01'
_NAME = re.compile(r'vectors-[a-f0-9]{32}\.sqlite3\Z')
_lock = threading.RLock()
_write_lock = threading.RLock()
_readers = OrderedDict()
_writers = OrderedDict()
_business_reader = None
_business_path = None


def _retain_business_reader():
    """Keep WAL coordination open without retaining a read transaction.

    Previously the KNN connection kept the business WAL open. Closing its last
    reader on every HTTP query is costly on the Windows Docker bind mount.
    This read-only, autocommit handle preserves that reuse after isolation.
    """
    global _business_reader, _business_path
    target = settings().data_dir / 'papers.sqlite3'
    key = str(target.resolve())
    with _lock:
        if _business_path == key:
            return
        if _business_reader is not None:
            _business_reader.close()
        db = sqlite3.connect(target.resolve().as_uri() + '?mode=ro', uri=True,
                             isolation_level=None, check_same_thread=False, timeout=30)
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('SELECT 1 FROM app_settings LIMIT 1').fetchone()
        except BaseException:
            db.close()
            _business_reader = _business_path = None
            raise
        _business_reader, _business_path = db, key


class _Reader:
    def __init__(self,db):
        self.db=db
        self.lock=threading.RLock()
        self.users=0
        self.closing=False

SCHEMA = '''
CREATE TABLE IF NOT EXISTS vector_spaces(name TEXT PRIMARY KEY,created_at TEXT NOT NULL,retired_at TEXT);
CREATE TABLE IF NOT EXISTS vector_cleanup(space TEXT NOT NULL,paper_id INTEGER NOT NULL,revision INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(space,paper_id));
CREATE TABLE IF NOT EXISTS vector_publications(space TEXT NOT NULL,paper_id INTEGER NOT NULL,was_ready INTEGER NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(space,paper_id));
CREATE TRIGGER IF NOT EXISTS vector_paper_delete AFTER DELETE ON papers BEGIN
 INSERT INTO vector_cleanup(space,paper_id) SELECT json_extract(value,'$.name'),OLD.id
 FROM app_settings WHERE name='vector_store'
 ON CONFLICT(space,paper_id) DO UPDATE SET revision=revision+1; END;
CREATE TRIGGER IF NOT EXISTS vector_paper_invalidate AFTER UPDATE OF embedding ON papers
 WHEN NEW.embedding IS NULL BEGIN
 INSERT INTO vector_cleanup(space,paper_id) SELECT json_extract(value,'$.name'),NEW.id
 FROM app_settings WHERE name='vector_store'
 ON CONFLICT(space,paper_id) DO UPDATE SET revision=revision+1; END;
'''


def path(name):
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError('无效的向量存储版本')
    return settings().data_dir / 'vectors' / name


def active(db=None):
    if db is None:
        from .db import connect
        with connect() as conn:
            return active(conn)
    row = db.execute("SELECT value FROM app_settings WHERE name='vector_store'").fetchone()
    return json.loads(row[0]) if row else None


def epoch(db=None):
    value = active(db)
    return value['epoch'] if value else 'legacy'


def _open(name, *, readonly=False):
    target = path(name)
    if readonly:
        db = sqlite3.connect(target.resolve().as_uri() + '?mode=ro', uri=True,
                             isolation_level=None, check_same_thread=False, timeout=30)
        db.execute('PRAGMA query_only=ON')
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(target, timeout=30, check_same_thread=False, uri=True)
        db.execute('PRAGMA journal_mode=WAL')
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=30000')
    db.execute(f'PRAGMA cache_size={-(64 if readonly else settings().vector_cache_mb) * 1024}')
    db.execute(f'PRAGMA mmap_size={settings().sqlite_mmap_mb * 1024 * 1024}')
    import sqlite_vec
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    return db


@contextmanager
def writer(name):
    key = (str(settings().data_dir.resolve()), name)
    with _write_lock:
        db = _writers.get(key)
        if db is None:
            db = _open(name)
            _writers[key] = db
        _writers.move_to_end(key)
        while len(_writers) > 2:
            _, old = _writers.popitem(last=False)
            old.close()
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


@contextmanager
def reader(name):
    # Serialize each connection, not different files: build progress reads must
    # not block web searches against the live version.
    _retain_business_reader()
    key = (str(settings().data_dir.resolve()), name)
    dispose=[]
    with _lock:
        entry = _readers.get(key)
        if entry is None:
            entry = _Reader(_open(name, readonly=True))
            _readers[key] = entry
        entry.users+=1
        _readers.move_to_end(key)
        while len(_readers) > 3:
            _, old = _readers.popitem(last=False)
            old.closing=True
            if not old.users:dispose.append(old)
    for old in dispose:old.db.close()
    try:
        with entry.lock:
            yield entry.db
    finally:
        with _lock:
            entry.users-=1
            closing=entry.closing and not entry.users
            if closing:entry.closing=False
        if closing:entry.db.close()


def close():
    global _business_reader, _business_path
    dispose=[]
    with _lock:
        for entry in _readers.values():
            entry.closing=True
            if not entry.users:dispose.append(entry)
        _readers.clear()
        if _business_reader is not None:
            _business_reader.close()
        _business_reader = _business_path = None
    for entry in dispose:entry.db.close()
    with _write_lock:
        for db in _writers.values():
            db.close()
        _writers.clear()


def revision(space=None):
    value=space if space is not None else active()
    if not value:
        return 0
    with reader(value['name']) as db:
        return db.execute("SELECT value FROM metadata WHERE name='revision'").fetchone()[0]


def create(dim, name=None):
    from .db import connect
    name = name or f'vectors-{uuid.uuid4().hex}.sqlite3'
    path(name)  # Validate before adding the registry entry.
    with connect() as db:
        stamp=now()
        db.execute('INSERT OR IGNORE INTO vector_spaces(name,created_at,retired_at) VALUES(?,?,?)', (name, stamp, stamp))
    with writer(name) as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS paper_vectors(
            paper_id INTEGER PRIMARY KEY,title TEXT NOT NULL,abstract TEXT NOT NULL,embedding BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS profile_vectors(profile_id INTEGER PRIMARY KEY,content TEXT NOT NULL,embedding BLOB);
            CREATE TABLE IF NOT EXISTS profile_vector_parts(profile_id INTEGER PRIMARY KEY,parts TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata(name TEXT PRIMARY KEY,value INTEGER NOT NULL);''')
        db.execute("INSERT OR IGNORE INTO metadata VALUES('dimension',?)", (dim,))
        db.execute("INSERT OR IGNORE INTO metadata VALUES('revision',0)")
    return name


def attach(db, name, alias='vector_data'):
    # Identifiers are internal constants; filenames never come from request paths.
    if alias not in ('vector_data', 'vector_build'):
        raise ValueError('无效的向量连接')
    db.execute(f'ATTACH DATABASE ? AS {alias}', (path(name).resolve().as_uri() + '?mode=ro',))


def index_create(db, dim):
    db.execute('DROP TABLE IF EXISTS main.papers_vec')
    db.execute(f'CREATE VIRTUAL TABLE main.papers_vec USING vec0(paper_id INTEGER PRIMARY KEY,embedding float[{dim}] distance_metric=cosine)')


def stage(db, papers):
    db.executemany('INSERT OR REPLACE INTO paper_vectors VALUES(?,?,?,?)', papers)


def activate(db, name, dim, *, model_epoch=None):
    old = active(db)
    value = {'name': name, 'dimension': dim, 'epoch': model_epoch or (old['epoch'] if old else 'legacy')}
    if old and old['name'] != name:
        db.execute('UPDATE vector_spaces SET retired_at=? WHERE name=?', (now(), old['name']))
    db.execute('UPDATE vector_spaces SET retired_at=NULL WHERE name=?',(name,))
    from .db import dumps
    db.execute("INSERT INTO app_settings VALUES('vector_store',?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (dumps(value), now()))
    db.execute("UPDATE app_settings SET value=CAST(value AS INTEGER)+1 WHERE name='paper_revision'")
    return value


def get_many(identifiers, *, space=None, db=None):
    identifiers = list(dict.fromkeys(identifiers))
    if not identifiers:
        return {}
    value = space if space is not None else active(db)
    if not value:
        from .db import rows
        result = {}
        for start in range(0, len(identifiers), 500):
            batch = identifiers[start:start+500]
            sql = 'SELECT id,embedding FROM papers WHERE id IN (' + ','.join('?' for _ in batch) + ')'
            found = db.execute(sql, batch) if db else rows(sql, batch)
            result.update((r['id'], r['embedding']) for r in found
                          if r['embedding'] is not None and len(r['embedding']) > 1)
        return result
    result = {}
    with reader(value['name']) as conn:
        for start in range(0, len(identifiers), 500):
            batch = identifiers[start:start+500]
            result.update((r[0], r[1]) for r in conn.execute(
                'SELECT paper_id,embedding FROM paper_vectors WHERE paper_id IN (' + ','.join('?' for _ in batch) + ')', batch))
    return result


def get(paper_id, db=None):
    if db is None:
        from .db import connect
        with connect() as conn:
            return get(paper_id,conn)
    row=db.execute('SELECT embedding FROM papers WHERE id=?',(paper_id,)).fetchone()
    if not row or row[0] is None:
        return None
    return get_many([paper_id], db=db).get(paper_id)


def hydrate(papers, *, space=None, db=None):
    ready = [p['id'] for p in papers if p.get('embedding') is not None]
    vectors = get_many(ready, space=space, db=db)
    for paper in papers:
        paper['embedding'] = vectors.get(paper['id'])
    return papers


def ensure():
    """Copy existing vectors once; keep legacy reads live until atomic activation.

    Run under the worker's single-process lock in production. Concurrent normal
    writers are detected by final snapshot validation, then copied again.
    """
    # Only initial migration takes this OS lock. Existing spaces bypass it.
    if value:=active():
        return value
    with _write_lock:
        _retain_business_reader()
        lock_path=settings().data_dir/'vector-store-init.lock'
        with lock_path.open('a+b') as handle:
            if os.name=='nt':
                import msvcrt,time
                handle.seek(0);handle.write(b'0');handle.flush();handle.seek(0)
                while True:
                    try:
                        msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1);break
                    except OSError:time.sleep(.05)
            else:
                import fcntl
                fcntl.flock(handle,fcntl.LOCK_EX)
            try:return _ensure()
            finally:
                if os.name=='nt':
                    handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)


def _ensure():
    from .db import connect
    from .background_load import delay
    import time
    value = active()
    if value:
        return value
    dim = settings().embedding_dim
    name = create(dim)
    try:
        with writer(name) as vectors:
            index_create(vectors, dim)
            vectors.commit()
            while True:
                last = 0
                with connect() as source:
                    while batch := source.execute('SELECT id,title,COALESCE(abstract,\'\') abstract,embedding FROM papers WHERE id>? AND embedding IS NOT NULL ORDER BY id LIMIT 1024', (last,)).fetchall():
                        last = batch[-1]['id']
                        values = [(r['id'],r['title'],r['abstract'],r['embedding']) for r in batch]
                        vectors.executemany('DELETE FROM papers_vec WHERE paper_id=?', [(r[0],) for r in values])
                        stage(vectors, values)
                        vectors.executemany('INSERT INTO papers_vec VALUES(?,?)', [(r[0],r[3]) for r in values])
                        vectors.commit()
                        time.sleep(delay())
                with connect() as source:
                    attach(source, name)
                    source.execute('BEGIN IMMEDIATE')
                    if active(source):
                        return active(source)
                    changed = source.execute('''SELECT 1 FROM papers p LEFT JOIN vector_data.paper_vectors v ON v.paper_id=p.id
                        WHERE p.embedding IS NOT NULL AND (v.paper_id IS NULL OR v.title!=p.title OR v.abstract!=COALESCE(p.abstract,'') OR v.embedding!=p.embedding) LIMIT 1''').fetchone()
                    if changed:
                        continue
                    source.execute('''INSERT INTO vector_cleanup(space,paper_id)
                        SELECT ?,v.paper_id FROM vector_data.paper_vectors v
                        LEFT JOIN papers p ON p.id=v.paper_id WHERE p.id IS NULL OR p.embedding IS NULL
                        ON CONFLICT(space,paper_id) DO UPDATE SET revision=revision+1''', (name,))
                    return activate(source, name, dim)
    except BaseException:
        with connect() as db:
            db.execute('UPDATE vector_spaces SET retired_at=? WHERE name=?',(now(),name))
        raise


def set_many(papers, blobs):
    """Stage vectors, record publication, then commit; failed/deleted rows stay hidden."""
    from .db import connect
    from .pipeline.alert_state import paper_queue
    value = ensure()
    if len(papers) != len(blobs):
        raise ValueError('论文与向量的数量不一致')
    if any(len(blob)!=value['dimension']*4 for blob in blobs):
        raise ValueError('向量维度与当前存储不一致')
    from .llm import runtime
    from .llm.secrets import decrypt_configuration
    with connect() as db:
        saved=db.execute("SELECT value FROM app_settings WHERE name='models'").fetchone()
        live=decrypt_configuration(json.loads(saved[0])) if saved else runtime.defaults()
        if runtime.embedding_identity(live)!=runtime.embedding_identity(runtime.configuration()):
            raise ValueError('向量模型已切换，请使用当前模型重试')
    written=[];acknowledged=False
    try:
        with writer(value['name']) as vectors:
            stage(vectors, [(p['id'],p['title'],p.get('abstract') or '',b) for p,b in zip(papers,blobs)])
            vectors.executemany('DELETE FROM papers_vec WHERE paper_id=?', [(p['id'],) for p in papers])
            vectors.executemany('INSERT INTO papers_vec VALUES(?,?)', [(p['id'],b) for p,b in zip(papers,blobs)])
            vectors.execute("UPDATE metadata SET value=value+1 WHERE name='revision'")
            # Business failure rolls this vector transaction back. If the final
            # vector commit fails, its durable publication records repair flags.
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if active(db)['name'] != value['name']:
                    raise ValueError('向量版本已切换，请使用当前模型重试')
                for paper in papers:
                    db.execute('''INSERT OR REPLACE INTO vector_publications
                        SELECT ?,id,embedding IS NOT NULL,? FROM papers WHERE id=? AND title=? AND COALESCE(abstract,'')=?''',
                        (value['name'],now(),paper['id'],paper['title'],paper.get('abstract') or ''))
                    result = db.execute("UPDATE papers SET embedding=? WHERE id=? AND title=? AND COALESCE(abstract,'')=?", (READY,paper['id'],paper['title'],paper.get('abstract') or ''))
                    if result.rowcount:
                        db.execute('DELETE FROM vector_cleanup WHERE space=? AND paper_id=?', (value['name'],paper['id']))
                        db.execute(paper_queue(str(int(paper['id']))).rstrip(';'))
                        written.append(paper['id'])
                    else:
                        db.execute('INSERT INTO vector_cleanup(space,paper_id) VALUES(?,?) ON CONFLICT(space,paper_id) DO UPDATE SET revision=revision+1', (value['name'],paper['id']))
            acknowledged=True
    except BaseException:
        if acknowledged:
            try:repair_publications(value['name'],written)
            except Exception:pass  # The durable record remains for worker recovery.
        raise
    try:
        with connect() as db:
            db.executemany('DELETE FROM vector_publications WHERE space=? AND paper_id=?',[(value['name'],i) for i in written])
    except Exception:
        pass  # Published data is valid; a missed acknowledgement is retried.
    return written


def repair_publications(name=None,identifiers=None):
    # Normal publication and its recovery cannot overlap within the worker.
    with _write_lock:
        return _repair_publications(name,identifiers)


def _repair_publications(name=None,identifiers=None):
    from .db import connect
    cutoff=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
    with connect() as db:
        if name is not None:
            found=db.execute('SELECT * FROM vector_publications WHERE space=? AND paper_id IN ('+','.join('?' for _ in identifiers)+')',[name,*identifiers]).fetchall() if identifiers else []
        else:
            found=db.execute('SELECT * FROM vector_publications WHERE created_at<? LIMIT 50',(cutoff,)).fetchall()
    for record in found:
        with connect() as db:
            paper=db.execute('SELECT id,title,abstract,embedding FROM papers WHERE id=?',(record['paper_id'],)).fetchone()
        matches=False
        if paper and path(record['space']).exists():
            with reader(record['space']) as vectors:
                stored=vectors.execute('SELECT title,abstract FROM paper_vectors WHERE paper_id=?',(paper['id'],)).fetchone()
                matches=stored and stored['title']==paper['title'] and stored['abstract']==(paper['abstract'] or '')
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT created_at FROM vector_publications WHERE space=? AND paper_id=?',
                                 (record['space'],record['paper_id'])).fetchone()
            if not current or current[0] != record['created_at']:
                continue
            live=active(db)
            if not matches and not record['was_ready'] and live and live['name']==record['space'] and paper:
                db.execute("UPDATE papers SET embedding=NULL WHERE id=? AND embedding=? AND title=? AND COALESCE(abstract,'')=?",
                           (record['paper_id'],READY,paper['title'],paper['abstract'] or ''))
            db.execute('DELETE FROM vector_publications WHERE space=? AND paper_id=? AND created_at=?',(record['space'],record['paper_id'],record['created_at']))
    return len(found)


def cleanup_batch():
    """Persistent exact-ID cleanup. Removing a paper cannot delete an unrelated vector."""
    from .db import connect
    with _write_lock,connect() as db:
        pending = db.execute('SELECT space,paper_id,revision FROM vector_cleanup ORDER BY space,paper_id LIMIT 50').fetchall()
        if not pending:
            return 0
        name = pending[0]['space']
        ids = [r['paper_id'] for r in pending if r['space'] == name]
        # A later successful regeneration can clear its queued invalidation.
        ids = [r[0] for r in db.execute('SELECT q.paper_id FROM vector_cleanup q LEFT JOIN papers p ON p.id=q.paper_id WHERE q.space=? AND q.paper_id IN (' + ','.join('?' for _ in ids) + ') AND (p.id IS NULL OR p.embedding IS NULL)', [name,*ids])]
        if ids and path(name).exists():
            with writer(name) as vectors:
                vectors.executemany('DELETE FROM papers_vec WHERE paper_id=?', [(i,) for i in ids])
                vectors.executemany('DELETE FROM paper_vectors WHERE paper_id=?', [(i,) for i in ids])
                vectors.execute("UPDATE metadata SET value=value+1 WHERE name='revision'")
        # A source edit queued after the initial read must survive this acknowledgement.
        db.executemany('DELETE FROM vector_cleanup WHERE space=? AND paper_id=? AND revision=?', [(name,r['paper_id'],r['revision']) for r in pending if r['space']==name])
    return len(ids)


def clean_retired():
    from .db import connect
    cutoff = (datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
    with connect() as db:
        live = active(db)
        pending = db.execute("SELECT value FROM app_settings WHERE name='embedding_rebuild'").fetchone()
        building = json.loads(pending[0]).get('vector_file') if pending else None
        protected=[name for name in (live['name'] if live else None,building) if name]
        old = db.execute('SELECT name FROM vector_spaces WHERE retired_at<?'+(' AND name NOT IN ('+','.join('?' for _ in protected)+')' if protected else '')+' LIMIT 1', [cutoff,*protected]).fetchone()
    if not old or old['name'] in (live['name'] if live else None, building):
        return
    key=(str(settings().data_dir.resolve()),old['name'])
    with _lock:
        entry=_readers.get(key)
        if entry and entry.users:return
        _readers.pop(key,None)
    if entry:entry.db.close()
    with _write_lock:
        if connection:=_writers.pop(key,None):connection.close()
    target = path(old['name'])
    try:
        for suffix in ('-wal','-shm',''):
            target.with_name(target.name + suffix).unlink(missing_ok=True)
    except OSError:
        return
    with connect() as db:
        db.execute('DELETE FROM vector_cleanup WHERE space=?', (old['name'],))
        db.execute('DELETE FROM vector_publications WHERE space=?',(old['name'],))
        db.execute('DELETE FROM vector_spaces WHERE name=?', (old['name'],))


def legacy_cleanup_batch():
    """Retire old blobs/index incrementally after the independent copy is live.

    Only obsolete vector storage is touched. Normalizing a legacy blob to its
    readiness marker does not change scores, alerts, counts or paper lifetime.
    """
    from .db import connect
    from .background_load import delay
    if delay()>.02:
        return 0
    with connect(background=True) as db:
        if not active(db) or db.execute("SELECT 1 FROM app_migrations WHERE name='independent_vector_legacy_cleaned'").fetchone():
            return 0
        saved=db.execute("SELECT value FROM app_settings WHERE name='vector_legacy_cursor'").fetchone()
        cursor=int(saved[0]) if saved else 0
        batch=db.execute('SELECT id FROM papers WHERE id>? AND length(embedding)>1 ORDER BY id LIMIT 50',(cursor,)).fetchall()
        if batch:
            db.executemany('UPDATE papers SET embedding=? WHERE id=?',[(READY,r['id']) for r in batch])
            from .db import dumps
            db.execute("INSERT INTO app_settings VALUES('vector_legacy_cursor',?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(str(batch[-1]['id']),now()))
            return len(batch)
        # Obsolete index rows are grouped by vec0's 1024-slot storage chunks;
        # this is not a change to the 50-paper expiry batch size.
        old=db.execute('SELECT paper_id FROM papers_vec LIMIT 1024').fetchall()
        if old:
            db.executemany('DELETE FROM papers_vec WHERE paper_id=?',[(r[0],) for r in old])
            return len(old)
        db.execute("INSERT OR IGNORE INTO app_migrations VALUES('independent_vector_legacy_cleaned',?)",(now(),))
        return 0


def profile_cleanup_batch():
    """Staged profiles are private, disposable after activation, or when their owner disappears."""
    from .db import connect
    with connect() as db:
        live=active(db)
        pending=db.execute("SELECT value FROM app_settings WHERE name='embedding_rebuild'").fetchone()
        building=json.loads(pending[0]).get('vector_file') if pending else None
        spaces=[r[0] for r in db.execute('SELECT name FROM vector_spaces')]
    for name in spaces:
        if not path(name).exists():continue
        with writer(name) as vectors:
            if name==building:
                vectors.execute('ATTACH DATABASE ? AS business',((settings().data_dir/'papers.sqlite3').resolve().as_uri()+'?mode=ro',))
                try:
                    found=vectors.execute('SELECT profile_id FROM profile_vectors WHERE NOT EXISTS(SELECT 1 FROM business.interest_profile p WHERE p.id=profile_vectors.profile_id) LIMIT 50').fetchall()
                finally:vectors.execute('DETACH DATABASE business')
            else:
                found=vectors.execute('SELECT profile_id FROM profile_vectors LIMIT 50').fetchall()
            if not found:continue
            ids=[r[0] for r in found]
            if ids:
                vectors.executemany('DELETE FROM profile_vectors WHERE profile_id=?',[(i,) for i in ids])
                if vectors.execute("SELECT 1 FROM sqlite_master WHERE name='profile_vector_parts'").fetchone():
                    vectors.executemany('DELETE FROM profile_vector_parts WHERE profile_id=?',[(i,) for i in ids])
                return len(ids)
    return 0


async def maintain():
    import asyncio
    from .background_load import yield_to_web
    while True:
        try:
            if not active():
                await asyncio.to_thread(ensure)
            await asyncio.to_thread(repair_publications)
            count = await asyncio.to_thread(cleanup_batch)
            if not count:
                await asyncio.to_thread(clean_retired)
                count=await asyncio.to_thread(profile_cleanup_batch)
            if not count:
                count=await asyncio.to_thread(legacy_cleanup_batch)
        except Exception as error:
            from .logs import event
            event('task', '向量清理暂缓，将自动重试', level='warning', error_type=type(error).__name__)
            count = 0
        await yield_to_web(.05 if count else 2)


atexit.register(close)
