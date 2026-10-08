from .vector_helpers import vector_rows,embedding
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI

from app.config import now, settings, today
from app.db import execute, one, rows
from app.llm import runtime
from app.llm.ollama import ollama
from app.llm.provider import cloud
from .conftest import headers, finish_interest_updates


def payload(client, admin):
    return client.get('/api/admin/models', headers=headers(admin)).json()


def persist(config):
    execute("INSERT OR REPLACE INTO app_settings VALUES('models',?,?)", (json.dumps(config),now()))


def test_model_settings_permissions_and_key_preservation(client, accounts):
    admin, regular = accounts
    assert client.get('/api/admin/models').status_code==401
    assert client.get('/api/admin/models',headers=headers(regular)).status_code==403
    config = payload(client,admin)
    assert len(config['features'])==11 and config['embedding_dim']==4
    config['connections'][1]['api_key']='test-private-key'
    result = client.put('/api/admin/models',json=config,headers=headers(admin))
    assert result.status_code==200 and 'test-private-key' not in result.text
    assert result.json()['connections'][1]['configured']
    assert 'api_key' not in result.json()['connections'][1]
    config = result.json()
    config['connections'][1]['api_key']=''
    assert client.put('/api/admin/models',json=config,headers=headers(admin)).status_code==200
    assert runtime.configuration()['connections'][1]['api_key']=='test-private-key'
    config['connections'][1]['clear_key']=True
    assert client.put('/api/admin/models',json=config,headers=headers(admin)).status_code==200
    assert runtime.configuration()['connections'][1]['api_key']==''
    settings.cache_clear()
    assert runtime.configuration()['routes']==config['routes']


@pytest.mark.asyncio
async def test_feature_routes_fallback_and_task_snapshot(client, monkeypatch):
    config = runtime.legacy_defaults()
    config['connections'][1]['api_key']='test-key'
    config['routes']['classify']={'primary':{'connection_id':'cloud','model':'classifier-v2'},'fallback':None}
    config['routes']['reading_l2']={'primary':{'connection_id':'local','model':'reader-local'},'fallback':None}
    config['routes']['brief']['fallback']={'connection_id':'cloud','model':'brief-backup'}
    persist(config)
    calls = []
    async def local(prompt, json_mode=True):
        calls.append(('local',runtime.current_binding()['model']))
        return {'valid':False}
    async def remote(messages, **kwargs):
        calls.append(('cloud',runtime.current_binding()['model']))
        return {'valid':True}
    monkeypatch.setattr(ollama,'chat',local)
    monkeypatch.setattr(cloud,'complete',remote)
    await runtime.complete('classify',[{'role':'user','content':'classify'}],json_mode=True)
    await runtime.complete('reading_l2',[{'role':'user','content':'read'}],json_mode=True)
    def validate(data):
        if not data['valid']:
            raise ValueError('invalid structured output')
        return data
    assert (await runtime.complete('brief',[{'role':'user','content':'brief'}],validate=validate))['valid']
    assert calls==[('cloud','classifier-v2'),('local','reader-local'),('local',settings().ollama_model),('cloud','brief-backup')]
    with runtime.model_snapshot():
        frozen = runtime.selected('classify')['model']
        config['routes']['classify']['primary']['model']='next-model'
        persist(config)
        assert runtime.selected('classify')['model']==frozen
    assert runtime.selected('classify')['model']=='next-model'
    assert not runtime.active_snapshots


@pytest.mark.asyncio
async def test_local_native_chat_and_cloud_fallback(client, monkeypatch):
    config = runtime.legacy_defaults()
    config['routes']['chat']={'primary':{'connection_id':'local','model':'local-tools'},'fallback':None}
    config['connections'][0]['base_url']='http://local.test:11434'
    persist(config)
    requests = []
    def handle(request):
        if request.url.path=='/api/show':return httpx.Response(200,json={'model_info':{}})
        requests.append((str(request.url),json.loads(request.content)))
        return httpx.Response(200,text=json.dumps({'message':{'content':'test reply'},'done':True})+'\n')
    original = httpx.AsyncClient
    monkeypatch.setattr('app.llm.ollama.httpx.AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    deltas=[d async for d in runtime.stream([{'role':'user','content':'hello'}],[])]
    assert deltas[0].content=='test reply'
    assert requests[0][0]=='http://local.test:11434/api/chat'
    assert requests[0][1]['model']=='local-tools' and requests[0][1]['think'] is None
    assert not rows('SELECT * FROM llm_usage')
    config['connections'][1]['api_key']='backup-key'
    config['routes']['chat']['fallback']={'connection_id':'cloud','model':'backup-tools'}
    persist(config)
    calls=[]
    async def stream(messages, tools):
        calls.append(runtime.current_binding()['model'])
        if len(calls)==1:
            raise RuntimeError('offline')
        yield SimpleNamespace(content='backup',tool_calls=None)
    monkeypatch.setattr(cloud,'stream',stream)
    monkeypatch.setattr(ollama,'stream',stream)
    assert [d.content async for d in runtime.stream([],[])]==['backup']
    assert calls==['local-tools','backup-tools']
    async def partial(messages, tools):
        yield SimpleNamespace(content='partial',tool_calls=None)
        raise RuntimeError('connection lost')
    monkeypatch.setattr(cloud,'stream',partial)
    monkeypatch.setattr(ollama,'stream',partial)
    with pytest.raises(RuntimeError):
        _=[d async for d in runtime.stream([],[])]


def test_connection_listing_uses_unsaved_key_and_safe_errors(client, accounts, monkeypatch):
    admin,_=accounts
    config=payload(client,admin)
    connection=config['connections'][1]
    connection.update(base_url='https://models.test/v1',api_key='private-unsaved-key')
    calls=[]
    def handle(request):
        calls.append((str(request.url),request.headers['authorization']))
        return httpx.Response(200,json={'object':'list','data':[{'id':'model-a','object':'model','created':0,'owned_by':'test'}]})
    def mock_client():
        binding=runtime.current_binding()
        return AsyncOpenAI(base_url=binding['base_url'],api_key=binding['api_key'],http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(cloud,'client',mock_client)
    result=client.post('/api/admin/models/connection-test',json=connection,headers=headers(admin)).json()
    assert result['ok'] and result['models']==['model-a']
    assert calls==[('https://models.test/v1/models','Bearer private-unsaved-key')]
    assert runtime.configuration()['connections'][1]['api_key']==''
    def fail():
        raise ValueError('private-unsaved-key')
    monkeypatch.setattr(cloud,'client',fail)
    failed=client.post('/api/admin/models/connection-test',json=connection,headers=headers(admin))
    assert not failed.json()['ok'] and 'private-unsaved-key' not in failed.text


def test_embedding_switch_requires_rebuild_and_preserves_data(client, accounts, papers, monkeypatch):
    from app import scheduler
    admin,_=accounts
    client.post('/api/profile/init',headers=headers(admin),json={'description':'agent memory','topic_ids':[3],'exclusions':[]})
    finish_interest_updates(client)
    client.post('/api/interactions',headers=headers(admin),json={'paper_id':papers[0],'action':'like'})
    config=payload(client,admin)
    config['routes']['embedding']['primary']['model']='new-embedding'
    config['embedding_dim']=8
    assert client.put('/api/admin/models',json=config,headers=headers(admin)).status_code==409
    config['rebuild_vectors']=True
    with runtime.model_snapshot():
        assert client.put('/api/admin/models',json=config,headers=headers(admin)).status_code==409
    started=[]
    monkeypatch.setattr(scheduler,'start_manual',started.append)
    response=client.put('/api/admin/models',json=config,headers=headers(admin))
    assert response.status_code==200 and response.json()['rebuild_queued'] and not response.json()['vectors_reset']
    assert started==['build_vectors'] and response.json()['pending_vectors']==3
    assert one('SELECT COUNT(*) n FROM papers')['n']==3
    assert len(vector_rows())==3
    assert len(rows('SELECT id FROM papers WHERE embedding IS NOT NULL'))==3
    assert one('SELECT embedding,content FROM interest_profile')['embedding'] is not None
    assert one('SELECT embedding_before FROM interactions')['embedding_before'] is not None
    assert one('SELECT liked FROM user_paper_state')['liked']==1
    # Restart reads the persisted dimension before creating the virtual table.
    settings.cache_clear()
    from app.db import init_db
    init_db()
    assert settings().embedding_dim==4
    assert runtime.public_configuration()['pending_rebuild']['config']['embedding_dim']==8


@pytest.mark.asyncio
async def test_vector_rebuild_finishes_all_batches_without_quality_reassessment(client, monkeypatch):
    from app.pipeline import embed
    from app.db import connect
    with connect() as db:
        db.executemany('INSERT INTO papers(arxiv_id,title,abstract,authors,created_at,ingested_date,scored) VALUES(?,?,?,?,?,?,1)',[(str(i),'title','abstract','[]',now(),today()) for i in range(1005)])
    async def forbidden(*args,**kwargs):
        raise AssertionError('existing quality must be retained')
    monkeypatch.setattr(embed.fulltext_cache,'get',forbidden)
    monkeypatch.setattr(runtime,'complete',forbidden)
    await embed.build_vectors()
    assert len(vector_rows())==1005
    assert one('SELECT COUNT(*) n FROM papers WHERE embedding IS NULL')['n']==0
