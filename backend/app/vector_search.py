"""Bounded, shared KNN reads against the authoritative SQLite vector table."""
import atexit
import sqlite3
import threading
import time
from collections import OrderedDict

from .config import settings

_lock = threading.RLock()
_connection = None
_database = None
_cache = OrderedDict()


def close():
    global _connection, _database
    with _lock:
        if _connection is not None:
            _connection.close()
        _connection = None
        _database = None
        _cache.clear()


def nearest_ids(vector, limit, revision, space=None):
    """Reuse one serialized reader; identical feed/rail searches share results.

    Autocommit ends each read snapshot after fetchall(), so worker writes remain
    visible. The revision and vector bytes invalidate results on data/model or
    profile changes. There is no second copy of the paper vector index.
    """
    global _connection, _database
    from . import vector_store
    space=space if space is not None else vector_store.active()
    cfg = settings()
    path = (vector_store.path(space['name']) if space else cfg.data_dir / 'papers.sqlite3').resolve()
    dimension=space['dimension'] if space else cfg.embedding_dim
    if len(vector)!=dimension*4:
        return []
    database = (str(path), dimension, cfg.sqlite_mmap_mb)
    key = (vector, limit, revision)
    with _lock:
        if database != _database:
            close()
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < cfg.recommendation_cache_seconds:
            _cache.move_to_end(key)
            return list(cached[1])
        if space:
            with vector_store.reader(space['name']) as db:
                version=db.execute("SELECT value FROM metadata WHERE name='revision'").fetchone()[0]
                key=(vector,limit,revision,version)
                cached=_cache.get(key)
                if cached and time.monotonic()-cached[0]<cfg.recommendation_cache_seconds:
                    _cache.move_to_end(key)
                    return list(cached[1])
                ids=tuple(row[0] for row in db.execute('SELECT paper_id FROM papers_vec WHERE embedding MATCH ? AND k=?',(vector,limit)))
            _database=database
        elif _connection is None:
            db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                 isolation_level=None, check_same_thread=False, timeout=30)
            try:
                db.execute('PRAGMA query_only=ON')
                db.execute('PRAGMA busy_timeout=30000')
                db.execute(f'PRAGMA mmap_size={cfg.sqlite_mmap_mb * 1024 * 1024}')
                import sqlite_vec
                db.enable_load_extension(True)
                sqlite_vec.load(db)
                db.enable_load_extension(False)
            except BaseException:
                db.close()
                raise
            _connection, _database = db, database
        if not space:
            try:
                ids = tuple(row[0] for row in _connection.execute(
                    'SELECT paper_id FROM papers_vec WHERE embedding MATCH ? AND k=?',
                    (vector, limit)).fetchall())
            except BaseException:
                close()
                raise
        _cache[key] = (time.monotonic(), ids)
        while len(_cache) > 64:
            _cache.popitem(last=False)
        return list(ids)


atexit.register(close)
