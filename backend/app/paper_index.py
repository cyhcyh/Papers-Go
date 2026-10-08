"""Substring search and exact author membership, maintained with paper writes."""
import re
import threading
from .db import connect, one
from .config import now

MIGRATION='paper_search_authors_v2'


def initialize(db):
    previous=db.execute("SELECT sql FROM sqlite_master WHERE name='paper_search'").fetchone()
    if previous and "content='papers'" in previous['sql']:
        # Keep search text together: external content caused costly random reads of large paper rows.
        for name in ('paper_search_insert','paper_search_before_update','paper_search_update','paper_search_delete'):
            db.execute('DROP TRIGGER IF EXISTS '+name)
        db.execute('DROP TABLE paper_search')
        db.execute('DELETE FROM paper_search_state')
    db.executescript('''
    CREATE VIRTUAL TABLE IF NOT EXISTS paper_search USING fts5(
      title,abstract,authors,tokenize='trigram',detail='none');
    CREATE TABLE IF NOT EXISTS paper_search_state(
      paper_id INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS paper_names(
      paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,name TEXT NOT NULL,
      PRIMARY KEY(paper_id,name));
    CREATE INDEX IF NOT EXISTS idx_paper_names_author ON paper_names(name,paper_id);
    CREATE INDEX IF NOT EXISTS idx_papers_missing_classification ON papers(id) WHERE classified=0;
    CREATE INDEX IF NOT EXISTS idx_papers_missing_brief ON papers(id) WHERE brief_json IS NULL;
    CREATE INDEX IF NOT EXISTS idx_papers_missing_quality ON papers(id) WHERE scored=0;
    CREATE INDEX IF NOT EXISTS idx_interactions_latest_feedback ON interactions(user_id,id DESC) WHERE action NOT IN ('expand','view');
    CREATE TABLE IF NOT EXISTS community_repos(
      repo TEXT PRIMARY KEY,stars INTEGER,etag TEXT,checked_at TEXT NOT NULL,next_check_at TEXT NOT NULL);
    CREATE TRIGGER IF NOT EXISTS paper_search_insert AFTER INSERT ON papers BEGIN
      INSERT INTO paper_search(rowid,title,abstract,authors) VALUES(NEW.id,NEW.title,NEW.abstract,NEW.authors);
      INSERT INTO paper_search_state VALUES(NEW.id);
      INSERT OR IGNORE INTO paper_names SELECT NEW.id,value FROM json_each(COALESCE(NEW.authors,'[]')) WHERE type='text';
    END;
    CREATE TRIGGER IF NOT EXISTS paper_search_before_update BEFORE UPDATE OF title,abstract,authors ON papers
    WHEN (NEW.title IS NOT OLD.title OR NEW.abstract IS NOT OLD.abstract OR NEW.authors IS NOT OLD.authors)
      AND EXISTS(SELECT 1 FROM paper_search_state WHERE paper_id=OLD.id) BEGIN
      DELETE FROM paper_search WHERE rowid=OLD.id;
    END;
    CREATE TRIGGER IF NOT EXISTS paper_search_update AFTER UPDATE OF title,abstract,authors ON papers
    WHEN NEW.title IS NOT OLD.title OR NEW.abstract IS NOT OLD.abstract OR NEW.authors IS NOT OLD.authors BEGIN
      INSERT INTO paper_search(rowid,title,abstract,authors) VALUES(NEW.id,NEW.title,NEW.abstract,NEW.authors);
      INSERT OR IGNORE INTO paper_search_state VALUES(NEW.id);
      DELETE FROM paper_names WHERE paper_id=NEW.id;
      INSERT OR IGNORE INTO paper_names SELECT NEW.id,value FROM json_each(COALESCE(NEW.authors,'[]')) WHERE type='text';
    END;
    CREATE TRIGGER IF NOT EXISTS paper_search_delete BEFORE DELETE ON papers
    WHEN EXISTS(SELECT 1 FROM paper_search_state WHERE paper_id=OLD.id) BEGIN
      DELETE FROM paper_search WHERE rowid=OLD.id;
    END;
    ''')
    if not db.execute('SELECT 1 FROM papers LIMIT 1').fetchone():
        db.execute('INSERT OR IGNORE INTO app_migrations VALUES(?,?)',(MIGRATION,now()))


def ready():
    return bool(one('SELECT 1 FROM app_migrations WHERE name=?',(MIGRATION,)))


def search_clause(query):
    pattern='%'+query+'%'
    exact='(p.title LIKE ? OR p.abstract LIKE ? OR EXISTS(SELECT 1 FROM json_each(p.authors) a WHERE a.value LIKE ?))'
    args=[pattern]*3
    # Short Chinese names and LIKE wildcards retain the original search semantics.
    grams=list(dict.fromkeys(part[i:i+3] for part in re.split(r'[%_]',query) for i in range(len(part)-2)))
    if grams and ready():
        match=' AND '.join('"'+gram.replace('"','""')+'"' for gram in grams)
        # MATCH reads postings once across columns; LIKE then verifies the original substring rule.
        exact_index='(title LIKE ? OR abstract LIKE ? OR EXISTS(SELECT 1 FROM json_each(paper_search.authors) a WHERE a.value LIKE ?))'
        return 'p.id IN (SELECT rowid FROM paper_search WHERE paper_search MATCH ? AND '+exact_index+')',[match,*args]
    return exact,args


def backfill(stop=None):
    stop=stop or threading.Event()
    if ready():return 0
    processed=0
    while not stop.is_set():
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            batch=db.execute('SELECT id,title,abstract,authors FROM papers WHERE id NOT IN (SELECT paper_id FROM paper_search_state) ORDER BY id LIMIT 250').fetchall()
            if not batch:
                db.execute('INSERT OR IGNORE INTO app_migrations VALUES(?,?)',(MIGRATION,now()))
                return processed
            for p in batch:
                db.execute('INSERT INTO paper_search(rowid,title,abstract,authors) VALUES(?,?,?,?)',tuple(p))
                db.execute('INSERT INTO paper_search_state VALUES(?)',(p['id'],))
                db.execute("INSERT OR IGNORE INTO paper_names SELECT ?,value FROM json_each(COALESCE(?,'[]')) WHERE type='text'",(p['id'],p['authors']))
            processed+=len(batch)
        if stop.wait(.02):break
    return processed
