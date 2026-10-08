from .vector_helpers import vector_rows,embedding
import asyncio
import copy
import json
import time
from types import SimpleNamespace
import httpx
import pytest
from openai import AsyncOpenAI
from app.config import settings,now,today
from app.db import connect,execute,one,rows
from app.llm import runtime,catalog,codex,vector_rebuild,embedding_queue
from app.llm.ollama import ollama
from app.llm.provider import cloud
from .conftest import headers


def persist(config):
    from app.llm.secrets import encrypt_configuration
    execute("INSERT OR REPLACE INTO app_settings VALUES('models',?,?)",(json.dumps(encrypt_configuration(config)),now()))


def authorize(ident='codex',expired=False):
    tokens={'access_token':'private-access-token','refresh_token':'private-refresh-token','expires_at':time.time()+(-10 if expired else 3600),'client_id':codex.CLIENT_ID}
    execute('INSERT OR REPLACE INTO codex_auth(connection_id,tokens,generation,status,updated_at) VALUES(?,?,?,?,?)',(ident,codex.seal(tokens),'generation','ready',now()))


def codex_config():
    config=runtime.configuration()
    config['connections'].append({'id':'codex','name':'Codex','kind':'codex','base_url':codex.BASE_URL,'api_key':''})
    config['routes']['chat']={'primary':{'connection_id':'codex','model':'test-codex','thinking':'on','reasoning_effort':'xhigh'},'fallback':None}
    return config


def test_catalog_native_fields_and_embedding_specs(client):
    connection={'id':'codex','kind':'codex','base_url':codex.BASE_URL}
    item=catalog.metadata({'slug':'gpt-test','display_name':'Test','supported_reasoning_levels':[{'effort':'low'},{'effort':'xhigh'},{'effort':'none'}],'default_reasoning_level':'low'},connection)
    assert item['reasoning_efforts']==['low','xhigh','none'] and item['thinking_modes']==['auto','on','off']
    assert item['default_reasoning_effort']=='low' and item['capability_source']=='api'
    assert catalog.metadata({'slug':'hidden','visibility':'hide'},connection) is None
    info=catalog.metadata({'id':'text-embedding-v4'},{'kind':'cloud','base_url':'https://dashscope.aliyuncs.com/compatible-mode/v1'})
    assert info['type']=='embedding' and info['embedding_batch_size']==10 and 1024 in info['embedding_dimensions']
    assert info['dimensions_parameter'] and info['thinking_modes']==['auto']


def test_refresh_persists_uses_saved_key_and_overrides(client,accounts,monkeypatch):
    admin,regular=accounts
    config=runtime.public_configuration();connection=config['connections'][1]
    connection.update(api_key='private-catalog-key',credential_revision='revision-test',base_url='https://models.test/v1')
    calls=[]
    def handle(request):
        calls.append(request.headers['authorization'])
        return httpx.Response(200,json={'object':'list','data':[{'id':'model-a','object':'model','created':0,'owned_by':'test','supported_reasoning_efforts':['low','xhigh']}]})
    monkeypatch.setattr(cloud,'client',lambda:AsyncOpenAI(base_url=runtime.current_binding()['base_url'],api_key=runtime.current_binding()['api_key'],http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))))
    assert client.post('/api/admin/models/refresh',json=connection,headers=headers(regular)).status_code==403
    response=client.post('/api/admin/models/refresh',json=connection,headers=headers(admin))
    assert response.json()['ok'] and response.json()['models'][0]['reasoning_efforts']==['low','xhigh'],response.text
    config['connections'][1]=connection
    for route in config['routes'].values():
        for choice in (route['primary'],route['fallback']):
            if choice and choice['connection_id']==connection['id']:choice['thinking']='auto'
    response=client.put('/api/admin/models',json=config,headers=headers(admin));assert response.status_code==200,response.text
    saved=response.json()['connections'][1]
    assert saved['catalog']['models'][0]['id']=='model-a' and 'private-catalog-key' not in response.text
    assert client.post('/api/admin/models/refresh',json=saved,headers=headers(admin)).json()['ok']
    assert calls==['Bearer private-catalog-key']*2
    override={'connection':saved,'model':'model-a','thinking_modes':['auto','on'],'reasoning_efforts':['high'],'protocol':'openai','type':'chat'}
    r=client.post('/api/admin/models/capability-override',json=override,headers=headers(admin));assert r.status_code==200,r.text
    assert r.json()['models'][0]['capability_source']=='manual'
    assert client.post('/api/admin/models/refresh',json=saved,headers=headers(admin)).json()['models'][0]['reasoning_efforts']==['high']
    def fail():raise ValueError('private-catalog-key')
    monkeypatch.setattr(cloud,'client',fail)
    failed=client.post('/api/admin/models/refresh',json=saved,headers=headers(admin))
    assert not failed.json()['ok'] and failed.json()['models'][0]['id']=='model-a'
    assert 'private-catalog-key' not in failed.text


@pytest.mark.asyncio
async def test_cloud_embedding_batches_dimensions_and_usage(client,monkeypatch):
    config=runtime.configuration();config['embedding_dim']=4
    config['connections'][1].update(api_key='embed-key',base_url='https://dashscope.aliyuncs.com/compatible-mode/v1')
    config['routes']['embedding']={'primary':{'connection_id':'cloud','model':'text-embedding-v4'},'fallback':None};persist(config)
    batches=[]
    def handle(request):
        body=json.loads(request.content);batches.append(body)
        return httpx.Response(200,json={'model':body['model'],'object':'list','data':[{'object':'embedding','index':i,'embedding':[float(text)+1,0,0,0]} for i,text in reversed(list(enumerate(body['input'])))],'usage':{'prompt_tokens':len(body['input']),'total_tokens':len(body['input'])}})
    monkeypatch.setattr(cloud,'client',lambda:AsyncOpenAI(base_url=runtime.current_binding()['base_url'],api_key='embed-key',http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))))
    vectors=await runtime.embed([str(i) for i in range(23)])
    assert [len(b['input']) for b in batches]==[10,10,3]
    assert all(b['dimensions']==4 for b in batches)
    assert [v[0] for v in vectors]==list(range(1,24))
    assert one('SELECT SUM(input_tokens) n FROM llm_usage')['n']==23
    assert not rows('SELECT * FROM embedding_permits')


@pytest.mark.asyncio
async def test_global_reserve_interactive_priority_and_cancellation(client):
    binding={'kind':'cloud','base_url':'https://embed.test/v1'};config={'embedding_cloud_concurrency':2}
    assert embedding_queue.claim(binding,config,'bg','background')
    assert not embedding_queue.claim(binding,config,'bg2','background')
    assert embedding_queue.claim(binding,config,'front','interactive')
    assert not embedding_queue.claim(binding,config,'front2','interactive')
    execute('DELETE FROM embedding_permits');execute('DELETE FROM embedding_waiters')
    config={'embedding_cloud_concurrency':1}
    assert embedding_queue.claim(binding,config,'occupant','background')
    assert not embedding_queue.claim(binding,config,'front','interactive')
    execute("DELETE FROM embedding_permits WHERE id='occupant'")
    assert not embedding_queue.claim(binding,config,'bg','background')
    assert embedding_queue.claim(binding,config,'front','interactive')
    entered=asyncio.Event()
    async def waiter():
        async with embedding_queue.permit(binding,config):entered.set()
    task=asyncio.create_task(waiter());await asyncio.sleep(.15);task.cancel()
    await asyncio.gather(task,return_exceptions=True)
    assert not entered.is_set()
    assert len(rows('SELECT * FROM embedding_waiters'))==1 # Only the explicit bg claim remains.
    execute('DELETE FROM embedding_permits');execute('DELETE FROM embedding_waiters')
    async with embedding_queue.permit(binding,config):assert len(rows('SELECT * FROM embedding_permits'))==1
    assert not rows('SELECT * FROM embedding_permits')


@pytest.mark.asyncio
async def test_staged_rebuild_failure_stop_resume_atomic_switch(client,papers,monkeypatch):
    config=runtime.configuration();config['embedding_dim']=8;config['routes']['embedding']['primary']['model']='new-model'
    old=embedding(papers[0]);vector_rebuild.set_pending(config)
    async def fail(texts):raise ValueError('embedding unavailable')
    monkeypatch.setattr(ollama,'embed',fail)
    with pytest.raises(ValueError):await vector_rebuild.run()
    assert vector_rebuild.state()['status']=='failed' and runtime.configuration()['embedding_dim']==4
    assert embedding(papers[0])==old
    entered=asyncio.Event()
    async def wait(texts):entered.set();await asyncio.Event().wait()
    monkeypatch.setattr(ollama,'embed',wait);task=asyncio.create_task(vector_rebuild.run());await entered.wait();task.cancel()
    await asyncio.gather(task,return_exceptions=True)
    assert vector_rebuild.state()['status']=='stopped' and len(vector_rows())==3
    async def embed(texts):return [[1.,0,0,0,0,0,0,0] for text in texts]
    monkeypatch.setattr(ollama,'embed',embed)
    await vector_rebuild.run()
    assert vector_rebuild.pending() is None and runtime.configuration()['embedding_dim']==8
    assert len(vector_rows())==3 and len(embedding(papers[0]))==32
    assert not rows('SELECT * FROM embedding_rebuild_papers')


@pytest.mark.asyncio
async def test_rebuild_includes_new_and_changed_rows_and_prevents_old_profile_write(client,accounts,papers,monkeypatch):
    from app.interest.profile import put_profile
    admin,_=accounts
    user_id=admin['user']['id'];put_profile(user_id,'## 核心兴趣\n- [w:0.8] graph',{},'manual')
    config=runtime.configuration();old_config=copy.deepcopy(config);config['routes']['embedding']['primary']['model']='new-model'
    vector_rebuild.set_pending(config);calls=0
    async def embed(texts):
        nonlocal calls
        calls+=1
        if calls==1:
            execute('UPDATE papers SET abstract=? WHERE id=?',('new abstract',papers[0]))
            execute('INSERT INTO papers(arxiv_id,title,abstract,authors,created_at,ingested_date) VALUES(?,?,?,?,?,?)',('new-paper','new','abstract','[]',now(),today()))
        return [[0.,0.,0.,1.] for _ in texts]
    monkeypatch.setattr(ollama,'embed',embed);await vector_rebuild.run()
    assert len(vector_rows())==4 and one('SELECT embedding FROM interest_profile')['embedding'] is not None
    with runtime.model_snapshot(old_config):put_profile(user_id,'## 核心兴趣\n- [w:0.8] old request',{},'manual',b'old-vector-000000')
    assert one('SELECT embedding FROM interest_profile ORDER BY version DESC')['embedding'] is None


@pytest.mark.asyncio
async def test_codex_device_login_encryption_and_refresh_singleflight(client,monkeypatch):
    original=httpx.AsyncClient;polls=0;refreshes=0
    async def handle(request):
        nonlocal polls,refreshes
        if request.url.path.endswith('/usercode'):return httpx.Response(200,json={'device_auth_id':'private-device-token','user_code':'ABCD-1234','interval':3})
        if request.url.path.endswith('/deviceauth/token'):
            polls+=1
            return httpx.Response(404) if polls==1 else httpx.Response(200,json={'authorization_code':'private-code','code_verifier':'private-verifier'})
        if b'refresh_token' in request.content:
            refreshes+=1;await asyncio.sleep(.05)
        return httpx.Response(200,json={'access_token':'private-access-token','refresh_token':'private-refresh-token','expires_in':3600})
    monkeypatch.setattr(codex.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    login=await codex.start_login('codex');assert login['status']=='waiting' and login['user_code']=='ABCD-1234'
    assert 'private-device-token' not in one('SELECT device FROM codex_auth')['device']
    def ready_poll():
        row=one('SELECT device FROM codex_auth');device=codex.unseal(row['device']);device['next_poll']=0;execute('UPDATE codex_auth SET device=?',(codex.seal(device),))
    ready_poll();assert (await codex.poll_login('codex'))['status']=='waiting'
    ready_poll();assert (await codex.poll_login('codex'))['logged_in']
    assert 'private-access-token' not in one('SELECT tokens FROM codex_auth')['tokens']
    assert 'private-refresh-token' not in json.dumps(codex.status('codex'))
    authorize(expired=True)
    results=await asyncio.gather(codex.credentials('codex'),codex.credentials('codex'))
    assert refreshes==1 and all(r['access_token']=='private-access-token' for r in results)
    codex.logout('codex')
    with pytest.raises(ValueError):await codex.credentials('codex')


@pytest.mark.asyncio
async def test_codex_logout_during_device_completion_cannot_restore_tokens(client,monkeypatch):
    original=httpx.AsyncClient
    async def handle(request):
        if request.url.path.endswith('/usercode'):return httpx.Response(200,json={'device_auth_id':'d','user_code':'code','interval':3})
        if request.url.path.endswith('/deviceauth/token'):return httpx.Response(200,json={'authorization_code':'a','code_verifier':'v'})
        codex.logout('codex');return httpx.Response(200,json={'access_token':'do-not-restore','refresh_token':'r'})
    monkeypatch.setattr(codex.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    await codex.start_login('codex');device=codex.unseal(one('SELECT device FROM codex_auth')['device']);device['next_poll']=0;execute('UPDATE codex_auth SET device=?',(codex.seal(device),))
    assert not (await codex.poll_login('codex'))['logged_in']
    assert not rows('SELECT * FROM codex_auth')


@pytest.mark.asyncio
async def test_codex_stream_reasoning_text_tools_and_independent_usage(client,monkeypatch):
    authorize();persist(codex_config());original=httpx.AsyncClient;requests=[]
    events=[{'type':'response.reasoning_summary_text.delta','delta':'private thinking'}, {'type':'response.output_text.delta','delta':'Hello'}, {'type':'response.output_item.added','output_index':1,'item':{'type':'function_call','call_id':'call1','name':'search_papers','arguments':''}}, {'type':'response.function_call_arguments.delta','output_index':1,'delta':'{"query":"graph"}'}, {'type':'response.completed','response':{'status':'completed','output':None,'usage':{'input_tokens':12,'output_tokens':9}}}]
    def handle(request):
        requests.append(json.loads(request.content));return httpx.Response(200,text=''.join('data: '+json.dumps(e)+'\n\n' for e in events),headers={'content-type':'text/event-stream'})
    monkeypatch.setattr(codex.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    tools=[{'type':'function','function':{'name':'search_papers','description':'search','parameters':{'type':'object','properties':{'query':{'type':'string'}}}}}]
    deltas=[d async for d in runtime.stream([{'role':'system','content':'research instructions'},{'role':'user','content':'hello'}],tools)]
    assert ''.join(d.content or '' for d in deltas)=='Hello'
    assert deltas[0].reasoning_content=='private thinking' and deltas[-1].tool_calls[0].function.arguments=='{"query":"graph"}'
    assert requests[0]['store'] is False and requests[0]['stream'] and requests[0]['reasoning']=={'effort':'xhigh'}
    assert requests[0]['tools'][0]['name']=='search_papers' and 'instructions' in requests[0]
    assert one('SELECT billing_source,input_tokens FROM llm_usage')=={'billing_source':'subscription','input_tokens':12}
    from app.api.site import _compute_overview
    overview=_compute_overview(7);assert not overview['by_model'] and overview['subscription_usage'][0]['input_tokens']==12


@pytest.mark.asyncio
@pytest.mark.parametrize('ending',['truncated','failed','empty','final-only'])
async def test_codex_incomplete_and_completed_output(client,monkeypatch,ending):
    authorize();persist(codex_config());original=httpx.AsyncClient
    if ending=='truncated':event={'type':'response.output_text.delta','delta':'partial'}
    elif ending=='failed':event={'type':'response.incomplete','response':{'usage':{'input_tokens':10,'output_tokens':4}}}
    else:event={'type':'response.completed','response':{'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'final'}]}] if ending=='final-only' else None}}
    monkeypatch.setattr(codex.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(lambda r:httpx.Response(200,text='data: '+json.dumps(event)+'\n\n')),**kw))
    if ending=='final-only':assert ''.join([d.content or '' async for d in runtime.stream([],[])])=='final'
    else:
        with pytest.raises(ValueError):_=[d async for d in runtime.stream([],[])]


@pytest.mark.asyncio
async def test_reading_queue_codex_uses_remote_limit(client,papers,monkeypatch):
    from app.pipeline import reading_queue,read
    config=codex_config();config['routes']['reading_l2']['primary']={'connection_id':'codex','model':'test-codex'};config['reading_cloud_concurrency']=1;persist(config)
    done=asyncio.Event()
    async def run(paper_id,level):await done.wait()
    monkeypatch.setattr(read,'run_card',run)
    try:
        reading_queue.enqueue(papers[0]);reading_queue.enqueue(papers[1]);reading_queue.dispatch()
        assert one("SELECT COUNT(*) n FROM reading_jobs WHERE status='running'")['n']==1
        assert one("SELECT COUNT(*) n FROM reading_jobs WHERE status='queued' AND kind='cloud'")['n']==1
        assert reading_queue.queue_info(papers[1])['queue_ahead']==0
    finally:
        for task in list(read._tasks.values()):task.cancel()
        await asyncio.gather(*list(read._tasks.values()),return_exceptions=True)
        read._tasks.clear()


def test_successful_refresh_removes_delisted_models_without_changing_routes(client,accounts,monkeypatch):
    admin,_=accounts
    connection=runtime.public_configuration()['connections'][1]
    connection.update(api_key='test',credential_revision='revision')
    versions=[[{'id':'old-model'},{'id':'kept-model'}],[{'id':'kept-model'},{'id':'new-model'}]]
    async def fetch(connection):return [catalog.metadata(entry,connection) for entry in versions.pop(0)]
    monkeypatch.setattr(catalog,'fetch',fetch)
    first=client.post('/api/admin/models/refresh',json=connection,headers=headers(admin)).json()
    second=client.post('/api/admin/models/refresh',json=connection,headers=headers(admin)).json()
    assert {m['id'] for m in first['models']}=={'old-model','kept-model'}
    assert {m['id'] for m in second['models']}=={'kept-model','new-model'}
    assert 'old-model' not in one('SELECT models FROM model_catalogs')['models']
    assert runtime.configuration()['routes']['chat']['primary']['model']!='new-model'


@pytest.mark.asyncio
async def test_codex_401_refresh_retries_before_any_output(client,monkeypatch):
    authorize();persist(codex_config());original=httpx.AsyncClient;response_calls=0;refresh_calls=0
    def handle(request):
        nonlocal response_calls,refresh_calls
        if request.url.path.endswith('/oauth/token'):
            refresh_calls+=1;return httpx.Response(200,json={'access_token':'refreshed-access','refresh_token':'rotated-refresh','expires_in':3600})
        response_calls+=1
        if response_calls==1:return httpx.Response(401)
        assert request.headers['authorization']=='Bearer refreshed-access'
        return httpx.Response(200,text='data: '+json.dumps({'type':'response.completed','response':{'status':'completed','output':[{'content':[{'type':'output_text','text':'ok'}]}],'usage':{'input_tokens':1,'output_tokens':1}}})+'\n\n')
    monkeypatch.setattr(codex.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    assert ''.join([d.content or '' async for d in runtime.stream([],[])])=='ok'
    assert response_calls==2 and refresh_calls==1 and len(rows('SELECT * FROM llm_usage'))==1


def test_codex_refresh_sends_compatible_version_and_uses_real_capabilities(client,accounts,monkeypatch):
    admin,_=accounts
    authorize()
    original=httpx.AsyncClient
    calls=[]
    def handle(request):
        calls.append(request)
        assert request.url.params['client_version']==codex.CLIENT_VERSION=='0.160.0'
        assert request.headers['accept']=='application/json'
        assert request.headers['authorization']=='Bearer private-access-token'
        return httpx.Response(200,json={'models':[
            {'slug':'codex-auto-review','visibility':'hide','supported_reasoning_levels':[{'effort':'high'}]},
            {'slug':'gpt-live-model','visibility':'list','display_name':'Live model',
             'supported_reasoning_levels':[{'effort':'low'},{'effort':'max'}],
             'default_reasoning_level':'low','context_window':272000},
        ]})
    monkeypatch.setattr(catalog.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    from app.llm import codex_updates
    async def validate(*args):return 'gpt-live-model'
    monkeypatch.setattr(codex_updates,'validate',validate)
    connection={'id':'codex','name':'Codex','kind':'codex','base_url':codex.BASE_URL,'codex_auto_update':False}
    response=client.post('/api/admin/models/refresh',json=connection,headers=headers(admin))
    assert response.status_code==200 and response.json()['ok'],response.text
    models=response.json()['models']
    assert [m['id'] for m in models]==['gpt-live-model']
    assert models[0]['reasoning_efforts']==['low','max']
    assert models[0]['thinking_modes']==['auto','on']
    assert models[0]['default_reasoning_effort']=='low' and models[0]['context_window']==272000
    assert catalog.saved(connection)['models']==models
    assert len(calls)==1 and 'private-access-token' not in response.text


def test_codex_hidden_only_catalog_failure_preserves_saved_models(client,accounts,monkeypatch):
    admin,_=accounts
    authorize()
    connection={'id':'codex','name':'Codex','kind':'codex','base_url':codex.BASE_URL,'codex_auto_update':False}
    existing=catalog.metadata({'slug':'previous-model','visibility':'list'},connection)
    catalog.store(connection,[existing])
    original=httpx.AsyncClient
    monkeypatch.setattr(catalog.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(
        lambda request:httpx.Response(200,json={'models':[{'slug':'codex-auto-review','visibility':'hide'}]})),**kw))
    response=client.post('/api/admin/models/refresh',json=connection,headers=headers(admin))
    assert response.status_code==200 and not response.json()['ok']
    assert [m['id'] for m in response.json()['models']]==['previous-model']
    assert [m['id'] for m in catalog.saved(connection)['models']]==['previous-model']
