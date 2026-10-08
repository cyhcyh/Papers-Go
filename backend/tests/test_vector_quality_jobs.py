import json

import pytest

from app.config import now
from app.db import connect,execute,one,rows,dumps,pack
from app.interest.profile import put_profile,CURRENT_PROFILE_IDS
from app.llm import runtime as models,vector_rebuild
from app.llm.ollama import ollama
from app.pipeline import embed,redo
from .conftest import headers


def history(accounts):
    ids=[]
    for account,count in zip(accounts,(4,2)):
        for version in range(count):
            user=account['user']['id']
            ids.append(put_profile(user,f'## 核心兴趣\n- [w:1] profile-{user}-{version}',{},'manual')['id'])
    current={r['id'] for r in rows(f'SELECT id FROM interest_profile WHERE id IN ({CURRENT_PROFILE_IDS})')}
    return current,set(ids)-current


def test_separate_jobs_have_independent_switches(client,accounts):
    auth=headers(accounts[0])
    jobs={s['name']:s for s in client.get('/api/admin/sources',headers=auth).json()}
    assert 'embed_score' not in jobs and {'build_vectors','assess_quality'}<=jobs.keys()
    assert client.patch('/api/admin/jobs/build_vectors',headers=auth,json={'enabled':False}).status_code==200
    jobs={s['name']:s for s in client.get('/api/admin/sources',headers=auth).json()}
    assert not jobs['build_vectors']['enabled'] and jobs['assess_quality']['enabled']
    assert client.post('/api/admin/jobs/build_vectors',headers=auth).status_code==409
    for name,component in [('build_vectors','quality'),('assess_quality','embedding')]:
        assert client.post(f'/api/admin/jobs/{name}/redo/preview',headers=auth,json={'components':[component]}).status_code==400


@pytest.mark.asyncio
async def test_daily_vectors_only_use_current_profiles_and_never_assess_quality(client,accounts,papers,monkeypatch):
    current,old=history(accounts)
    calls=[]
    async def vectors(texts):calls.extend(texts);return [[1.,0,0,0] for _ in texts]
    async def forbidden(*args,**kwargs):raise AssertionError('vector job must not call a quality model')
    monkeypatch.setattr(models,'embed',vectors);monkeypatch.setattr(models,'complete',forbidden)
    original=rows('SELECT quality_score,skeleton,scored FROM papers ORDER BY id')
    await embed.build_vectors()
    assert len(calls)==2
    assert {r['id'] for r in rows('SELECT id FROM interest_profile WHERE embedding IS NOT NULL')}==current
    assert all(one('SELECT embedding FROM interest_profile WHERE id=?',(ident,))['embedding'] is None for ident in old)
    assert rows('SELECT quality_score,skeleton,scored FROM papers ORDER BY id')==original
    progress=json.loads(one("SELECT progress FROM source_status WHERE name='build_vectors'")['progress'])
    assert progress['stages'][1]['total']==2 and progress['stages'][1]['completed']==2


@pytest.mark.asyncio
@pytest.mark.parametrize('components,expected',[
    (['embedding'],(3,0,3)),(['profile_embedding'],(0,2,2)),(['embedding','profile_embedding'],(3,2,5))])
async def test_vector_redo_choices_use_current_profiles(client,accounts,papers,monkeypatch,components,expected):
    current,old=history(accounts)
    options=redo.RedoOptions(components=components)
    preview=redo.preview('build_vectors',options)
    assert (preview['papers'],preview['profiles'],preview['operations'])==expected
    calls=[]
    async def vectors(texts):calls.extend(texts);return [[0.,1,0,0] for _ in texts]
    monkeypatch.setattr(models,'embed',vectors)
    run=redo.create_run('build_vectors',options)
    await redo.run(run,'build_vectors')
    assert redo.state(run)['completed']==expected[2]
    generated={r['id'] for r in rows('SELECT id FROM interest_profile WHERE embedding IS NOT NULL')}
    assert generated==(current if 'profile_embedding' in components else set())
    assert not generated&old
    if components==['profile_embedding']:assert all(text.startswith('profile-') for text in calls)
    if components==['embedding']:assert not any(text.startswith('profile-') for text in calls)


@pytest.mark.asyncio
async def test_quality_job_does_not_require_vectors(client,accounts,papers,monkeypatch):
    history(accounts)
    execute('UPDATE papers SET embedding=NULL,scored=0')
    async def forbidden(*args,**kwargs):raise AssertionError('quality job must not generate vectors')
    async def complete(feature,messages,**kwargs):
        assert feature=='quality'
        return kwargs['validate']({'summary':'具体的新构造','contribution':80,'evidence_insufficient':True})
    monkeypatch.setattr(models,'embed',forbidden);monkeypatch.setattr(models,'complete',complete)
    await embed.assess_quality()
    assert all(r['scored'] and r['quality_score']==80 and r['embedding'] is None for r in rows('SELECT scored,quality_score,embedding FROM papers'))
    assert not one('SELECT id FROM interest_profile WHERE embedding IS NOT NULL')


@pytest.mark.asyncio
async def test_profile_redo_discards_result_when_profile_becomes_historical(client,accounts,monkeypatch):
    user=accounts[0]['user']['id']
    old=put_profile(user,'## 核心兴趣\n- [w:1] old',{},'manual')['id']
    latest=[]
    async def vectors(texts):
        latest.append(put_profile(user,'## 核心兴趣\n- [w:1] new',{},'manual')['id'])
        return [[1.,0,0,0] for _ in texts]
    monkeypatch.setattr(models,'embed',vectors)
    await redo.process({'profile_id':old,'component':'profile_embedding'})
    assert latest and one('SELECT embedding FROM interest_profile WHERE id=?',(old,))['embedding'] is None
    assert one('SELECT embedding FROM interest_profile WHERE id=?',(latest[0],))['embedding'] is None


@pytest.mark.asyncio
async def test_model_switch_rebuilds_latest_profiles_and_discards_old_staging(client,accounts,papers,monkeypatch):
    current,old=history(accounts)
    execute('UPDATE interest_profile SET embedding=?',(pack([1.,0,0,0]),))
    config=models.configuration();config['embedding_dim']=8;config['routes']['embedding']['primary']['model']='new-model'
    vector_rebuild.set_pending(config)
    obsolete=next(iter(old))
    execute('INSERT INTO embedding_rebuild_profiles VALUES(?,?,?)',(obsolete,one('SELECT content FROM interest_profile WHERE id=?',(obsolete,))['content'],b'outdated'))
    calls=[]
    async def vectors(texts):calls.extend(texts);return [[0.,1,0,0,0,0,0,0] for _ in texts]
    monkeypatch.setattr(ollama,'embed',vectors)
    await vector_rebuild.run()
    assert sum(text.startswith('profile-') for text in calls)==2
    assert {r['id'] for r in rows('SELECT id FROM interest_profile WHERE embedding IS NOT NULL')}==current
    assert not rows('SELECT * FROM embedding_rebuild_profiles') and vector_rebuild.pending() is None
    assert all(len(r['embedding'])==32 for r in rows('SELECT embedding FROM interest_profile WHERE embedding IS NOT NULL'))
    progress=json.loads(one("SELECT progress FROM source_status WHERE name='build_vectors'")['progress'])
    assert progress['stages'][1]['total']==2 and progress['stages'][1]['completed']==2


def test_split_migration_preserves_switches_redo_successes_and_queued_work(client,accounts,papers):
    current,old=history(accounts)
    execute("DELETE FROM app_migrations WHERE name='split-vector-quality-jobs-v1'")
    execute("INSERT INTO app_settings(name,value,updated_at) VALUES('pipeline_enabled',?,?)",(dumps({'embed_score':False}),now()))
    execute("INSERT INTO source_status(name,error) VALUES('embed_score','已手动停止')")
    options=dumps({'components':['embedding','quality'],'embedding_identity':list(models.embedding_identity(models.configuration()))})
    ident=execute("INSERT INTO pipeline_redo_runs(name,options,status,created_at,updated_at) VALUES('embed_score',?,'stopped',?,?)",(options,now(),now()))
    execute("INSERT INTO pipeline_redo_items(run_id,paper_id,component,status) VALUES(?,?,'embedding','done')",(ident,papers[0]))
    execute("INSERT INTO pipeline_redo_items(run_id,paper_id,component,status) VALUES(?,?,'quality','done')",(ident,papers[0]))
    for profile in (next(iter(current)),next(iter(old))):
        execute("INSERT INTO pipeline_redo_items(run_id,profile_id,component) VALUES(?,?,'profile_embedding')",(ident,profile))
    execute("INSERT INTO pipeline_commands(action,name,created_at,redo_id) VALUES('redo','embed_score',?,?)",(now(),ident))
    with connect() as db:redo.migrate_split_jobs(db)
    runs={r['name']:r for r in rows('SELECT id,name,status,options FROM pipeline_redo_runs')}
    assert runs['build_vectors']['id']==ident and runs['build_vectors']['status']=='stopped'
    assert runs['assess_quality']['status']=='completed'
    assert 'embedding_identity' not in json.loads(runs['assess_quality']['options'])
    assert json.loads(one("SELECT value FROM app_settings WHERE name='pipeline_enabled'")['value'])=={'build_vectors':False,'assess_quality':False}
    assert not one("SELECT name FROM source_status WHERE name='embed_score'")
    assert all(r['error']=='已手动停止' for r in rows('SELECT error FROM source_status'))
    assert redo.state(ident)['completed']==1 and redo.state(ident)['total']==2
    assert {r['name'] for r in rows('SELECT name FROM pipeline_commands')}=={'build_vectors','assess_quality'}
    before=rows('SELECT * FROM pipeline_redo_items ORDER BY id')
    with connect() as db:redo.migrate_split_jobs(db)
    assert rows('SELECT * FROM pipeline_redo_items ORDER BY id')==before
    assert one('SELECT COUNT(*) n FROM pipeline_commands')['n']==2
