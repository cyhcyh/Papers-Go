"""Purge only expired papers and their directly related records in short batches."""
import asyncio
import time
from datetime import datetime, timezone

from ..config import now
from ..db import connect, execute, one, dumps
from ..logs import event

BATCH_SIZE = 50
RUN_SECONDS = 30


def purge_batch(stamp, batch_size=None):
    with connect(background=True) as db:
        db.execute('BEGIN IMMEDIATE')
        ids = [r[0] for r in db.execute('''SELECT p.id FROM papers p
            WHERE p.expires_at<=? AND NOT EXISTS(SELECT 1 FROM reading_jobs j WHERE j.paper_id=p.id)
            AND NOT EXISTS(SELECT 1 FROM fulltext_cache_pins f WHERE f.paper_id=p.id AND f.expires_at>?)
            ORDER BY p.expires_at,p.id LIMIT ?''', (stamp, stamp, batch_size or BATCH_SIZE))]
        if not ids:
            return 0
        db.execute('CREATE TEMP TABLE IF NOT EXISTS expired_papers(id INTEGER PRIMARY KEY)')
        db.execute('DELETE FROM expired_papers')
        db.executemany('INSERT INTO expired_papers VALUES(?)', [(i,) for i in ids])
        maximum = db.execute('SELECT MAX(id) FROM papers').fetchone()[0]
        db.execute("""INSERT INTO app_settings VALUES('last_paper_id',?,?) ON CONFLICT(name)
            DO UPDATE SET value=CAST(MAX(CAST(value AS INTEGER),CAST(excluded.value AS INTEGER)) AS TEXT)""",
            (str(maximum), stamp))
        db.execute('''INSERT OR IGNORE INTO retired_paper_sources
            SELECT source_id,? FROM paper_sources WHERE paper_id IN (SELECT id FROM expired_papers)''', (stamp,))
        db.execute('''INSERT OR IGNORE INTO retired_paper_sources
            SELECT arxiv_id,? FROM papers WHERE id IN (SELECT id FROM expired_papers) AND arxiv_id IS NOT NULL''', (stamp,))
        db.execute('''DELETE FROM interactions WHERE target_id IN
            (SELECT id FROM interactions WHERE paper_id IN (SELECT id FROM expired_papers))''')
        for table in ('interactions', 'user_paper_state', 'notifications', 'reading_cards',
                      'paper_topics', 'topic_pending_papers', 'paper_sources', 'paper_categories'):
            db.execute('DELETE FROM ' + table + ' WHERE paper_id IN (SELECT id FROM expired_papers)')
        # vec0 cannot push a subquery IN predicate down to its row-ID lookup.
        from ..vector_store import active
        if not active(db):
            db.executemany('DELETE FROM papers_vec WHERE paper_id=?', [(i,) for i in ids])
        db.execute('DELETE FROM papers WHERE id IN (SELECT id FROM expired_papers)')
        return len(ids)


async def expire_papers():
    from ..task_settings import configuration
    batch_size = configuration()['advanced']['paper_expiry']['batch_size']
    stamp = datetime.fromisoformat(now()).astimezone(timezone.utc).isoformat(timespec='seconds')
    total = one('SELECT COUNT(*) n FROM papers WHERE expires_at<=?', (stamp,))['n']
    completed = 0
    started = time.monotonic()
    def progress():
        execute("""INSERT INTO source_status(name,progress) VALUES('paper_expiry',?)
            ON CONFLICT(name) DO UPDATE SET progress=excluded.progress""",
            (dumps({'total': total, 'completed': completed, 'failed': 0,
                    'pending': max(0, total-completed), 'processed': completed, 'unit': '篇'}),))
    await asyncio.to_thread(progress)
    try:
        while True:
            # Finish a transaction before cancellation releases the pipeline lock.
            batch = asyncio.create_task(asyncio.to_thread(purge_batch, stamp, batch_size))
            try:
                count = await asyncio.shield(batch)
            except asyncio.CancelledError:
                completed += await batch
                raise
            completed += count
            await asyncio.to_thread(progress)
            if not count or time.monotonic()-started >= RUN_SECONDS:
                break
            from ..background_load import yield_to_web
            await yield_to_web(.05)
    finally:
        await asyncio.to_thread(progress)
        event('task', '到期论文清退记录', job='paper_expiry', deleted_papers=completed,
              pending_papers=max(0,total-completed), cutoff=stamp)
    return completed
