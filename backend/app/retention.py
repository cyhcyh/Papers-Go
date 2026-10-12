"""Bounded background retention. Business data and active work are never evicted."""
import asyncio
import sqlite3
from datetime import datetime, timedelta

from .config import now
from .db import connect
from .background_load import yield_to_web
from . import usage_history

BATCH_SIZE = 500
SKILL_HISTORY = 5
REDO_HISTORY = 3
USAGE_DAYS, USAGE_LIMIT, USAGE_SUMMARY_DAYS = 30, 100_000, 365
AUDIT_DAYS, AUDIT_LIMIT = 180, 100_000
COMMAND_DAYS, COMMAND_LIMIT = 30, 1_000
DELETION_DAYS, DELETION_LIMIT = 30, 100
TREND_CACHE_DAYS, TREND_CACHE_LIMIT = 30, 1_000
RESPONSE_CACHE_LIMIT = 1_000
RETIRED_SOURCE_DAYS = 365
ORPHAN_CACHE_DAYS = 365


def initialize(db):
    db.executescript(usage_history.SCHEMA)
    db.executescript('''
    CREATE TABLE IF NOT EXISTS skill_bundle_cleanup (
      skill_id TEXT NOT NULL,version_id TEXT PRIMARY KEY,retry_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_skill_cleanup_retry ON skill_bundle_cleanup(retry_at);
    CREATE TABLE IF NOT EXISTS retention_cursors(name TEXT PRIMARY KEY,stamp TEXT NOT NULL,item TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_retired_sources_date ON retired_paper_sources(retired_at,source_id);
    CREATE INDEX IF NOT EXISTS idx_audit_date ON audit_log(created_at,id);
    CREATE INDEX IF NOT EXISTS idx_commands_retention ON pipeline_commands(created_at,id) WHERE status IN ('done','failed','interrupted');
    CREATE INDEX IF NOT EXISTS idx_commands_redo ON pipeline_commands(redo_id,status);
    CREATE INDEX IF NOT EXISTS idx_deletion_retention ON source_deletion_jobs(status,completed_at,id);
    CREATE INDEX IF NOT EXISTS idx_redo_history ON pipeline_redo_runs(name,id DESC);
    CREATE INDEX IF NOT EXISTS idx_trend_cache_date ON trend_reference_cache(fetched_at,scope_key);
    CREATE INDEX IF NOT EXISTS idx_llm_cache_expiry ON llm_cache(expires_at,key);
    CREATE INDEX IF NOT EXISTS idx_author_cache_date ON author_impact_cache(fetched_at,author_id);
    CREATE INDEX IF NOT EXISTS idx_author_alias_target ON author_identity_aliases(author_id);
    CREATE INDEX IF NOT EXISTS idx_watch_author ON watches(author_id) WHERE active=1 AND type='author';
    CREATE INDEX IF NOT EXISTS idx_watch_author_name ON watches(value COLLATE NOCASE) WHERE active=1 AND type='author';
    CREATE INDEX IF NOT EXISTS idx_repo_cache_date ON community_repos(checked_at,repo);
    ''')


def before(stamp, days):
    return (datetime.fromisoformat(stamp)-timedelta(days=days)).isoformat()


def candidates(db, table, key, date, cutoff, cap=None, where='1', args=()):
    # The boundary lookup reads at most cap index entries, not all excess rows.
    condition = date+'<?'
    values = [*args, cutoff]
    if cap is not None:
        condition += f''' OR ({date},{key}) < (SELECT {date},{key} FROM {table}
            WHERE {where} ORDER BY {date} DESC,{key} DESC LIMIT 1 OFFSET ?)'''
        values.extend([*args, cap-1])
    return db.execute(f'''SELECT {key} FROM {table} WHERE ({where}) AND ({condition})
        ORDER BY {date},{key} LIMIT ?''', [*values, BATCH_SIZE]).fetchall()


def prune(table, key, date, cutoff, cap=None, where='1', args=()):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        ids = candidates(db, table, key, date, cutoff, cap, where, args)
        db.executemany(f'DELETE FROM {table} WHERE {key}=?', [(r[0],) for r in ids])
    return len(ids)


def usage(stamp):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        ids = candidates(db, 'llm_usage', 'id', 'created_at', before(stamp, USAGE_DAYS), USAGE_LIMIT)
        records = [db.execute('SELECT * FROM llm_usage WHERE id=?', (r[0],)).fetchone() for r in ids]
        usage_history.archive(db, records)
        db.executemany('DELETE FROM llm_usage WHERE id=?', [(r[0],) for r in ids])
    return len(ids)


def skill_versions(stamp):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        expired=db.execute('''DELETE FROM agent_skill_leases WHERE rowid IN
            (SELECT rowid FROM agent_skill_leases WHERE expires_at<=? ORDER BY expires_at LIMIT ?)''',
            (stamp,BATCH_SIZE)).rowcount
        old = db.execute('''SELECT v.id,v.skill_id FROM agent_skill_versions v
            JOIN agent_skills s ON s.id=v.skill_id WHERE v.id!=s.version_id
            AND v.revision < (SELECT revision FROM agent_skill_versions n
                WHERE n.skill_id=v.skill_id AND n.id!=s.version_id ORDER BY revision DESC LIMIT 1 OFFSET ?)
            AND NOT EXISTS(SELECT 1 FROM agent_skill_leases l WHERE l.version_id=v.id)
            ORDER BY v.skill_id,v.revision LIMIT ?''', (SKILL_HISTORY-1, BATCH_SIZE)).fetchall()
        db.executemany('INSERT OR IGNORE INTO skill_bundle_cleanup VALUES(?,?,?)',
                       [(r['skill_id'], r['id'], stamp) for r in old])
        db.executemany('DELETE FROM agent_skill_versions WHERE id=?', [(r['id'],) for r in old])
    return BATCH_SIZE if expired==BATCH_SIZE else len(old)


def skill_files(stamp):
    from . import agent_skills
    with connect() as db:
        batch = db.execute('SELECT * FROM skill_bundle_cleanup WHERE retry_at<=? ORDER BY retry_at LIMIT 20', (stamp,)).fetchall()
    for row in batch:
        try:
            agent_skills.remove_version_files(row['skill_id'], row['version_id'])
        except (OSError, ValueError):
            with connect() as db:
                db.execute('UPDATE skill_bundle_cleanup SET retry_at=? WHERE version_id=?',
                           ((datetime.fromisoformat(stamp)+timedelta(hours=1)).isoformat(), row['version_id']))
        else:
            with connect() as db:
                db.execute('DELETE FROM skill_bundle_cleanup WHERE version_id=?', (row['version_id'],))
    return BATCH_SIZE if len(batch)==20 else len(batch)


def redo_runs(stamp):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('''SELECT r.id FROM pipeline_redo_runs r
            WHERE r.status IN ('completed','failed','stopped')
            AND r.id < (SELECT n.id FROM pipeline_redo_runs n WHERE n.name=r.name
                AND n.status IN ('completed','failed','stopped') ORDER BY n.id DESC LIMIT 1 OFFSET ?)
            AND NOT EXISTS(SELECT 1 FROM pipeline_commands c WHERE c.redo_id=r.id AND c.status IN ('queued','processing'))
            ORDER BY r.id LIMIT 1''', (REDO_HISTORY-1,)).fetchone()
        if not old:
            return 0
        db.execute('''DELETE FROM pipeline_redo_items WHERE id IN
            (SELECT id FROM pipeline_redo_items WHERE run_id=? ORDER BY id LIMIT ?)''', (old['id'], BATCH_SIZE))
        if not db.execute('SELECT 1 FROM pipeline_redo_items WHERE run_id=? LIMIT 1', (old['id'],)).fetchone():
            db.execute('DELETE FROM pipeline_redo_runs WHERE id=?', (old['id'],))
    return BATCH_SIZE  # Continue later, including runs with no remaining items.


def author_cache(stamp):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        cursor=db.execute("SELECT stamp,item FROM retention_cursors WHERE name='author_cache'").fetchone()
        position=tuple(cursor) if cursor else ('','')
        batch=db.execute('''SELECT author_id,fetched_at FROM author_impact_cache WHERE fetched_at<?
            AND (fetched_at,author_id)>(?,?) ORDER BY fetched_at,author_id LIMIT ?''',
            (before(stamp,ORPHAN_CACHE_DAYS),*position,BATCH_SIZE)).fetchall()
        placeholders=','.join('?' for _ in batch)
        deleted=db.execute('DELETE FROM author_impact_cache WHERE author_id IN ('+placeholders+') AND '+'''
        NOT EXISTS(SELECT 1 FROM paper_author_links l WHERE l.author_id=author_impact_cache.author_id)
        AND NOT EXISTS(SELECT 1 FROM watches w WHERE w.active=1 AND w.type='author'
            AND (w.author_id=author_impact_cache.author_id OR w.value=author_impact_cache.name COLLATE NOCASE))
        AND NOT EXISTS(SELECT 1 FROM author_identity_aliases a WHERE a.author_id=author_impact_cache.author_id OR a.fragment_id=author_impact_cache.author_id)
        AND NOT EXISTS(SELECT 1 FROM author_query_failures f WHERE f.author_id=author_impact_cache.author_id)''',
        [row['author_id'] for row in batch]).rowcount if batch else 0
        position=(batch[-1]['fetched_at'],batch[-1]['author_id']) if len(batch)==BATCH_SIZE else ('','')
        db.execute("INSERT INTO retention_cursors VALUES('author_cache',?,?) ON CONFLICT(name) DO UPDATE SET stamp=excluded.stamp,item=excluded.item",position)
    return BATCH_SIZE if len(batch)==BATCH_SIZE else deleted


def repository_cache(stamp):
    # Use the existing trigram index. Until its upgrade backfill finishes, skip
    # eviction rather than scanning every abstract or risking a false orphan.
    from .paper_index import MIGRATION, search_clause
    with connect() as db:
        if not db.execute('SELECT 1 FROM app_migrations WHERE name=?', (MIGRATION,)).fetchone():
            return 0
        cursor=db.execute("SELECT stamp,item FROM retention_cursors WHERE name='repository_cache'").fetchone()
        position=tuple(cursor) if cursor else ('','')
        batch = db.execute('''SELECT repo,checked_at FROM community_repos WHERE checked_at<?
            AND (checked_at,repo)>(?,?) ORDER BY checked_at,repo LIMIT 50''',
                           (before(stamp, ORPHAN_CACHE_DAYS), *position)).fetchall()
    removed = 0
    for row in batch:
        repo = row['repo']
        clause, args = search_clause('github.com/'+repo)
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM papers p WHERE '+clause+' LIMIT 1', args).fetchone():
                removed += db.execute('DELETE FROM community_repos WHERE repo=? AND checked_at<?',
                                      (repo, before(stamp, ORPHAN_CACHE_DAYS))).rowcount
    with connect() as db:
        position=(batch[-1]['checked_at'],batch[-1]['repo']) if len(batch)==50 else ('','')
        db.execute("INSERT INTO retention_cursors VALUES('repository_cache',?,?) ON CONFLICT(name) DO UPDATE SET stamp=excluded.stamp,item=excluded.item",position)
    return BATCH_SIZE if len(batch)==50 else removed


def phases(stamp):
    return [
        lambda: skill_versions(stamp), lambda: skill_files(stamp), lambda: redo_runs(stamp),
        lambda: prune('pipeline_commands','id','created_at',before(stamp,COMMAND_DAYS),COMMAND_LIMIT,
                      "status IN ('done','failed','interrupted')"),
        lambda: prune('source_deletion_jobs','id','completed_at',before(stamp,DELETION_DAYS),DELETION_LIMIT,
                      "status='done' AND NOT EXISTS(SELECT 1 FROM source_deletion_items i WHERE i.job_id=source_deletion_jobs.id) AND NOT EXISTS(SELECT 1 FROM source_deletion_sources s WHERE s.job_id=source_deletion_jobs.id)"),
        lambda: usage(stamp),
        lambda: prune('llm_usage_daily','rowid','day',before(stamp,USAGE_SUMMARY_DAYS)[:10]),
        lambda: prune('audit_log','id','created_at',before(stamp,AUDIT_DAYS),AUDIT_LIMIT),
        lambda: prune('retired_paper_sources','source_id','retired_at',before(stamp,RETIRED_SOURCE_DAYS)),
        lambda: prune('trend_reference_cache','scope_key','fetched_at',before(stamp,TREND_CACHE_DAYS),TREND_CACHE_LIMIT),
        lambda: prune('llm_cache','key','expires_at',int(datetime.fromisoformat(stamp).timestamp()),RESPONSE_CACHE_LIMIT),
        lambda: author_cache(stamp), lambda: repository_cache(stamp),
    ]


async def maintain():
    # No cleanup on HTTP requests or during schema migration. Upgrade backlogs
    # receive small batches once a minute, otherwise maintenance runs hourly.
    await asyncio.sleep(60)
    while True:
        backlog = False
        for operation in phases(now()):
            task = asyncio.create_task(asyncio.to_thread(operation))
            try:
                backlog |= await asyncio.shield(task) >= BATCH_SIZE
            except asyncio.CancelledError:
                await asyncio.gather(task, return_exceptions=True)
                raise
            except (sqlite3.Error, OSError, ValueError) as error:
                from .logs import event
                event('system','历史数据清理暂未完成，将稍后重试',level='warning',error_type=type(error).__name__)
                backlog = True
            await yield_to_web(.05)
        await asyncio.sleep(60 if backlog else 3600)
