from .vector_helpers import vector_rows,embedding
import asyncio
import copy
from contextvars import Context
import json
import pytest
from app import scheduler
from app.config import now,settings
from app.db import connect,execute,one,rows
from app.llm import catalog,runtime,vector_rebuild
from app.llm.ollama import ollama
from app.llm.secrets import encrypt_configuration
from app.pipeline import classify
from .conftest import headers
from .test_model_catalog_codex import authorize


def payload(client,admin):return client.get('/api/admin/models',headers=headers(admin)).json()


def store(value):
    execute("INSERT OR REPLACE INTO app_settings VALUES('models',?,?)",(json.dumps(encrypt_configuration(value)),now()))


def shared_connection_change(client,admin):
    current=runtime.configuration()
    connection=next(c for c in current['connections'] if c['id']=='cloud')
    connection.update(base_url='https://old.test/v1',api_key='old-test-key')
    current['routes']['embedding']['primary'].update(connection_id='cloud',model='test-embedding',thinking='auto')
    current['routes']['classify']['primary'].update(connection_id='cloud',model='classifier',thinking='auto')
    store(current)
    candidate=payload(client,admin)
    next(c for c in candidate['connections'] if c['id']=='cloud').update(base_url='https://new.test/v1',api_key='new-test-key')
    candidate['rebuild_vectors']=True
    return candidate


@pytest.fixture
def saves(monkeypatch):
    started=[]
    monkeypatch.setattr(scheduler,'start_manual',started.append)
    async def discover(binding):return {'thinking_modes':['auto','on','off'],'reasoning_efforts':['low','medium','high'],'type':'chat','protocol':'responses' if binding['kind']=='codex' else 'openai','note':''}
    monkeypatch.setattr('app.api.model_config.discover',discover)
    return started


def switch(client,admin):
    c=payload(client,admin)
    c['routes']['embedding']['primary']['model']='replacement-embedding'
    c['embedding_dim']=8;c['rebuild_vectors']=True
    c['connections'].append({'id':'codex','name':'Codex','kind':'codex','base_url':'https://chatgpt.com/backend-api/codex'})
    authorize()
    c['routes']['classify']={'primary':{'connection_id':'codex','model':'gpt-6.1-sol','thinking':'auto','reasoning_effort':'medium'},'fallback':None}
    c['classify_cloud_concurrency']=7
    r=client.put('/api/admin/models',headers=headers(admin),json=c)
    assert r.status_code==200,r.text
    return r.json()


def test_ollama_alias_selection_does_not_rebuild_or_change_vectors(client,accounts,papers,saves):
    admin=accounts[0];c=payload(client,admin);old=embedding(papers[0])
    connection=c['connections'][0]
    catalog.store(connection,[catalog.metadata({'name':'bge-m3:latest'},connection)])
    c['routes']['embedding']['primary']['model']='bge-m3:latest';c['rebuild_vectors']=True
    c['routes']['classify']['primary'].update(connection_id='cloud',model='classifier',thinking='auto')
    r=client.put('/api/admin/models',headers=headers(admin),json=c)
    assert r.status_code==200,r.text
    assert not r.json()['rebuild_queued'] and r.json()['pending_rebuild'] is None and not saves
    assert runtime.selected('classify')['model']=='classifier'
    assert runtime.selected('embedding')['model']=='bge-m3:latest'
    assert embedding(papers[0])==old
    assert catalog.model_info(connection,'bge-m3')['id']=='bge-m3:latest'
    explicit=copy.deepcopy(c);explicit['routes']['embedding']['primary']['model']='bge-m3:v2'
    assert runtime.embedding_identity(explicit)!=runtime.embedding_identity(c)
    cloud=copy.deepcopy(c);cloud['routes']['embedding']['primary']['connection_id']='cloud'
    other=copy.deepcopy(cloud);other['routes']['embedding']['primary']['model']='bge-m3'
    assert runtime.embedding_identity(other)!=runtime.embedding_identity(cloud)


def test_vector_switch_saves_classify_immediately_and_allows_later_edits(client,accounts,papers,saves):
    admin=accounts[0];r=switch(client,admin)
    assert r['rebuild_queued'] and saves==['build_vectors']
    assert r['embedding_dim']==4 and r['routes']['classify']['primary']['model']=='gpt-6.1-sol'
    assert r['classify_cloud_concurrency']==7 and len(vector_rows())==3
    c=payload(client,admin)
    c['routes']['classify']['primary']['model']='gpt-6-sol';c['classify_cloud_concurrency']=3
    vector_rebuild.update('running')
    result=client.put('/api/admin/models',headers=headers(admin),json=c)
    assert result.status_code==200,result.text
    assert runtime.selected('classify')['model']=='gpt-6-sol' and runtime.concurrency('classify')==3
    assert vector_rebuild.pending()['status']=='running' and saves==['build_vectors']
    c['embedding_dim']=16;c['rebuild_vectors']=True
    assert client.put('/api/admin/models',headers=headers(admin),json=c).status_code==409


@pytest.mark.asyncio
async def test_shared_connection_switch_keeps_old_embeddings_and_merges_new_settings(client,accounts,papers,saves,monkeypatch):
    admin=accounts[0];candidate=shared_connection_change(client,admin);old=embedding(papers[0])
    without_rebuild={**candidate,'rebuild_vectors':False}
    assert client.put('/api/admin/models',headers=headers(admin),json=without_rebuild).status_code==409
    assert runtime.selected('chat')['base_url']=='https://old.test/v1' and not vector_rebuild.pending()
    response=client.put('/api/admin/models',headers=headers(admin),json=candidate)
    assert response.status_code==200,response.text
    assert saves==['build_vectors'] and embedding(papers[0])==old
    assert runtime.selected('embedding')['base_url']=='https://old.test/v1'
    assert runtime.selected('embedding')['api_key']=='old-test-key'
    assert runtime.selected('classify')['base_url']=='https://new.test/v1'
    assert runtime.selected('chat')['api_key']=='new-test-key'
    assert len([c for c in runtime.configuration()['connections'] if c.get('embedding_retained')])==1
    saved_json=one("SELECT value FROM app_settings WHERE name='models'")['value']
    assert 'old-test-key' not in saved_json and 'new-test-key' not in saved_json
    assert 'old-test-key' not in response.text and 'new-test-key' not in response.text
    edited=payload(client,admin);edited['classify_cloud_concurrency']=9
    # Public settings saves cannot alter the internal old-vector credentials.
    next(c for c in edited['connections'] if c.get('embedding_retained')).update(base_url='https://tampered.test/v1',api_key='tampered-key')
    result=client.put('/api/admin/models',headers=headers(admin),json=edited)
    assert result.status_code==200,result.text
    assert runtime.selected('embedding')['base_url']=='https://old.test/v1'
    async def embed(texts):
        assert runtime.selected('embedding')['base_url']=='https://new.test/v1'
        assert runtime.selected('embedding')['api_key']=='new-test-key'
        return [[0.,0,0,1.] for _ in texts]
    monkeypatch.setattr(runtime,'embed',embed)
    await vector_rebuild.run()
    assert vector_rebuild.pending() is None
    assert runtime.selected('embedding')['connection_id']=='cloud'
    assert runtime.selected('embedding')['base_url']=='https://new.test/v1'
    assert runtime.concurrency('classify')==9
    assert not any(c.get('embedding_retained') for c in runtime.configuration()['connections'])
    assert embedding(papers[0])!=old


@pytest.mark.asyncio
async def test_shared_connection_failure_stop_and_cancel_preserve_old_vector_binding(client,accounts,papers,saves,monkeypatch):
    admin=accounts[0];candidate=shared_connection_change(client,admin);old=embedding(papers[0])
    result=client.put('/api/admin/models',headers=headers(admin),json=candidate)
    assert result.status_code==200,result.text
    async def fail(texts):raise ValueError('offline')
    monkeypatch.setattr(runtime,'embed',fail)
    with pytest.raises(ValueError):await vector_rebuild.run()
    assert vector_rebuild.state()['status']=='failed'
    entered=asyncio.Event()
    async def wait(texts):entered.set();await asyncio.Event().wait()
    monkeypatch.setattr(runtime,'embed',wait)
    task=asyncio.create_task(vector_rebuild.run());await entered.wait();task.cancel();await asyncio.gather(task,return_exceptions=True)
    assert vector_rebuild.state()['status']=='stopped'
    assert client.post('/api/admin/models/embedding-rebuild/cancel',headers=headers(admin)).status_code==200
    assert vector_rebuild.pending() is None and embedding(papers[0])==old
    assert runtime.selected('embedding')['base_url']=='https://old.test/v1'
    assert runtime.selected('embedding')['api_key']=='old-test-key'
    assert runtime.selected('chat')['base_url']=='https://new.test/v1'
    # A new attempt reuses the retained live binding rather than making more copies.
    retry=payload(client,admin);retry['routes']['embedding']['primary']['connection_id']='cloud';retry['rebuild_vectors']=True
    result=client.put('/api/admin/models',headers=headers(admin),json=retry)
    assert result.status_code==200,result.text
    assert len([c for c in runtime.configuration()['connections'] if c.get('embedding_retained')])==1


@pytest.mark.asyncio
async def test_finishing_rebuild_merges_vectors_without_overwriting_new_settings(client,accounts,papers,saves,monkeypatch):
    admin=accounts[0];switch(client,admin);updated=False
    async def embed(texts):
        nonlocal updated
        if not updated:
            updated=True
            # An HTTP request does not inherit the background worker's model snapshot.
            c=Context().run(payload,client,admin)
            c['routes']['classify']['primary']['model']='gpt-6-sol'
            c['classify_cloud_concurrency']=5;c['brief_cloud_concurrency']=6
            r=Context().run(lambda:client.put('/api/admin/models',headers=headers(admin),json=c))
            assert r.status_code==200,r.text
        return [[1.,0,0,0,0,0,0,0] for _ in texts]
    monkeypatch.setattr(ollama,'embed',embed)
    await vector_rebuild.run()
    assert vector_rebuild.pending() is None and runtime.configuration()['embedding_dim']==8
    assert runtime.selected('embedding')['model']=='replacement-embedding:latest'
    assert runtime.selected('classify')['model']=='gpt-6-sol'
    assert runtime.concurrency('classify')==5 and runtime.configuration()['brief_cloud_concurrency']==6
    assert len(embedding(papers[0]))==32


@pytest.mark.asyncio
async def test_failure_stop_and_cancel_keep_saved_classify_and_old_vectors(client,accounts,papers,saves,monkeypatch):
    admin=accounts[0];old=embedding(papers[0]);switch(client,admin)
    async def fail(texts):raise ValueError('offline')
    monkeypatch.setattr(ollama,'embed',fail)
    with pytest.raises(ValueError):await vector_rebuild.run()
    assert vector_rebuild.state()['status']=='failed' and runtime.selected('classify')['model']=='gpt-6.1-sol'
    entered=asyncio.Event()
    async def wait(texts):entered.set();await asyncio.Event().wait()
    monkeypatch.setattr(ollama,'embed',wait)
    task=asyncio.create_task(vector_rebuild.run());await entered.wait();task.cancel();await asyncio.gather(task,return_exceptions=True)
    assert vector_rebuild.state()['status']=='stopped'
    cancelled=client.post('/api/admin/models/embedding-rebuild/cancel',headers=headers(admin))
    assert cancelled.status_code==200 and vector_rebuild.pending() is None
    assert runtime.selected('classify')['model']=='gpt-6.1-sol' and runtime.configuration()['embedding_dim']==4
    assert embedding(papers[0])==old


def test_pending_target_connection_cannot_be_removed_or_repointed(client,accounts,papers,saves):
    admin=accounts[0];c=payload(client,admin)
    c['connections'].append({'id':'new','name':'New vectors','kind':'cloud','base_url':'https://vectors.test/v1','api_key':'private-key'})
    c['routes']['embedding']['primary'].update(connection_id='new',model='text-embedding-new');c['rebuild_vectors']=True
    r=client.put('/api/admin/models',headers=headers(admin),json=c);assert r.status_code==200,r.text
    current=payload(client,admin)
    for item in current['connections']:
        if item['id']=='new':item['base_url']='https://changed.test/v1'
    r=client.put('/api/admin/models',headers=headers(admin),json=current)
    assert r.status_code==409 and '待切换向量' in r.json()['detail']
    current['connections']=[c for c in current['connections'] if c['id']!='new']
    assert client.put('/api/admin/models',headers=headers(admin),json=current).status_code==409


def test_legacy_alias_pending_recovers_nonvector_settings_without_rebuilding(client,accounts,papers):
    current=runtime.configuration();store(current);old=embedding(papers[0])
    target=copy.deepcopy(current);target['routes']['embedding']['primary']['model']='bge-m3:latest'
    target['routes']['classify']['primary'].update(connection_id='cloud',model='saved-classifier',thinking='auto')
    execute("INSERT INTO app_settings VALUES('embedding_rebuild',?,?)",(json.dumps({'config':encrypt_configuration(target),'status':'stopped'}),now()))
    with connect() as db:vector_rebuild.migrate_legacy(db)
    assert vector_rebuild.pending() is None and runtime.selected('classify')['model']=='saved-classifier'
    assert embedding(papers[0])==old


def test_classify_concurrency_persists_limits_and_frozen_task_setting(client,accounts,saves):
    admin=accounts[0];c=payload(client,admin)
    c['routes']['classify']['primary'].update(connection_id='cloud',model='classifier',thinking='auto')
    c['classify_cloud_concurrency']=16
    assert client.put('/api/admin/models',headers=headers(admin),json=c).status_code==200
    assert runtime.concurrency('classify')==16
    with runtime.model_snapshot():
        changed=copy.deepcopy(runtime.configuration());changed['classify_cloud_concurrency']=2;store(changed)
        assert runtime.concurrency('classify')==16
    assert runtime.concurrency('classify')==2
    c['classify_cloud_concurrency']=17
    assert client.put('/api/admin/models',headers=headers(admin),json=c).status_code==422
    c['classify_cloud_concurrency']=0
    assert client.put('/api/admin/models',headers=headers(admin),json=c).status_code==422
    changed['routes']['classify']['primary'].update(connection_id='local',model='qwen3:4b');store(changed)
    assert runtime.concurrency('classify')==1


@pytest.mark.asyncio
async def test_classification_workers_obey_admin_concurrency_setting(client,papers,monkeypatch):
    c=runtime.configuration();c['routes']['classify']['primary'].update(connection_id='cloud',model='classifier',thinking='auto');c['classify_cloud_concurrency']=2;store(c)
    async def ready():pass
    monkeypatch.setattr(classify,'check_service',ready);monkeypatch.setattr(classify,'ensure_index',ready)
    active=0;peak=0
    async def classify_one(paper):
        nonlocal active,peak
        active+=1;peak=max(peak,active)
        await asyncio.sleep(.01)
        active-=1
        execute('UPDATE papers SET classified=1 WHERE id=?',(paper['id'],))
    monkeypatch.setattr(classify,'classify_paper',classify_one)
    assert await classify.classify()==3 and peak==2
    assert json.loads(one("SELECT progress FROM source_status WHERE name='classify'")['progress'])['concurrency']==2


@pytest.mark.asyncio
async def test_vector_connection_address_waits_for_rebuild_while_other_settings_save(client,accounts,papers,saves,monkeypatch):
    admin=accounts[0];c=payload(client,admin)
    for feature,route in c['routes'].items():
        if feature!='embedding':route['primary'].update(connection_id='cloud',model='cloud-model',thinking='auto',reasoning_effort='auto')
    for route in c['routes'].values():route['fallback']=None
    c['connections'][0]['base_url']='http://new-vectors.test:11434';c['rebuild_vectors']=True
    old_url=runtime.configuration()['connections'][0]['base_url']
    r=client.put('/api/admin/models',headers=headers(admin),json=c)
    assert r.status_code==200,r.text
    assert runtime.configuration()['connections'][0]['base_url']==old_url
    assert vector_rebuild.pending()['config']['connections'][0]['base_url']=='http://new-vectors.test:11434'
    current=payload(client,admin);current['classify_cloud_concurrency']=9
    r=client.put('/api/admin/models',headers=headers(admin),json=current)
    assert r.status_code==200,r.text
    async def embed(texts):return [[0.,0,0,1.] for _ in texts]
    monkeypatch.setattr(ollama,'embed',embed)
    await vector_rebuild.run()
    assert runtime.configuration()['connections'][0]['base_url']=='http://new-vectors.test:11434'
    assert runtime.concurrency('classify')==9


def test_save_rejects_race_with_completed_vector_switch(client,accounts,saves,monkeypatch):
    admin=accounts[0];current=runtime.configuration();store(current);c=payload(client,admin);switched=False
    async def discover(binding):
        nonlocal switched
        if not switched:
            switched=True
            latest=copy.deepcopy(current);latest['routes']['embedding']['primary']['model']='just-activated-embedding';store(latest)
        return {'thinking_modes':['auto','on','off'],'reasoning_efforts':[],'type':'chat','protocol':'openai','note':''}
    monkeypatch.setattr('app.api.model_config.discover',discover)
    r=client.put('/api/admin/models',headers=headers(admin),json=c)
    assert r.status_code==409 and '刷新' in r.json()['detail']
    assert runtime.selected('embedding')['model']=='just-activated-embedding'


def test_legacy_real_switch_keeps_staged_results_and_recovers_other_settings(client,accounts,papers):
    current=runtime.configuration();store(current)
    target=copy.deepcopy(current);target['routes']['embedding']['primary']['model']='another-embedding'
    target['routes']['classify']['primary'].update(connection_id='cloud',model='saved-classifier',thinking='auto')
    paper=one('SELECT title,abstract,embedding FROM papers WHERE id=?',(papers[0],))
    execute('INSERT INTO embedding_rebuild_papers VALUES(?,?,?,?)',(papers[0],paper['title'],paper['abstract'],paper['embedding']))
    execute("INSERT INTO app_settings VALUES('embedding_rebuild',?,?)",(json.dumps({'config':encrypt_configuration(target),'status':'stopped'}),now()))
    with connect() as db:vector_rebuild.migrate_legacy(db)
    assert vector_rebuild.pending()['separate_settings'] and vector_rebuild.state()['completed']==1
    assert runtime.selected('classify')['model']=='saved-classifier'
    assert runtime.embedding_identity(runtime.configuration())==runtime.embedding_identity(current)
