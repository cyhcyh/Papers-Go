"""Reusable taxonomy embeddings using the configured embedding model and sqlite-vec."""
import asyncio
from contextlib import contextmanager
import json
import math
import re
import sqlite3
import weakref
from .config import settings
from .db import pack
from .llm import runtime as models
from .logs import event

_locks=weakref.WeakKeyDictionary()
_ready={}


@contextmanager
def cache():
    path=settings().data_dir/'topic_candidates.sqlite3'
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=30)
    db.row_factory=sqlite3.Row
    import sqlite_vec
    db.enable_load_extension(True);sqlite_vec.load(db);db.enable_load_extension(False)
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback();raise
    finally:db.close()


def index_entries():
    from .standard_topics import catalog
    return list(catalog().values())


def entry_text(entry):
    return entry['label']+'\nResearch discipline: '+entry['discipline']+'\nDescription: '+entry['description']


def paper_text(paper):
    # Dense formula notation can overwhelm the prose signal. Classification
    # evidence still uses the original text; only the retrieval query is cleaned.
    text=paper['title']+'\n'+(paper.get('abstract') or '')
    text=re.sub(r'\$\$.*?\$\$|\$[^$]*\$|\\\[.*?\\\]|\\\(.*?\\\)', ' ',text,flags=re.S)
    text=re.sub(r'\\(?:textbf|textit|emph|textrm|text)\{([^{}]*)\}',r'\1',text)
    from .source_catalog import official_categories
    source=next((e for e in official_categories() if e['code']==paper.get('primary_category')),None)
    if source:text='Research area: '+source['label']+'\n'+text
    return text


async def ensure_index():
    loop=asyncio.get_running_loop()
    lock=_locks.setdefault(loop,asyncio.Lock())
    async with lock:
        from .standard_topics import revision
        from .db import connect
        with connect() as source:version=revision(source)
        identity=json.dumps(models.embedding_identity(models.configuration()))
        path=str((settings().data_dir/'topic_candidates.sqlite3').resolve())
        if _ready.get(path,())[:2]==(identity,version) and (settings().data_dir/'topic_candidates.sqlite3').is_file():
            return _ready[path][2]
        entries=index_entries();dimension=models.vector_dimension()
        with cache() as db:
            db.execute('CREATE TABLE IF NOT EXISTS metadata(name TEXT PRIMARY KEY,value TEXT)')
            saved=db.execute("SELECT value FROM metadata WHERE name='identity'").fetchone()
            if not saved or saved['value']!=identity:
                for table in ('entries','ccs_vec','msc_vec','area_vec'):db.execute('DROP TABLE IF EXISTS '+table)
                db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('identity',identity))
            db.execute('CREATE TABLE IF NOT EXISTS entries(id INTEGER PRIMARY KEY,standard_key TEXT UNIQUE,system TEXT,text TEXT)')
            for table in ('area_vec',):
                db.execute(f'CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0(embedding float[{dimension}])')
            # Replace old standard entries; retain unchanged research-area vectors.
            valid={e['key'] for e in entries}
            for old in db.execute('SELECT id,standard_key FROM entries').fetchall():
                if old['standard_key'] not in valid:
                    db.execute('DELETE FROM area_vec WHERE rowid=?',(old['id'],))
                    db.execute('DELETE FROM entries WHERE id=?',(old['id'],))
            for table in ('ccs_vec','msc_vec'):db.execute('DROP TABLE IF EXISTS '+table)
            saved={r['standard_key']:r['text'] for r in db.execute('SELECT standard_key,text FROM entries')}
        missing=[e for e in entries if saved.get(e['key'])!=entry_text(e)]
        if missing:event('topic','更新研究方向语义索引',job='classify',total=len(entries),pending=len(missing))
        for start in range(0,len(missing),32):
            batch=missing[start:start+32]
            from .llm.embedding_queue import background
            async with background():
                vectors=await models.embed([entry_text(e) for e in batch])
            if len(vectors)!=len(batch) or any(len(v)!=dimension or not all(math.isfinite(x) for x in v) or not any(v) for v in vectors):
                raise ValueError('研究方向向量数量、维度或数值无效')
            with cache() as db:
                for entry,vector in zip(batch,vectors):
                    norm=math.sqrt(sum(x*x for x in vector));blob=pack([x/norm for x in vector])
                    old=db.execute('SELECT id FROM entries WHERE standard_key=?',(entry['key'],)).fetchone()
                    if old:
                        ident=old['id'];db.execute('UPDATE entries SET text=?,system=? WHERE id=?',(entry_text(entry),entry['system'],ident))
                    else:ident=db.execute('INSERT INTO entries(standard_key,system,text) VALUES(?,?,?)',(entry['key'],entry['system'],entry_text(entry))).lastrowid
                    table='area_vec'
                    db.execute(f'DELETE FROM {table} WHERE rowid=?',(ident,))
                    db.execute(f'INSERT INTO {table}(rowid,embedding) VALUES(?,?)',(ident,blob))
            if (start//32)%10==0 or start+32>=len(missing):
                event('topic','研究方向语义索引进度',job='classify',completed=min(start+32,len(missing)),total=len(missing))
        _ready[path]=(identity,version,len(entries))
        return len(entries)


async def semantic_candidates(paper,blocked=(),limit=40):
    from .standard_topics import candidates
    options=candidates(paper,blocked,limit)
    if not options:return []
    await ensure_index()
    vectors=await models.embed([paper_text(paper)])
    if len(vectors)!=1 or len(vectors[0])!=settings().embedding_dim:raise ValueError('论文分类向量维度无效')
    vector=vectors[0];norm=math.sqrt(sum(x*x for x in vector))
    if not norm or not all(math.isfinite(x) for x in vector):raise ValueError('论文分类向量数值无效')
    table='area_vec'
    with cache() as db:
        neighbors=db.execute(f'SELECT e.standard_key,v.distance FROM {table} v JOIN entries e ON e.id=v.rowid WHERE v.embedding MATCH ? AND k=200 ORDER BY v.distance',(pack([x/norm for x in vector]),)).fetchall()
    scores={r['standard_key']:1-r['distance']**2/2 for r in neighbors}
    return candidates(paper,blocked,limit,semantic_scores=scores)
