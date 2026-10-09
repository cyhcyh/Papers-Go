"""Build a replacement embedding space without clearing the live index."""
import asyncio
import copy
import json
import uuid
from ..db import connect, one, rows, dumps, pack
from ..config import now, settings
from .secrets import encrypt_configuration, decrypt_configuration
from ..interest.profile import CURRENT_PROFILE_IDS
from .. import vector_store
from ..pipeline_control import check_cancelled

SCHEMA='''
CREATE TABLE IF NOT EXISTS embedding_rebuild_papers (
 paper_id INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
 title TEXT NOT NULL,abstract TEXT NOT NULL,embedding BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS embedding_rebuild_profiles (
 profile_id INTEGER PRIMARY KEY REFERENCES interest_profile(id) ON DELETE CASCADE,
 content TEXT NOT NULL,embedding BLOB);
'''
INDEX_BATCH_SIZE = 1024


def pending():
    row=one("SELECT value FROM app_settings WHERE name='embedding_rebuild'")
    if not row:return None
    value=json.loads(row['value']);value['config']=decrypt_configuration(value['config'])
    return value


def endpoint(connection):
    return connection['kind'],connection['base_url'].rstrip('/')


def remove_unused_retained(config):
    used={v['connection_id'] for route in config['routes'].values()
          for v in (route['primary'],route.get('fallback')) if v}
    config['connections']=[c for c in config['connections'] if not c.get('embedding_retained') or c['id'] in used]


def with_embedding(current,target):
    """Merge only the vector space; preserve newer unrelated administrator settings."""
    result=copy.deepcopy(current)
    result['routes']['embedding']=copy.deepcopy(target['routes']['embedding'])
    result['embedding_dim']=target['embedding_dim']
    ident=target['routes']['embedding']['primary']['connection_id']
    desired=next(c for c in target['connections'] if c['id']==ident)
    existing=next((c for c in result['connections'] if c['id']==ident),None)
    if existing is None:result['connections'].append(copy.deepcopy(desired))
    elif endpoint(existing)!=endpoint(desired):
        shared=any(feature!='embedding' and any(v and v['connection_id']==ident for v in (route['primary'],route.get('fallback')))
                   for feature,route in current['routes'].items())
        if shared:
            # Keep the live embedding space on its old endpoint and credentials.
            # Other routes retain the administrator's updated shared connection.
            retained={**copy.deepcopy(desired),'id':'embedding_previous_'+uuid.uuid4().hex,'embedding_retained':True}
            result['connections'].append(retained)
            result['routes']['embedding']['primary']['connection_id']=retained['id']
        else:
            result['connections']=[copy.deepcopy(desired) if c['id']==ident else c for c in result['connections']]
    remove_unused_retained(result)
    return result


def check_connection(config,value,current):
    ident=value['config']['routes']['embedding']['primary']['connection_id']
    target=next(c for c in value['config']['connections'] if c['id']==ident)
    connection=next((c for c in config['connections'] if c['id']==ident),None)
    active=next((c for c in current['connections'] if c['id']==ident),None)
    expected=endpoint(active) if active else endpoint(target)
    if connection is None or endpoint(connection)!=expected:
        raise ValueError('此连接用于待切换向量，请先完成或取消重建，再删除或修改服务地址与类型')
    if endpoint(connection)!=endpoint(target):
        for feature,route in config['routes'].items():
            if feature!='embedding' and any(v and v['connection_id']==ident for v in (route['primary'],route.get('fallback'))):
                raise ValueError('此连接正在切换向量服务地址或类型，请为其他功能选择独立连接')


def set_pending(config,*,db=None):
    if db is None:
        with connect() as transaction:set_pending(config,db=transaction)
        return
    old=db.execute("SELECT value FROM app_settings WHERE name='embedding_rebuild'").fetchone()
    if old and (name:=json.loads(old[0]).get('vector_file')):
        db.execute('UPDATE vector_spaces SET retired_at=? WHERE name=?',(now(),name))
    db.execute("INSERT INTO app_settings VALUES('embedding_rebuild',?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
               (dumps({'config':encrypt_configuration(config),'status':'queued','error':None,'separate_settings':True,
                       'vector_file':f'vectors-{uuid.uuid4().hex}.sqlite3'}),now()))


def migrate_legacy(db):
    record=db.execute("SELECT value FROM app_settings WHERE name='embedding_rebuild'").fetchone()
    if not record:return
    value=json.loads(record['value'])
    if value.get('separate_settings'):return
    from . import runtime
    active=db.execute("SELECT value FROM app_settings WHERE name='models'").fetchone()
    current=decrypt_configuration(json.loads(active['value'])) if active else runtime.defaults()
    target=decrypt_configuration(value['config'])
    live=with_embedding(target,current)
    db.execute("INSERT INTO app_settings VALUES('models',?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(dumps(encrypt_configuration(live)),now()))
    if runtime.embedding_identity(current)==runtime.embedding_identity(target):
        db.execute("DELETE FROM app_settings WHERE name='embedding_rebuild'")
        db.execute('DELETE FROM embedding_rebuild_papers');db.execute('DELETE FROM embedding_rebuild_profiles')
    else:
        value['separate_settings']=True
        db.execute("UPDATE app_settings SET value=?,updated_at=? WHERE name='embedding_rebuild'",(dumps(value),now()))


def state():
    value=pending()
    if not value:return None
    from .runtime import public_configuration
    return {'status':value['status'],'error':value.get('error'),
            'config':public_configuration(value['config'],include_rebuild=False),
            'completed':_stored_count(value),
            'total':one('SELECT COUNT(*) n FROM papers')['n'],
            'index_completed':value.get('index_completed',0),
            'index_total':value.get('index_total',0)}


def update(status,error=None):
    with connect() as db:
        row=db.execute("SELECT value FROM app_settings WHERE name='embedding_rebuild'").fetchone()
        if row:
            value=json.loads(row['value']);value.update(status=status,error=error)
            db.execute("UPDATE app_settings SET value=?,updated_at=? WHERE name='embedding_rebuild'",(dumps(value),now()))


def cancel():
    with connect() as db:
        old=db.execute("SELECT value FROM app_settings WHERE name='embedding_rebuild'").fetchone()
        if old and (name:=json.loads(old[0]).get('vector_file')):
            db.execute('UPDATE vector_spaces SET retired_at=? WHERE name=?',(now(),name))
        db.execute("DELETE FROM app_settings WHERE name='embedding_rebuild'")


PAPERS="""SELECT p.id,p.title,p.abstract FROM papers p LEFT JOIN vector_build.paper_vectors r ON r.paper_id=p.id
 WHERE r.paper_id IS NULL OR r.title!=p.title OR r.abstract!=COALESCE(p.abstract,'') ORDER BY p.id"""
PROFILES=f"""SELECT p.id,p.content FROM interest_profile p LEFT JOIN vector_build.profile_vectors r ON r.profile_id=p.id
 LEFT JOIN vector_build.profile_vector_parts a ON a.profile_id=p.id
 WHERE p.id IN ({CURRENT_PROFILE_IDS}) AND (r.profile_id IS NULL OR r.content!=p.content OR a.profile_id IS NULL) ORDER BY p.id"""


def _index_state(db, status, completed, total):
    row=db.execute("SELECT value FROM app_settings WHERE name='embedding_rebuild'").fetchone()
    if not row:raise ValueError('向量重建配置已不存在')
    value=json.loads(row['value'])
    value.update(status=status,index_completed=completed,index_total=total,error=None)
    db.execute("UPDATE app_settings SET value=?,updated_at=? WHERE name='embedding_rebuild'",(dumps(value),now()))


def _stored_count(value):
    name=value.get('vector_file')
    if name and vector_store.path(name).exists():
        with vector_store.reader(name) as db:
            return db.execute('SELECT COUNT(*) FROM paper_vectors').fetchone()[0]
    return one('SELECT COUNT(*) n FROM embedding_rebuild_papers')['n'] if not name else 0


def _prepare_storage(config):
    value=pending()
    with connect() as db:
        db.execute(f'DELETE FROM embedding_rebuild_profiles WHERE profile_id NOT IN ({CURRENT_PROFILE_IDS})')
    name=value.get('vector_file') or f'vectors-{uuid.uuid4().hex}.sqlite3'
    vector_store.create(config['embedding_dim'],name)
    if not value.get('vector_file'):
        # Resume work staged by a previous application version without calling models again.
        with vector_store.writer(name) as vectors,connect() as source:
            cursor=source.execute('SELECT paper_id,title,abstract,embedding FROM embedding_rebuild_papers')
            while batch:=cursor.fetchmany(1024):
                vector_store.stage(vectors,[tuple(r) for r in batch]);vectors.commit()
            vectors.executemany('INSERT OR REPLACE INTO profile_vectors VALUES(?,?,?)',
                                [tuple(r) for r in source.execute('SELECT * FROM embedding_rebuild_profiles')])
        with connect() as db:
            record=db.execute("SELECT value FROM app_settings WHERE name='embedding_rebuild'").fetchone()
            data=json.loads(record[0]);data['vector_file']=name
            db.execute("UPDATE app_settings SET value=?,updated_at=? WHERE name='embedding_rebuild'",(dumps(data),now()))
    return name


def _pending_rows(sql,args=()):
    with connect() as db:
        vector_store.attach(db,pending()['vector_file'],'vector_build')
        return [dict(r) for r in db.execute(sql,args)]


def _prepare_index(dim):
    name=pending()['vector_file']
    with vector_store.writer(name) as vectors:
        vectors.execute('ATTACH DATABASE ? AS business',((settings().data_dir/'papers.sqlite3').resolve().as_uri()+'?mode=ro',))
        try:
            vectors.execute('DELETE FROM paper_vectors WHERE NOT EXISTS(SELECT 1 FROM business.papers p WHERE p.id=paper_vectors.paper_id)')
            vector_store.index_create(vectors,dim)
            total=vectors.execute('SELECT COUNT(*) FROM paper_vectors').fetchone()[0]
        finally:
            vectors.commit();vectors.execute('DETACH DATABASE business')
    with connect() as db:
        _index_state(db,'indexing',0,total)
        return total


def _index_batch(last_id, completed, total):
    name=pending()['vector_file']
    with vector_store.writer(name) as vectors:
        batch=vectors.execute('SELECT paper_id,embedding FROM paper_vectors WHERE paper_id>? ORDER BY paper_id LIMIT ?',
                         (last_id,INDEX_BATCH_SIZE)).fetchall()
        vectors.executemany('INSERT INTO papers_vec VALUES(?,?)',[(r['paper_id'],r['embedding']) for r in batch])
        completed+=len(batch)
    with connect() as db:
        _index_state(db,'indexing',completed,total)
        return batch[-1]['paper_id'] if batch else last_id,completed,len(batch)


def _apply_index(config, indexed):
    """All readers keep the old space until this transaction commits."""
    from . import runtime as models
    name=pending()['vector_file']
    with connect(background=True) as db:
        vector_store.attach(db,name,'vector_build')
        db.execute('BEGIN IMMEDIATE')
        # Paper/profile edits during index preparation must be regenerated first.
        if (db.execute(PAPERS+' LIMIT 1').fetchone() or db.execute(PROFILES+' LIMIT 1').fetchone()
                or db.execute('SELECT COUNT(*) FROM papers').fetchone()[0]!=indexed):return False
        # Readiness markers are small; existing paper blobs are never rewritten
        # in the activation transaction. All authoritative vectors are external.
        db.execute('UPDATE papers SET embedding=? WHERE embedding IS NULL',(vector_store.READY,))
        db.execute('UPDATE interest_profile SET embedding=NULL')
        db.execute('UPDATE interest_profile SET embedding_parts=NULL')
        db.execute(f'UPDATE interest_profile SET embedding=(SELECT embedding FROM vector_build.profile_vectors WHERE profile_id=interest_profile.id) WHERE id IN ({CURRENT_PROFILE_IDS})')
        db.execute(f'UPDATE interest_profile SET embedding_parts=(SELECT parts FROM vector_build.profile_vector_parts WHERE profile_id=interest_profile.id) WHERE id IN ({CURRENT_PROFILE_IDS})')
        active=db.execute("SELECT value FROM app_settings WHERE name='models'").fetchone()
        latest=decrypt_configuration(json.loads(active['value'])) if active else models.defaults()
        merged=with_embedding(latest,config)
        db.execute("INSERT INTO app_settings VALUES('models',?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(dumps(encrypt_configuration(merged)),now()))
        vector_store.activate(db,name,config['embedding_dim'],model_epoch=name)
        db.execute("DELETE FROM app_settings WHERE name='embedding_rebuild'")
    return True


async def _database_step(function,*args):
    # Cancellation finishes the current transaction before releasing the job lock.
    task=asyncio.create_task(asyncio.to_thread(function,*args))
    try:return await asyncio.shield(task)
    except asyncio.CancelledError:
        result=await task
        # A successful final switch must not be reported as stopped.
        if function is _apply_index and result:return result
        raise


async def run():
    from . import runtime as models
    from .embedding_queue import background
    from .catalog import embedding_info
    from ..pipeline.progress import TaskProgress
    from ..interest.profile import profile_embedding,embedding_inputs
    from ..pipeline import paper_retries
    value=pending()
    if not value:return 0
    await _database_step(vector_store.ensure)
    config=value['config'];binding=models.resolve(config['routes']['embedding']['primary'],config)
    concurrency=models.embedding_parallelism(binding,config,background=True)
    batch_size=(binding.get('embedding_metadata') or embedding_info(binding['model'],binding))['embedding_batch_size']
    name=await _database_step(_prepare_storage,config)
    progress=TaskProgress('build_vectors', [('embedding','重建论文向量',one('SELECT COUNT(*) n FROM papers')['n'],'篇',binding['model'],concurrency),
                                         ('profile_embedding','重建当前兴趣向量',one(f'SELECT COUNT(*) n FROM interest_profile WHERE id IN ({CURRENT_PROFILE_IDS})')['n'],'份',binding['model'],concurrency)])
    with vector_store.reader(name) as db:
        stored_profiles=db.execute('SELECT COUNT(*) FROM profile_vectors').fetchone()[0]
    progress.finish('embedding',completed=_stored_count(pending()))
    progress.finish('profile_embedding',completed=stored_profiles)
    update('running')
    try:
        with models.model_snapshot(config,replace=True):
            async with background():
                while True:
                    check_cancelled()
                    progress.begin('embedding')
                    eligible_papers='SELECT * FROM ('+PAPERS+') WHERE '+paper_retries.eligible('build_vectors')+' ORDER BY id'
                    while batch:=await _database_step(_pending_rows,eligible_papers+' LIMIT ?',(batch_size*concurrency,)):
                        check_cancelled()
                        async def process(papers):
                            try:
                                vectors=await models.embed([p['title']+'\n'+(p['abstract'] or '') for p in papers])
                                check_cancelled()
                                def save():
                                    with vector_store.writer(name) as db:
                                        vector_store.stage(db,[(paper['id'],paper['title'],paper['abstract'] or '',pack(vector)) for paper,vector in zip(papers,vectors)])
                                await _database_step(save)
                            except Exception:
                                check_cancelled()
                                paper_retries.failed('build_vectors',[p['id'] for p in papers])
                                raise
                            paper_retries.succeeded('build_vectors',[p['id'] for p in papers])
                            progress.finish('embedding',completed=len(papers),paper=papers[0])
                        tasks=[asyncio.create_task(process(batch[i:i+batch_size])) for i in range(0,len(batch),batch_size)]
                        try:await asyncio.gather(*tasks)
                        finally:
                            for task in tasks:
                                if not task.done():task.cancel()
                            await asyncio.gather(*tasks,return_exceptions=True)
                        from ..background_load import yield_to_web
                        await yield_to_web()
                    if await _database_step(_pending_rows,PAPERS+' LIMIT 1'):
                        raise paper_retries.PausedError('部分论文向量已暂停自动处理，请使用“重试失败项”恢复。原向量仍然可用')
                    progress.begin('profile_embedding')
                    while batch:=await _database_step(_pending_rows,PROFILES+' LIMIT 32'):
                        for profile in batch:
                            check_cancelled()
                            if not one(f'SELECT id FROM interest_profile WHERE id=? AND id IN ({CURRENT_PROFILE_IDS})',(profile['id'],)):continue
                            vector=await profile_embedding(profile['content'])
                            if vector is None and embedding_inputs(profile['content']):raise ValueError('新模型生成兴趣向量失败')
                            check_cancelled()
                            def save_profile():
                                with vector_store.writer(name) as db:
                                    db.execute('INSERT OR REPLACE INTO profile_vectors VALUES(?,?,?)',(profile['id'],profile['content'],vector))
                                    from ..interest.vectors import parts_json
                                    db.execute('INSERT OR REPLACE INTO profile_vector_parts VALUES(?,?)',(profile['id'],parts_json(vector)))
                            await _database_step(save_profile)
                            progress.finish('profile_embedding',completed=1)
                    dim=config['embedding_dim']
                    check_cancelled()
                    total=await _database_step(_prepare_index,dim)
                    last_id=indexed=0
                    progress.set_phase('indexing','写入新向量索引',indexed,total)
                    while True:
                        check_cancelled()
                        last_id,indexed,count=await _database_step(_index_batch,last_id,indexed,total)
                        progress.set_phase('indexing','写入新向量索引',indexed,total)
                        if not count:break
                        from ..background_load import yield_to_web
                        await yield_to_web()
                    check_cancelled()
                    update('applying')
                    progress.set_phase('applying','切换向量模型与索引')
                    if not await _database_step(_apply_index,config,indexed):
                        update('running')
                        progress.set_phase(None,None)
                        continue
                    settings().embedding_dim=dim
                    from ..vector_search import close
                    close()
                    current_profiles=one(f'SELECT COUNT(*) n FROM interest_profile WHERE id IN ({CURRENT_PROFILE_IDS})')['n']
                    progress.stages['embedding'].update(total=indexed,completed=indexed)
                    progress.stages['profile_embedding'].update(total=current_profiles,completed=current_profiles)
                    progress.set_phase('complete','向量模型切换完成')
                    return progress.value()['completed']
    except asyncio.CancelledError:
        update('stopped');raise
    except Exception as error:
        update('failed',models.safe_error(error)[:300]);raise
    finally:progress.close()
