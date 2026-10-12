import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from app import retention, prompts
from app.agent import chat_history, context_compression as compression
from app.agent.tools import confirm_tool
from app.config import now, today
from app.db import connect, dumps, execute, one, rows
from app.llm import runtime
from app.llm.secrets import encrypt_configuration
from .conftest import headers


def session(client, account):
    return client.post('/api/chat/sessions',headers=headers(account),json={}).json()['id']


def add_messages(sid, count, stamp=None, size=50):
    stamp=stamp or now()
    with connect() as db:
        for n in range(count):
            db.execute('INSERT INTO chat_messages(session_id,role,content,created_at) VALUES(?,?,?,?)',
                       (sid,'user' if n%2==0 else 'assistant',f'message-{n} '+('x'*size),stamp))


def enable_compression(**overrides):
    config=runtime.configuration()
    config['routes']['chat']['primary']={'connection_id':'local','model':'main-model'}
    config['routes']['chat_compression']['primary']={'connection_id':'local','model':'summary-model'}
    config['chat_compression']=compression.CompressionSettings(**{'enabled':True,'context_window':7000,'summary_context_window':8000,
        'protect_last_n':4,'summary_max_tokens':1000,**overrides}).model_dump()
    execute("UPDATE app_settings SET value=? WHERE name='models'",(dumps(encrypt_configuration(config)),))
    return config


async def prepared(sid, uid, **kwargs):
    return [item async for item in compression.prepare(sid,uid,'system','LATEST REQUEST',[],**kwargs)]


@pytest.mark.parametrize('kind,catalog_window,control_window,override,expected', [
    ('cloud',None,None,0,256_000),
    ('cloud',0,None,0,256_000),
    ('codex',None,None,0,256_000),
    ('cloud',64_000,None,0,64_000),
    ('codex',200_000,None,0,200_000),
    ('cloud',None,128_000,0,128_000),
    ('cloud',64_000,128_000,0,64_000),
    ('cloud',64_000,None,32_000,32_000),
    ('ollama',None,None,0,16_384),
    ('ollama',128_000,None,0,16_384),
    ('ollama',128_000,None,8_192,8_192),
])
def test_context_capacity_priority_and_unknown_cloud_fallback(monkeypatch,kind,catalog_window,control_window,override,expected):
    monkeypatch.setattr('app.llm.catalog.model_info',lambda *args:{'context_window':catalog_window,'output_limit':4096})
    binding={'kind':kind,'model':'test-model','control_capabilities':{'context_window':control_window}}
    assert compression.limits(binding,override)==(expected,4096)


def test_unknown_cloud_capacity_allows_enabling_compression(client,accounts,monkeypatch):
    from app.llm.controls import capabilities
    async def discover(binding):return capabilities(binding)
    monkeypatch.setattr('app.api.model_config.discover',discover)
    account=accounts[0]
    config=client.get('/api/admin/models',headers=headers(account)).json()
    cloud=next(c for c in config['connections'] if c['id']=='cloud')
    cloud['api_key']='test-key'
    for feature in ('chat','chat_compression'):
        config['routes'][feature]={'primary':{'connection_id':'cloud','model':'unknown-'+feature,'thinking':'auto','reasoning_effort':'auto'},'fallback':None}
    config['chat_compression']['enabled']=True
    result=client.put('/api/admin/models',headers=headers(account),json=config)
    assert result.status_code==200,result.text
    saved=runtime.configuration()
    assert saved['chat_compression']['enabled']
    for feature,key in (('chat','context_window'),('chat_compression','summary_context_window')):
        assert saved['chat_compression'][key]==0
        assert compression.limits(runtime.selected(feature))[0]==256_000


def test_paginated_messages_sessions_and_owner_isolation(client, accounts):
    a,b=accounts;sid=session(client,a)
    add_messages(sid,120)
    auth=headers(a)
    first=client.get(f'/api/chat/sessions/{sid}/messages',headers=auth).json()
    assert len(first)==50 and first[0]['content'].startswith('message-70 ')
    second=client.get(f'/api/chat/sessions/{sid}/messages?before={first[0]["id"]}',headers=auth).json()
    assert len(second)==50 and second[-1]['id']<first[0]['id']
    assert client.get(f'/api/chat/sessions/{sid}/messages',headers=headers(b)).status_code==404
    for _ in range(55):session(client,a)
    latest=client.get('/api/chat/sessions',headers=auth).json()
    assert len(latest)==50
    older=client.get('/api/chat/sessions?before='+str(latest[-1]['id']),headers=auth).json()
    assert len(older)==6
    assert not {r['id'] for r in latest}.intersection(r['id'] for r in older)


@pytest.mark.asyncio
async def test_compression_is_independent_incremental_and_budgeted(client,accounts,monkeypatch):
    a=accounts[0];sid=session(client,a);uid=a['user']['id']
    enable_compression();add_messages(sid,40,size=250)
    calls=[]
    async def summarize(feature,messages,**kwargs):
        assert feature=='chat_compression'
        assert runtime.selected(feature)['model']=='summary-model'
        assert kwargs['output_tokens']==1000
        assert compression.cost(messages)+kwargs['output_tokens']+512<=8000
        calls.append(messages)
        return 'Goal: retain research constraints. Work remains pending.'
    monkeypatch.setattr(runtime,'complete',summarize)
    events=await prepared(sid,uid)
    assert calls and len(calls)<=8
    assert any(k=='compression' and v['status']=='running' for k,v in events)
    history=events[-1][1]
    text=dumps(history)
    assert all(f'message-{n} ' in text for n in (0,1,2,36,37,38,39))
    assert '不是新的请求或授权' in text
    assert compression.cost(history)<int(7000*.85)
    snapshot=one('SELECT * FROM chat_context WHERE session_id=?',(sid,))
    assert snapshot['revision']>=1 and snapshot['through_id']>3
    count=len(calls)
    again=await prepared(sid,uid)
    assert len(calls)==count and again[-1][0]=='history'
    assert one('SELECT COUNT(*) n FROM chat_context')['n']==1
    assert one('SELECT COUNT(*) n FROM chat_messages')['n']==40
    assert client.delete(f'/api/chat/sessions/{sid}',headers=headers(a)).status_code==200
    assert not one('SELECT 1 FROM chat_context')


@pytest.mark.asyncio
async def test_below_threshold_never_calls_compression(client,accounts,monkeypatch):
    a=accounts[0];sid=session(client,a);enable_compression();add_messages(sid,4)
    async def unexpected(*args,**kwargs):raise AssertionError('unnecessary model call')
    monkeypatch.setattr(runtime,'complete',unexpected)
    events=await prepared(sid,a['user']['id'])
    assert len(events)==1 and len(events[0][1])==4
    assert not one('SELECT 1 FROM chat_context')


def test_chat_stream_shows_compression_and_keeps_current_request_once(client,accounts,monkeypatch):
    from types import SimpleNamespace
    a=accounts[0];sid=session(client,a)
    enable_compression(context_window=40000,summary_context_window=16000)
    add_messages(sid,80,size=500)
    calls=[];inputs=[]
    async def summarize(feature,messages,**kwargs):
        calls.append(feature)
        return 'The user is exploring research methods. No action is authorized.'
    async def stream(messages,tools):
        inputs.append(messages)
        yield SimpleNamespace(content='最终回答',reasoning_content='',tool_calls=[])
    monkeypatch.setattr(runtime,'complete',summarize)
    monkeypatch.setattr(runtime,'stream',stream)
    result=client.post(f'/api/chat/sessions/{sid}/messages',headers=headers(a),json={'content':'CURRENT-UNIQUE-QUESTION'})
    assert 'event: compression' in result.text and '"status": "running"' in result.text
    assert '最终回答' in result.text and 'event: error' not in result.text
    assert set(calls)=={'chat_compression'}
    assert dumps(inputs[0]).count('CURRENT-UNIQUE-QUESTION')==1
    assert inputs[0][-1]=={'role':'user','content':'CURRENT-UNIQUE-QUESTION'}
    assert not one('SELECT active_until FROM chat_sessions WHERE id=?',(sid,))['active_until']
    assert one("SELECT content FROM chat_messages WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",(sid,))['content']=='最终回答'


@pytest.mark.asyncio
async def test_preparation_does_not_reuse_tools_from_current_turn_or_other_user(client,accounts,monkeypatch):
    a,b=accounts;sid=session(client,a);enable_compression();add_messages(sid,4)
    current=execute("INSERT INTO chat_messages(session_id,role,content,created_at) VALUES(?,'user','CURRENT',?)",(sid,now()))
    execute("INSERT INTO chat_messages(session_id,role,content,created_at) VALUES(?,'tool','CURRENT-TOOL',?)",(sid,now()))
    events=await prepared(sid,a['user']['id'],current_id=current,extra=[{'role':'tool','content':'CURRENT-TOOL'}])
    assert 'CURRENT' not in dumps(events[-1][1])
    with pytest.raises(ValueError,match='会话不存在'):await prepared(sid,b['user']['id'])


@pytest.mark.asyncio
async def test_compression_context_uses_actual_confirmation_state(client,accounts):
    a=accounts[0];uid=a['user']['id'];sid=session(client,a);enable_compression()
    payload=[{'id':'legacy','name':'add_watch','arguments':{'type':'keyword','value':'research'},'status':'pending'}]
    execute("INSERT INTO chat_messages(session_id,role,content,tool_calls,created_at) VALUES(?,'assistant','Please confirm',?,?)",(sid,dumps(payload),now()))
    execute('INSERT INTO pending_tools(id,user_id,session_id,name,arguments,status,created_at) VALUES(?,?,?,?,?,?,?)',('legacy',uid,sid,'add_watch','{}','confirmed',now()))
    events=await prepared(sid,uid)
    text=dumps(events[-1][1])
    assert 'confirmed' in text and '不是新的授权' in text
    assert '"status": "pending"' not in text


@pytest.mark.asyncio
async def test_expired_confirmation_cannot_apply_watch(client,accounts):
    a=accounts[0];uid=a['user']['id'];sid=session(client,a)
    old=(datetime.fromisoformat(now())-timedelta(hours=25)).isoformat()
    args={'type':'keyword','value':'not-authorized'}
    mid=execute("INSERT INTO chat_messages(session_id,role,content,tool_calls,created_at) VALUES(?,'assistant','',?,?)",
                (sid,dumps([{'id':'expired','name':'add_watch','arguments':args,'status':'pending'}]),old))
    execute('INSERT INTO pending_tools(id,user_id,session_id,name,arguments,created_at,message_id) VALUES(?,?,?,?,?,?,?)',
            ('expired',uid,sid,'add_watch',dumps(args),old,mid))
    with pytest.raises(HTTPException) as error:await confirm_tool('expired',uid,True)
    assert error.value.status_code==409
    assert one("SELECT status FROM pending_tools WHERE id='expired'")['status']=='expired'
    assert not one('SELECT 1 FROM watches WHERE user_id=?',(uid,))


@pytest.mark.asyncio
async def test_failure_uses_latest_turns_and_keeps_saved_summary(client,accounts,monkeypatch):
    a=accounts[0];uid=a['user']['id'];sid=session(client,a)
    enable_compression();add_messages(sid,200,size=250)
    execute('INSERT INTO chat_context VALUES(?,?,?,?,?,?)',(sid,'Existing summary',0,'',1,now()))
    calls=[]
    async def fail(*args,**kwargs):calls.append(1);raise ValueError('truncated')
    monkeypatch.setattr(runtime,'complete',fail)
    events=await prepared(sid,uid)
    assert calls==[1]
    assert any(k=='compression' and v['status']=='failed' for k,v in events)
    assert 'message-199 ' in dumps(events[-1][1])
    assert one('SELECT summary FROM chat_context')['summary']=='Existing summary'
    assert one('SELECT revision FROM chat_context')['revision']==1


@pytest.mark.asyncio
async def test_stop_during_summary_keeps_snapshot_and_original_messages(client,accounts,monkeypatch):
    a=accounts[0];sid=session(client,a);enable_compression();add_messages(sid,40,size=250)
    execute('INSERT INTO chat_context VALUES(?,?,?,?,?,?)',(sid,'Existing summary',0,'',1,now()))
    async def stopped(*args,**kwargs):raise asyncio.CancelledError()
    monkeypatch.setattr(runtime,'complete',stopped)
    with pytest.raises(asyncio.CancelledError):await prepared(sid,a['user']['id'])
    assert one('SELECT summary FROM chat_context')['summary']=='Existing summary'
    assert one('SELECT COUNT(*) n FROM chat_messages')['n']==40


@pytest.mark.asyncio
async def test_local_summary_respects_budget_and_rejects_truncated_output(client,monkeypatch):
    import json
    import httpx
    from app.llm.ollama import ollama
    from app.llm.provider import completion_options
    requests=[]
    def handle(request):
        if request.url.path=='/api/show':return httpx.Response(200,json={'model_info':{}})
        requests.append(json.loads(request.content))
        return httpx.Response(200,json={'message':{'content':'Incomplete summary'},'done_reason':'length'})
    original=httpx.AsyncClient
    monkeypatch.setattr('app.llm.ollama.httpx.AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    binding={'kind':'ollama','base_url':'http://summary.test','model':'small-model','feature':'chat_compression','thinking':'off','context_tokens':6000,'output_tokens':256}
    with runtime.bind(binding):
        with pytest.raises(ValueError,match='输出上限'):await ollama.chat('Summarize this conversation')
    assert requests[0]['options']['num_ctx']==6000
    assert requests[0]['options']['num_predict']==256
    assert completion_options({**binding,'kind':'cloud'},'small-model','https://api.test/v1')['max_tokens']==256


def test_chat_retention_complete_turns_quota_activity_and_empty_cap(client,accounts,monkeypatch):
    a=accounts[0];uid=a['user']['id'];old=(datetime.fromisoformat(now())-timedelta(days=366)).isoformat()
    sid=session(client,a);active=session(client,a);recent=session(client,a)
    add_messages(sid,4,old);add_messages(active,2,old);add_messages(recent,6)
    execute("INSERT INTO chat_messages(session_id,role,content,created_at) VALUES(?,'tool','tool-result',?)",(sid,old))
    execute('INSERT INTO chat_context VALUES(?,?,?,?,?,?)',(sid,'old',0,'',1,old))
    chat_history.touch(active,uid)
    monkeypatch.setattr(chat_history,'MESSAGE_LIMIT',6)
    for _ in range(5):chat_history.maintain(now())
    assert not one('SELECT 1 FROM chat_sessions WHERE id=?',(sid,))
    assert not one('SELECT 1 FROM chat_context WHERE session_id=?',(sid,))
    assert one('SELECT COUNT(*) n FROM chat_messages WHERE session_id=?',(active,))['n']==2
    remaining=rows('SELECT role FROM chat_messages WHERE session_id=? ORDER BY id',(recent,))
    assert [m['role'] for m in remaining]==['user','assistant']*2
    monkeypatch.setattr(chat_history,'EMPTY_LIMIT',2)
    with connect() as db:
        for _ in range(5):db.execute("INSERT INTO chat_sessions(user_id,title,created_at) VALUES(?,'Empty',?)",(uid,now()))
    chat_history.maintain(now())
    assert one('SELECT COUNT(*) n FROM chat_sessions s WHERE NOT EXISTS(SELECT 1 FROM chat_messages m WHERE m.session_id=s.id)')['n']==2


def test_confirmed_and_expired_proposals_survive_detail_cleanup(client,accounts):
    a=accounts[0];uid=a['user']['id'];sid=session(client,a)
    old=(datetime.fromisoformat(now())-timedelta(days=31)).isoformat()
    for name,status in [('done','confirmed'),('cancel','rejected'),('stale','pending')]:
        mid=execute("INSERT INTO chat_messages(session_id,role,content,tool_calls,created_at) VALUES(?,'assistant','',?,?)",
                    (sid,dumps([{'id':name,'name':'add_watch','arguments':{'type':'keyword','value':'test'},'status':'pending'}]),old))
        execute('INSERT INTO pending_tools(id,user_id,session_id,name,arguments,status,created_at,message_id) VALUES(?,?,?,?,?,?,?,?)',
                (name,uid,sid,'add_watch','{}',status,old,mid))
    retention.confirmations(now())
    assert not one('SELECT 1 FROM pending_tools')
    data=client.get(f'/api/chat/sessions/{sid}/messages',headers=headers(a)).json()
    assert [m['proposals'][0]['status'] for m in data]==['confirmed','rejected','expired']


def test_age_cleanup_is_batched_and_empty_sessions_expire(client,accounts):
    a=accounts[0];uid=a['user']['id'];sid=session(client,a)
    old=(datetime.fromisoformat(now())-timedelta(days=366)).isoformat()
    add_messages(sid,40,old)
    amount=chat_history.maintain(now(),batch_size=10)
    assert amount==10
    assert one('SELECT COUNT(*) n FROM chat_messages WHERE session_id=?',(sid,))['n']==30
    empty=session(client,a)
    execute('UPDATE chat_sessions SET created_at=? WHERE id=?',((datetime.fromisoformat(now())-timedelta(days=8)).isoformat(),empty))
    for _ in range(5):chat_history.maintain(now(),batch_size=10)
    assert not one('SELECT 1 FROM chat_sessions WHERE id=?',(empty,))


def test_statistics_archive_failure_never_deletes_original_events(client,accounts,papers,monkeypatch):
    from app.pipeline import metrics
    uid=accounts[0]['user']['id'];old=(datetime.fromisoformat(now())-timedelta(days=100)).isoformat()
    execute("INSERT INTO interactions(user_id,paper_id,action,created_at) VALUES(?,?,'like',?)",(uid,papers[0],old))
    original=metrics.aggregate_day
    def fail(*args,**kwargs):original(*args,**kwargs);raise ValueError('archive failed')
    monkeypatch.setattr(metrics,'aggregate_day',fail)
    with pytest.raises(ValueError):retention.interactions(now())
    assert one('SELECT COUNT(*) n FROM interactions')['n']==1
    assert not one('SELECT 1 FROM daily_metrics')


def test_frozen_aggregate_is_preserved_even_without_original_events(client,accounts):
    from datetime import date
    from app.pipeline.metrics import aggregate_day
    uid=accounts[0]['user']['id'];day=date.fromisoformat(today())-timedelta(days=100)
    execute('INSERT INTO daily_metrics(date,user_id,liked,archived) VALUES(?,?,7,1)',(day.isoformat(),uid))
    with connect() as db:aggregate_day(db,uid,day,archived=True)
    assert one('SELECT liked FROM daily_metrics WHERE date=? AND user_id=?',(day.isoformat(),uid))['liked']==7


def test_retention_freezes_statistics_keeps_saved_state_and_bounds_notifications(client,accounts,papers,monkeypatch):
    from app.pipeline.metrics import metrics
    from app.pipeline.trends import trend_stats
    a=accounts[0];uid=a['user']['id'];stamp=now();old=(datetime.fromisoformat(stamp)-timedelta(days=100)).isoformat()
    first=execute("INSERT INTO interactions(user_id,paper_id,action,created_at) VALUES(?,?,'like',?)",(uid,papers[0],old))
    execute("INSERT INTO interactions(user_id,paper_id,action,target_id,created_at) VALUES(?,?,'undo',?,?)",(uid,papers[0],first,old))
    execute("INSERT INTO interactions(user_id,paper_id,action,created_at) VALUES(?,?,'save',?)",(uid,papers[1],old))
    execute('INSERT INTO user_paper_state(user_id,paper_id,saved,updated_at) VALUES(?,?,1,?)',(uid,papers[1],stamp))
    monkeypatch.setattr(retention,'BATCH_SIZE',1)
    for _ in range(5):retention.interactions(stamp)
    assert not one('SELECT 1 FROM interactions')
    frozen=one('SELECT * FROM daily_metrics')
    assert frozen['saved']==1 and frozen['liked']==0 and frozen['archived']==1
    metrics()
    assert one('SELECT * FROM daily_metrics')==frozen
    assert one('SELECT saved FROM user_paper_state')['saved']==1
    ancient='2020-01-01'
    execute('INSERT INTO topic_daily_stats VALUES(?,?,?,?)',(ancient,3,20,80))
    execute('INSERT INTO trend_dirty VALUES(?,?,?)',(ancient,3,1))
    execute('INSERT INTO daily_metrics(date,user_id,shown) VALUES(?,?,?)',(ancient,uid,20))
    trend_stats()
    for read,days in [(1,91),(0,181),(0,100),(1,10)]:
        execute("INSERT INTO notifications(user_id,type,title,body,read,created_at) VALUES(?,'watch_hit','test','test',?,?)",
                (uid,read,(datetime.fromisoformat(stamp)-timedelta(days=days)).isoformat()))
    for _ in range(5):
        for operation in retention.phases(stamp):operation()
    assert len(rows('SELECT * FROM notifications'))==2
    assert not one('SELECT 1 FROM topic_daily_stats WHERE date=?',(ancient,))
    assert not one('SELECT 1 FROM daily_metrics WHERE date=?',(ancient,))
    assert not one('SELECT 1 FROM trend_dirty WHERE date=?',(ancient,))


def test_only_recently_ingested_papers_generate_new_alerts(client,accounts,papers):
    from app.pipeline.alerts import evaluate_alerts
    a=accounts[0];uid=a['user']['id']
    client.post('/api/watches',headers=headers(a),json={'type':'keyword','value':'memory'})
    execute('UPDATE papers SET ingested_date=? WHERE id=?',((datetime.fromisoformat(today())-timedelta(days=31)).date().isoformat(),papers[0]))
    evaluate_alerts()
    assert not one('SELECT 1 FROM notifications WHERE user_id=?',(uid,))
    execute('UPDATE papers SET ingested_date=? WHERE id=?',(today(),papers[0]))
    evaluate_alerts()
    assert one('SELECT COUNT(*) n FROM notifications WHERE user_id=?',(uid,))['n']==1
    assert evaluate_alerts()==0
    wid=one('SELECT id FROM watches WHERE user_id=?',(uid,))['id']
    assert client.delete('/api/watches/'+str(wid),headers=headers(a)).status_code==200
    assert not one('SELECT 1 FROM watches WHERE id=?',(wid,))
    new=client.post('/api/watches',headers=headers(a),json={'type':'keyword','value':'agent'}).json()['id']
    assert new>wid
    evaluate_alerts()
    assert one('SELECT 1 FROM notifications WHERE user_id=? AND dedupe_key=?',(uid,f'watch:{new}:{papers[0]}'))


def test_upgrade_and_old_clients_keep_all_previous_model_routes(client,accounts,monkeypatch):
    from app.llm.controls import capabilities
    async def discover(binding):return capabilities(binding)
    monkeypatch.setattr('app.api.model_config.discover',discover)
    a=accounts[0];config=client.get('/api/admin/models',headers=headers(a)).json()
    assert not config['chat_compression']['enabled']
    assert config['routes']['chat_compression']['primary']['model']==''
    assert 'chat_compression' in prompts.configuration()
    config.pop('chat_compression');config['routes'].pop('chat_compression')
    result=client.put('/api/admin/models',headers=headers(a),json=config)
    assert result.status_code==200,result.text
    for feature,route in config['routes'].items():
        for slot in ('primary','fallback'):
            before,after=route[slot],result.json()['routes'][feature][slot]
            if before:
                from app.llm.catalog import canonical_name
                binding=next(c for c in config['connections'] if c['id']==before['connection_id'])
                before={**before,'model':canonical_name(binding,before['model'])}
                for key in ('connection_id','model','thinking','reasoning_effort'):assert before[key]==after[key]
            else:assert not after
    updated=result.json();updated['chat_compression']['enabled']=True
    assert client.put('/api/admin/models',headers=headers(a),json=updated).status_code==400

