"""Persistent interest drafts: persist first, activate only after valid vectors exist."""
import asyncio
import base64
import json
import uuid

from ..config import now, settings
from ..db import connect, dumps, unpack
from ..llm import runtime as models
from ..logs import event
from .. import vector_store
from .profile import current, embedding_inputs, profile_embedding, put_profile
from .vectors import ProfileEmbedding, combine, with_parts
from .retrieval_query import query_payload, prompt_revision

SCHEMA = '''CREATE TABLE IF NOT EXISTS interest_update_drafts(
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 revision TEXT NOT NULL, content TEXT NOT NULL, structured TEXT NOT NULL,
 reason TEXT NOT NULL, base_profile_id INTEGER, status TEXT NOT NULL,
 error_type TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS interest_update_queue ON interest_update_drafts(status,created_at);'''
_initialized = set()
_local_slot = asyncio.Semaphore(1)


def initialize_upgrade(db):
    """One-time promotion: preserve live data and queue only current profiles."""
    name='interest-retrieval-v1'
    if db.execute('SELECT 1 FROM app_migrations WHERE name=?',(name,)).fetchone():
        return
    from .. import recommendation_settings
    site=json.loads(db.execute("SELECT value FROM app_settings WHERE name='site'").fetchone()['value'])
    weights=recommendation_settings.configuration(db)
    personal = recommendation_settings.defaults()['personal']
    if weights['personal']['author'] != personal['author']:
        personal = recommendation_settings.with_author(personal, weights['personal']['author'])
    site['recommendation']={'personal':personal,'guest':weights['guest']}
    db.execute("UPDATE app_settings SET value=?,updated_at=? WHERE name='site'",(dumps(site),now()))
    profiles=db.execute('''SELECT p.* FROM interest_profile p JOIN users u ON u.id=p.user_id
        WHERE u.disabled=0 AND p.id IN (SELECT MAX(id) FROM interest_profile GROUP BY user_id)''').fetchall()
    for profile in profiles:
        if not embedding_inputs(profile['content']):continue
        timestamp=now()
        db.execute('''INSERT OR IGNORE INTO interest_update_drafts
            VALUES(?,?,?,?,?,?,?,?,?,?)''',(profile['user_id'],uuid.uuid4().hex,profile['content'],
                profile['structured'],'retrieval_upgrade',profile['id'],'queued',None,timestamp,timestamp))
    db.execute('INSERT INTO app_migrations(name,applied_at) VALUES(?,?)',(name,now()))


def initialize(*, recover=False):
    key = str(settings().data_dir.absolute())
    if key not in _initialized:
        with connect() as db:
            db.executescript(SCHEMA)
        _initialized.add(key)
    if recover:
        with connect() as db:
            db.execute("UPDATE interest_update_drafts SET status='queued',updated_at=? WHERE status='processing'", (now(),))


def latest(user_id, db=None):
    initialize()
    if db is not None:
        row = db.execute('SELECT * FROM interest_update_drafts WHERE user_id=?', (user_id,)).fetchone()
        return dict(row) if row else None
    with connect() as conn:
        return latest(user_id, conn)


def reusable(content, profile, *, require_descriptions=False):
    entries = [{'text': t, 'weight': w} for t, w in embedding_inputs(content)]
    if not entries:
        return True, None
    if not profile or not profile['embedding']:
        return False, None
    old_entries = [{'text': t, 'weight': w} for t, w in embedding_inputs(profile['content'])]
    if query_payload(content, entries) != query_payload(profile['content'], old_entries):
        return False, None
    parts = json.loads(profile.get('embedding_parts') or '[]')
    # Background/category edits remain cheap for pre-upgrade profiles. The
    # one-time conversion explicitly requires the new descriptions instead.
    if not parts:
        if not require_descriptions and embedding_inputs(content) == embedding_inputs(profile['content']) and len(profile['embedding']) == models.vector_dimension()*4:
            return True, with_parts(profile)
        return False, None
    if [p['text'] for p in parts] != [e['text'] for e in entries]:
        return False, None
    revision, epoch = prompt_revision(), vector_store.epoch()
    legacy = all('query_revision' not in p for p in parts)
    if not (legacy and not require_descriptions) and any(p.get('query_revision') != revision or p.get('vector_epoch') != epoch for p in parts):
        return False, None
    weighted = []
    for entry, part in zip(entries, parts):
        vector = unpack(base64.b64decode(part['embedding']))
        if len(vector) != models.vector_dimension():
            return False, None
        weighted.append({'weight': entry['weight'], 'vector': vector})
    # Preserve feedback-adjusted component vectors, update only their weights.
    return True, ProfileEmbedding(combine(weighted), parts)


def state(job):
    if not job:
        return None
    return {k: job[k] for k in ('revision', 'status', 'created_at', 'updated_at')} | {
        'message': '更新失败，原推荐已保留，请重新保存重试。' if job['status'] == 'failed' else ''}


def submit(user_id, content, structured, reason='manual'):
    initialize()
    revision = uuid.uuid4().hex
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM interest_profile WHERE user_id=? ORDER BY version DESC LIMIT 1', (user_id,)).fetchone()
        active = dict(row) if row else None
        reused, vector = reusable(content, active, require_descriptions=reason in ('retrieval_upgrade','rollback'))
        timestamp = now()
        db.execute('''INSERT INTO interest_update_drafts VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET revision=excluded.revision,content=excluded.content,
            structured=excluded.structured,reason=excluded.reason,base_profile_id=excluded.base_profile_id,
            status=excluded.status,error_type=NULL,created_at=excluded.created_at,updated_at=excluded.updated_at''',
            (user_id, revision, content, dumps(structured), reason, active['id'] if active else None,
             'completed' if reused else 'queued', None, timestamp, timestamp))
        result = put_profile(user_id, content, structured, reason, vector, db=db) if reused else {
            'id': active['id'] if active else None, 'version': active['version'] if active else 0}
        return {**result, 'update': state(latest(user_id, db))}


def claim():
    with connect() as db:
        # Idle polling must not take SQLite's write lock from foreground requests.
        if not db.execute("SELECT 1 FROM interest_update_drafts d JOIN users u ON u.id=d.user_id WHERE d.status='queued' AND u.disabled=0 LIMIT 1").fetchone():
            return None
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('''SELECT d.* FROM interest_update_drafts d JOIN users u ON u.id=d.user_id
            WHERE d.status='queued' AND u.disabled=0 ORDER BY d.created_at,d.user_id LIMIT 1''').fetchone()
        if not row:
            return None
        job = dict(row)
        db.execute("UPDATE interest_update_drafts SET status='processing',updated_at=? WHERE user_id=? AND revision=?",
                   (now(), job['user_id'], job['revision']))
        return job


class Superseded(Exception):
    pass


def apply(job, vector, identity, epoch):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        saved_job = latest(job['user_id'], db)
        if not saved_job or saved_job['revision'] != job['revision'] or saved_job['status'] != 'processing':
            return False
        user = db.execute('SELECT disabled FROM users WHERE id=?', (job['user_id'],)).fetchone()
        if not user or user['disabled']:
            raise Superseded('User no longer active')
        row = db.execute('SELECT * FROM interest_profile WHERE user_id=? ORDER BY version DESC LIMIT 1', (job['user_id'],)).fetchone()
        active = dict(row) if row else None
        if (active['id'] if active else None) != job['base_profile_id']:
            raise Superseded('Profile changed during preparation')
        cfg = db.execute("SELECT value FROM app_settings WHERE name='models'").fetchone()
        if cfg:
            from ..llm.secrets import decrypt_configuration
            live = decrypt_configuration(json.loads(cfg['value']))
        else:
            live = models.defaults()
        if models.embedding_identity(live) != identity or vector_store.epoch(db) != epoch:
            raise Superseded('Embedding model changed during preparation')
        # If feedback arrived while preparing an unchanged interest, keep it.
        reused, recent = reusable(job['content'], active, require_descriptions=job['reason'] in ('retrieval_upgrade','rollback'))
        if reused:
            vector = recent
        elif vector is not None and active and embedding_inputs(job['content']) == embedding_inputs(active['content']):
            # A conversion can take a few seconds. Keep feedback submitted in
            # that interval by applying the existing learning rule to the new
            # representation; do not transfer it to changed research interests.
            from .online import update_profile
            feedback = db.execute('''SELECT i.paper_id,i.action FROM interactions i
                WHERE i.profile_id=? AND i.created_at>=? AND i.vector_epoch=?
                AND i.action IN ('like','skip')
                AND NOT EXISTS(SELECT 1 FROM interactions u WHERE u.target_id=i.id AND u.action='undo')
                ORDER BY i.id''', (active['id'],job['created_at'],epoch)).fetchall()
            bundle = {'content':job['content'],'embedding':vector,'embedding_parts':vector.parts_json}
            for interaction in feedback:
                blob,parts,_ = update_profile(bundle,vector_store.get(interaction['paper_id'],db),interaction['action'])
                bundle.update(embedding=blob,embedding_parts=parts)
            if feedback:
                vector = ProfileEmbedding(bundle['embedding'],json.loads(bundle['embedding_parts']))
        if embedding_inputs(job['content']) and vector is None:
            raise ValueError('Missing interest vectors')
        put_profile(job['user_id'], job['content'], json.loads(job['structured']), job['reason'], vector, db=db)
        db.execute("UPDATE interest_update_drafts SET status='completed',error_type=NULL,updated_at=? WHERE user_id=? AND revision=?",
                   (now(), job['user_id'], job['revision']))
    event('profile', '后台兴趣更新完成', user_id=job['user_id'])
    return True


def finish(job, status, error_type=None):
    with connect() as db:
        db.execute("UPDATE interest_update_drafts SET status=?,error_type=?,updated_at=? WHERE user_id=? AND revision=? AND status='processing'",
                   (status, error_type, now(), job['user_id'], job['revision']))


async def process(job):
    async def generate():
        with models.model_snapshot():
            identity, epoch = models.embedding_identity(models.configuration()), vector_store.epoch()
            vector = await profile_embedding(job['content'], strict=True)
            return await asyncio.to_thread(apply, job, vector, identity, epoch)
    task = None
    try:
        # Existing embedding permits still apply; local description calls stay serial.
        local = models.configured('interest_init') and models.selected('interest_init')['kind'] == 'ollama'
        async def run():
            if local:
                async with _local_slot:
                    return await generate()
            return await generate()
        task = asyncio.create_task(asyncio.wait_for(run(), timeout=180))
        while not task.done():
            job_state = await asyncio.to_thread(latest, job['user_id'])
            if not job_state or job_state['revision'] != job['revision']:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return False
            await asyncio.wait({task}, timeout=.5)
        return await task
    except asyncio.CancelledError:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(finish, job, 'queued')
        raise
    except Exception as error:
        await asyncio.to_thread(finish, job, 'failed', type(error).__name__)
        event('profile', '后台兴趣更新失败，保留原推荐', level='warning', user_id=job['user_id'], error_type=type(error).__name__)
        return False


async def serve():
    initialize(recover=True)
    async def consume():
        while True:
            try:
                job = await asyncio.to_thread(claim)
                if job:
                    await process(job)
                else:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                event('profile', '兴趣队列暂时不可用', level='warning', error_type=type(error).__name__)
                await asyncio.sleep(2)
    tasks = [asyncio.create_task(consume()) for _ in range(2)]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
