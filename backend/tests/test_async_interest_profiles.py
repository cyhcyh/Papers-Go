import asyncio
import json
import pytest

from app.db import connect, execute, one, set_paper_vector
from app.config import now, today
from app.interest import profile_updates as updates, retrieval_query
from app.interest.profile import current, put_profile, profile_embedding
from app.interest.form import ProfileForm, render_form
from app.llm import runtime as models
from app import vector_store
from .conftest import headers, finish_interest_updates


@pytest.fixture
def prepared(client, accounts, monkeypatch):
    monkeypatch.setattr(models,'configured',lambda feature:True)
    uid=accounts[0]['user']['id']
    content=render_form(ProfileForm(long_term=[{'text':'Graph theory','weight':.7}]))
    vector=client.portal.call(profile_embedding,content)
    put_profile(uid,content,{},'init',vector)
    return uid,headers(accounts[0])


def save(client,auth,text='New interest',weight=.7):
    response=client.put('/api/profile',headers=auth,json={'form':{'long_term':[{'text':text,'weight':weight}]}})
    assert response.status_code==200
    return response.json()


def test_save_returns_before_models_and_preserves_active_until_ready(client,prepared,monkeypatch):
    uid,auth=prepared;old=current(uid)
    async def forbidden(*args,**kwargs):raise AssertionError('Saving must not call models')
    with monkeypatch.context() as patch:
        patch.setattr(models,'complete',forbidden);patch.setattr(models,'embed',forbidden)
        result=save(client,auth)
    assert result['update']['status']=='queued'
    public=client.get('/api/profile',headers=auth).json()
    assert public['current']['id']==old['id'] and public['current']['embedding_ready']
    assert public['draft']['form']['long_term'][0]['text']=='New interest'
    assert 'embedding' not in public['draft'] and 'embedding_parts' not in public['current']
    finish_interest_updates(client)
    public=client.get('/api/profile',headers=auth).json()
    assert public['update']['status']=='completed' and public['draft'] is None
    assert public['current']['id']!=old['id'] and public['current']['embedding_ready']


def test_weight_changes_reuse_learned_parts_without_model_calls(client,prepared,monkeypatch):
    uid,auth=prepared;before=current(uid)
    async def forbidden(*args,**kwargs):raise AssertionError('Weights must reuse vectors')
    monkeypatch.setattr(models,'complete',forbidden);monkeypatch.setattr(models,'embed',forbidden)
    assert save(client,auth,'Graph theory',.9)['update']['status']=='completed'
    assert current(uid)['embedding_parts']==before['embedding_parts']


def test_upgrade_queues_current_profiles_once_and_preserves_site_settings(client,prepared):
    uid,_=prepared
    from app import recommendation_settings
    with connect() as db:
        site=json.loads(db.execute("SELECT value FROM app_settings WHERE name='site'").fetchone()['value'])
        site['name']='Keep this site name'
        site['admin_path']='/keep-this-admin-entry'
        site['recommendation']={'personal':{'interest':.6,'quality':.3,'diversity':.1},
                                'guest':{'quality':.55,'recency':.45}}
        db.execute("UPDATE app_settings SET value=? WHERE name='site'",(json.dumps(site),))
        db.execute("DELETE FROM app_migrations WHERE name='interest-retrieval-v1'")
        before=current(uid)
        updates.initialize_upgrade(db)
    job=updates.latest(uid)
    assert job['status']=='queued' and job['reason']=='retrieval_upgrade'
    assert job['base_profile_id']==before['id'] and current(uid)==before
    with connect() as db:
        saved=json.loads(db.execute("SELECT value FROM app_settings WHERE name='site'").fetchone()['value'])
        assert saved['name']==site['name'] and saved['admin_path']==site['admin_path']
        assert saved['recommendation']['guest']==pytest.approx({'quality':.539,'recency':.441,'author':.02})
        assert saved['recommendation']['personal']==recommendation_settings.defaults()['personal']
        saved['recommendation']['personal']['quality']=.15
        db.execute("UPDATE app_settings SET value=? WHERE name='site'",(json.dumps(saved),))
        updates.initialize_upgrade(db)
        assert recommendation_settings.configuration(db)['personal']['quality']==.15
    assert updates.latest(uid)['revision']==job['revision']


def test_failed_generation_retains_active_and_retry_completes(client,prepared,monkeypatch):
    uid,auth=prepared;old=current(uid)
    save(client,auth)
    async def failed(*args,**kwargs):raise TimeoutError('private upstream detail')
    with monkeypatch.context() as patch:
        patch.setattr(models,'complete',failed)
        assert client.portal.call(updates.process,updates.claim()) is False
    public=client.get('/api/profile',headers=auth).json()
    assert public['update']['status']=='failed' and 'private upstream detail' not in str(public)
    assert current(uid)['id']==old['id']
    save(client,auth);finish_interest_updates(client)
    assert updates.latest(uid)['status']=='completed'


@pytest.mark.asyncio
async def test_newer_edit_cancels_old_request(client,prepared,monkeypatch):
    uid,auth=prepared;old=current(uid)
    started=asyncio.Event()
    async def delayed(*args,**kwargs):
        started.set();await asyncio.Event().wait()
    save(client,auth,'First')
    job=updates.claim()
    with monkeypatch.context() as patch:
        patch.setattr(models,'complete',delayed)
        task=asyncio.create_task(updates.process(job))
        await asyncio.wait_for(started.wait(),2)
        result=save(client,auth,'Latest')
        assert result['update']['revision']!=job['revision']
        assert await asyncio.wait_for(task,2) is False
    assert current(uid)['id']==old['id']
    finish_interest_updates(client)
    assert 'Latest' in current(uid)['content'] and 'First' not in current(uid)['content']


def test_worker_restart_recovers_saved_request(client,prepared):
    uid,auth=prepared;save(client,auth);job=updates.claim()
    updates.initialize(recover=True)
    assert updates.latest(uid)['status']=='queued'
    assert updates.latest(uid)['revision']==job['revision']
    finish_interest_updates(client)
    assert updates.latest(uid)['status']=='completed'


@pytest.mark.parametrize('change',['profile','vector_space','disabled'])
def test_background_result_cannot_overwrite_changed_state(client,prepared,change):
    uid,auth=prepared;save(client,auth);job=updates.claim();old=current(uid)
    if change=='profile':put_profile(uid,old['content'],{},'reflect',old['embedding'])
    elif change=='disabled':execute('UPDATE users SET disabled=1 WHERE id=?',(uid,))
    else:execute("INSERT INTO app_settings(name,value,updated_at) VALUES('vector_store',?,datetime('now'))",(json.dumps({'name':'changed','epoch':'changed'}),))
    before=current(uid)
    with pytest.raises(updates.Superseded):
        updates.apply(job,None,models.embedding_identity(models.configuration()),'legacy')
    assert current(uid)==before


def test_draft_is_private_and_cascades_with_deleted_user(client,accounts,prepared):
    uid,auth=prepared;save(client,auth)
    other=client.get('/api/profile',headers=headers(accounts[1])).json()
    assert other['draft'] is None and other['update'] is None
    execute('UPDATE users SET is_admin=1 WHERE id=?',(accounts[1]['user']['id'],))
    response=client.delete('/api/admin/users/'+str(uid),headers=headers(accounts[1]))
    assert response.status_code==200
    assert updates.latest(uid) is None


def test_descriptions_cache_without_weights_and_prompt_edit_invalidates(client,prepared,monkeypatch):
    calls=[]
    async def describe(feature,messages,**kwargs):
        originals=json.loads(messages[-1]['content'])['interests'];calls.append(originals)
        return {'interests':[{'original':t,'query':t+' — stated research objects.'} for t in originals]}
    monkeypatch.setattr(models,'complete',describe)
    async def scenario():
        for weight in (.7,.9):
            content=render_form(ProfileForm(long_term=[{'text':'Another direction','weight':weight}]))
            await profile_embedding(content,strict=True)
        assert len(calls)==1
        original=retrieval_query.prompts.get(retrieval_query.PROMPT_NAME)
        execute("INSERT INTO app_settings(name,value,updated_at) VALUES('prompts',?,datetime('now'))",(json.dumps({retrieval_query.PROMPT_NAME:original+'\nUse precise terminology.'}),))
        await profile_embedding(content,strict=True)
    client.portal.call(scenario)
    assert len(calls)==2


def test_empty_preferences_save_without_model_generation(client,prepared,monkeypatch):
    uid,auth=prepared
    async def forbidden(*args,**kwargs):raise AssertionError('Empty interests need no model')
    monkeypatch.setattr(models,'complete',forbidden);monkeypatch.setattr(models,'embed',forbidden)
    result=client.put('/api/profile',headers=auth,json={'form':{}}).json()
    assert result['update']['status']=='completed' and current(uid)['embedding'] is None


def test_conversion_keeps_feedback_received_during_preparation(client,prepared):
    uid,auth=prepared;profile=current(uid)
    old_parts=json.loads(profile['embedding_parts'])
    for part in old_parts:
        for key in ('query','query_revision','vector_epoch'):part.pop(key,None)
    execute('UPDATE interest_profile SET embedding_parts=? WHERE id=?',(json.dumps(old_parts),profile['id']))
    updates.submit(uid,profile['content'],{},'retrieval_upgrade')
    job=updates.claim()
    rebuilt=client.portal.call(profile_embedding,profile['content'])
    ident=execute('INSERT INTO papers(title,abstract,primary_category,published,ingested_date,created_at) VALUES(?,?,?,?,?,?)',
                  ('Related graph result','Graph results','math.CO',today(),today(),now()))
    set_paper_vector(ident,[.6,.8,0.,0.])
    assert client.post('/api/interactions',headers=auth,json={'paper_id':ident,'action':'like'}).status_code==200
    learned=current(uid)['embedding']
    assert learned!=bytes(rebuilt)
    assert updates.apply(job,rebuilt,models.embedding_identity(models.configuration()),vector_store.epoch())
    assert current(uid)['embedding']==learned
    assert json.loads(current(uid)['embedding_parts'])[0]['query_revision']==retrieval_query.prompt_revision()
