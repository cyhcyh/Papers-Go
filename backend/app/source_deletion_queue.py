"""Durable source deletion, with bounded business transactions and cache cleanup."""
import asyncio
import json
from fastapi import HTTPException
from .config import now, settings
from .db import connect, rows, one, dumps
from .logs import event
from .source_catalog import invalidate
from .source_deletion import (source_keys, deletion_plan_many, apply_papers,
                              update_topics, clean_profile, finish_sources, remove_pdf)

BATCH_SIZE = 100


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS source_deletion_jobs(
      id INTEGER PRIMARY KEY AUTOINCREMENT,source_keys TEXT NOT NULL,
      source_info TEXT NOT NULL,actor_id INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'queued',
      phase TEXT NOT NULL DEFAULT 'preparing',total INTEGER NOT NULL,processed INTEGER NOT NULL DEFAULT 0,
      deleted_papers INTEGER NOT NULL DEFAULT 0,kept_papers INTEGER NOT NULL DEFAULT 0,
      removed_pdf_files INTEGER NOT NULL DEFAULT 0,profile_cursor INTEGER NOT NULL DEFAULT 0,
      unassigned_topics TEXT NOT NULL DEFAULT '[]',error TEXT,
      created_at TEXT NOT NULL,updated_at TEXT NOT NULL,completed_at TEXT);
    CREATE INDEX IF NOT EXISTS idx_source_deletion_status ON source_deletion_jobs(status,id);
    CREATE TABLE IF NOT EXISTS source_deletion_sources(
      key TEXT PRIMARY KEY,job_id INTEGER NOT NULL REFERENCES source_deletion_jobs(id));
    CREATE TABLE IF NOT EXISTS source_deletion_items(
      job_id INTEGER NOT NULL REFERENCES source_deletion_jobs(id),paper_id INTEGER NOT NULL,
      state TEXT NOT NULL DEFAULT 'pending',pdf_path TEXT,outcome TEXT,PRIMARY KEY(job_id,paper_id));
    CREATE INDEX IF NOT EXISTS idx_source_deletion_items ON source_deletion_items(job_id,state,paper_id);
    CREATE INDEX IF NOT EXISTS idx_interactions_paper ON interactions(paper_id);
    CREATE INDEX IF NOT EXISTS idx_interactions_target ON interactions(target_id);
    CREATE INDEX IF NOT EXISTS idx_user_paper_state_paper ON user_paper_state(paper_id);
    CREATE INDEX IF NOT EXISTS idx_notifications_paper ON notifications(paper_id);
    CREATE INDEX IF NOT EXISTS idx_pending_topic ON topic_pending_papers(topic_id,paper_id);
    ''')


def public(job):
    result={key:job[key] for key in ('id','status','phase','total','processed','deleted_papers',
                                    'kept_papers','removed_pdf_files','error','created_at',
                                    'updated_at','completed_at')}
    result['sources']=[{'key':s['key'],'code':s['code'],'label':s['label']} for s in json.loads(job['source_info'])]
    return result


def list_jobs():
    return [public(job) for job in rows("SELECT * FROM source_deletion_jobs WHERE status!='done' UNION ALL SELECT * FROM (SELECT * FROM source_deletion_jobs WHERE status='done' ORDER BY id DESC LIMIT 3) ORDER BY id DESC")]


def enqueue(keys, actor_id, expected):
    keys=source_keys(keys)
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        existing=db.execute("SELECT * FROM source_deletion_jobs WHERE status!='done' ORDER BY id LIMIT 1").fetchone()
        if existing:
            if json.loads(existing['source_keys'])==keys:return public(dict(existing))
            raise HTTPException(409,'已有分类删除任务，请等待完成或先重试失败的任务')
        sources,delete,keep=deletion_plan_many(db,keys)
        confirmed=(len(sources)==1 and sources[0]['code']==expected.confirm_code) if hasattr(expected,'confirm_code') else expected.confirm_text==f'删除 {len(sources)} 个分类'
        if not confirmed or len(delete)!=expected.delete_papers or len(keep)!=expected.keep_papers:
            raise HTTPException(409,'分类或论文数量已变化，请刷新删除预览后重新确认')
        stamp=now()
        ident=db.execute('INSERT INTO source_deletion_jobs(source_keys,source_info,actor_id,total,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                         (dumps(keys),dumps(sources),actor_id,len(delete)+len(keep),stamp,stamp)).lastrowid
        db.executemany('INSERT INTO source_deletion_sources VALUES(?,?)',[(key,ident) for key in keys])
        # In-flight fetch responses also check these flags before inserting.
        db.execute('UPDATE source_categories SET enabled=0,fetch_enabled=0,guest_default=0 WHERE key IN (SELECT key FROM removed_sources)')
        job=dict(db.execute('SELECT * FROM source_deletion_jobs WHERE id=?',(ident,)).fetchone())
    invalidate()
    event('admin','分类删除任务已提交',user_id=actor_id,job='source_deletion',deletion_id=ident,
          source_keys=keys,delete_papers=len(delete),keep_papers=len(keep))
    return public(job)


def retry(ident):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        job=db.execute('SELECT * FROM source_deletion_jobs WHERE id=?',(ident,)).fetchone()
        if not job:raise HTTPException(404,'删除任务不存在')
        if job['status']=='failed':
            db.execute("UPDATE source_deletion_jobs SET status='queued',error=NULL,updated_at=? WHERE id=?",(now(),ident))
        return public(dict(db.execute('SELECT * FROM source_deletion_jobs WHERE id=?',(ident,)).fetchone()))


def recover():
    with connect() as db:
        db.execute("UPDATE source_deletion_jobs SET status='queued',updated_at=? WHERE status='running'",(now(),))


def _schedule_items(db,job,delete,keep):
    papers=delete+keep
    if not papers:return False
    # Newly arrived cross-category papers are reconsidered before finalization.
    # A previously retained paper may have been updated by another source.
    states={row['paper_id']:row['state'] for row in db.execute('SELECT paper_id,state FROM source_deletion_items WHERE job_id=?',(job['id'],))}
    repeated=sum(states.get(paper['id'])=='done' for paper in papers)
    db.executemany("INSERT INTO source_deletion_items(job_id,paper_id) VALUES(?,?) ON CONFLICT(job_id,paper_id) DO UPDATE SET state='pending',pdf_path=NULL",[(job['id'],paper['id']) for paper in papers])
    total=db.execute('SELECT COUNT(*) FROM source_deletion_items WHERE job_id=?',(job['id'],)).fetchone()[0]
    db.execute("UPDATE source_deletion_jobs SET phase='papers',total=?,processed=MAX(0,processed-?),updated_at=? WHERE id=?",(total,repeated,now(),job['id']))
    return True


def step(ident):
    """Commit one bounded step. Return deleted IDs to cancel local reading calls."""
    job=one('SELECT * FROM source_deletion_jobs WHERE id=?',(ident,))
    if not job or job['status'] not in ('queued','running'):return []
    # File deletion runs outside a business write transaction. Its paths and
    # pending state were committed together with the corresponding paper delete.
    if files:=rows("SELECT paper_id,pdf_path FROM source_deletion_items WHERE job_id=? AND state='files' ORDER BY paper_id LIMIT ?",(ident,BATCH_SIZE)):
        removed=0
        root=(settings().data_dir/'pdf').resolve()
        if root.is_dir():
            for item in files:removed+=remove_pdf(item['paper_id'],item['pdf_path'],root)
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.executemany("UPDATE source_deletion_items SET state='done',pdf_path=NULL WHERE job_id=? AND paper_id=?",[(ident,item['paper_id']) for item in files])
            db.execute('UPDATE source_deletion_jobs SET processed=processed+?,removed_pdf_files=removed_pdf_files+?,updated_at=? WHERE id=?',(len(files),removed,now(),ident))
        return []
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        job=dict(db.execute('SELECT * FROM source_deletion_jobs WHERE id=?',(ident,)).fetchone())
        keys=json.loads(job['source_keys']);removed_keys=set(keys)
        db.execute("UPDATE source_deletion_jobs SET status='running',updated_at=? WHERE id=?",(now(),ident))
        if job['phase']=='preparing':
            _,delete,keep=deletion_plan_many(db,keys)
            if not _schedule_items(db,job,delete,keep):
                db.execute("UPDATE source_deletion_jobs SET phase='papers',total=0 WHERE id=?",(ident,))
            return []
        if job['phase']=='profiles':
            profiles=db.execute('SELECT * FROM interest_profile WHERE user_id>? AND id IN (SELECT MAX(id) FROM interest_profile GROUP BY user_id) ORDER BY user_id LIMIT 50',(job['profile_cursor'],)).fetchall()
            for profile in profiles:clean_profile(db,profile,removed_keys,set(json.loads(job['unassigned_topics'])))
            if profiles:
                db.execute('UPDATE source_deletion_jobs SET profile_cursor=?,updated_at=? WHERE id=?',(profiles[-1]['user_id'],now(),ident))
            else:db.execute("UPDATE source_deletion_jobs SET phase='finalizing' WHERE id=?",(ident,))
            return []
        batch=db.execute("SELECT paper_id,outcome FROM source_deletion_items WHERE job_id=? AND state='pending' ORDER BY paper_id LIMIT ?",(ident,BATCH_SIZE)).fetchall()
        if batch:
            ids=[item['paper_id'] for item in batch]
            sources,delete,keep=deletion_plan_many(db,keys,ids)
            apply_papers(db,sources,delete,keep)
            deleted={paper['id'] for paper in delete};retained={paper['id'] for paper in keep}
            outcomes={item['paper_id']:item['outcome'] for item in batch}
            db.executemany("UPDATE source_deletion_items SET state='done',outcome=? WHERE job_id=? AND paper_id=?",[('kept' if paper_id in retained else 'missing',ident,paper_id) for paper_id in ids if paper_id not in deleted])
            db.executemany("UPDATE source_deletion_items SET state='files',outcome='deleted',pdf_path=? WHERE job_id=? AND paper_id=?",[(paper['pdf_path'],ident,paper['id']) for paper in delete])
            deleted_delta=sum(outcomes[paper_id]!='deleted' for paper_id in deleted)-sum(outcomes[paper_id]=='deleted' for paper_id in ids if paper_id not in deleted)
            kept_delta=sum(outcomes[paper_id]!='kept' for paper_id in retained)-sum(outcomes[paper_id]=='kept' for paper_id in ids if paper_id not in retained)
            db.execute('UPDATE source_deletion_jobs SET processed=processed+?,deleted_papers=deleted_papers+?,kept_papers=kept_papers+?,updated_at=? WHERE id=?',
                       (len(ids)-len(delete),deleted_delta,kept_delta,now(),ident))
            return list(deleted)
        sources,delete,keep=deletion_plan_many(db,keys)
        if _schedule_items(db,job,delete,keep):return []
        if job['phase']=='papers':
            unassigned=set(json.loads(job['unassigned_topics']))|update_topics(db,removed_keys)
            db.execute("UPDATE source_deletion_jobs SET phase='profiles',profile_cursor=0,unassigned_topics=?,updated_at=? WHERE id=?",(dumps(sorted(unassigned)),now(),ident))
            return []
        finish_sources(db,sources,refresh_stats=False)
        db.execute('DELETE FROM source_deletion_sources WHERE job_id=?',(ident,))
        db.execute('DELETE FROM source_deletion_items WHERE job_id=?',(ident,))
        db.execute("UPDATE source_deletion_jobs SET status='done',phase='done',processed=total,error=NULL,completed_at=?,updated_at=? WHERE id=?",(now(),now(),ident))
    invalidate()
    job=one('SELECT * FROM source_deletion_jobs WHERE id=?',(ident,))
    event('admin','分类删除任务完成',user_id=job['actor_id'],job='source_deletion',deletion_id=ident,
          source_keys=keys,deleted_papers=job['deleted_papers'],retained_papers=job['kept_papers'],removed_pdf_files=job['removed_pdf_files'])
    return []


def fail(ident,error):
    # Paths and exception payloads can contain private data. Show a bounded
    # diagnosis and retain all committed progress for an administrator's retry.
    message='缓存文件清理失败，请检查文件权限后重试。' if isinstance(error,OSError) else '后台删除失败，请查看日志后重试。'
    with connect() as db:
        db.execute("UPDATE source_deletion_jobs SET status='failed',error=?,updated_at=? WHERE id=?",(message,now(),ident))
    event('admin','分类删除任务失败',level='error',job='source_deletion',deletion_id=ident,error_type=type(error).__name__)


async def serve():
    await asyncio.to_thread(recover)
    while True:
        job=None
        try:
            job=await asyncio.to_thread(one,"SELECT id FROM source_deletion_jobs WHERE status IN ('queued','running') ORDER BY id LIMIT 1")
            if not job:
                await asyncio.sleep(1)
                continue
            # Reuse the existing autocommit read handle to avoid checkpointing
            # the business WAL on every connection close on Windows bind mounts.
            from .vector_store import _retain_business_reader
            await asyncio.to_thread(_retain_business_reader)
            # Shield the thread: shutdown waits for its small transaction before
            # exiting, so a restart never overlaps two consumers for the same step.
            task=asyncio.create_task(asyncio.to_thread(step,job['id']))
            try:deleted=await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            from .pipeline.read import _tasks
            targets=[_tasks[paper_id] for paper_id in deleted if paper_id in _tasks]
            for running in targets:running.cancel()
            if targets:await asyncio.gather(*targets,return_exceptions=True)
        except asyncio.CancelledError:raise
        except Exception as error:
            if job:await asyncio.to_thread(fail,job['id'],error)
            else:
                event('admin','分类删除队列读取失败，将重试',level='error',job='source_deletion',error_type=type(error).__name__)
                await asyncio.sleep(5)
        await asyncio.sleep(.05)
