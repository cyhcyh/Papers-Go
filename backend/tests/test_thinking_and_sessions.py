import asyncio
import json
from types import SimpleNamespace as N

import httpx
import pytest
from fastapi import HTTPException

from app.config import now
from app.db import dumps, execute, one, rows
from app.llm import runtime
from app.llm.controls import capabilities, request_options
from app.llm.ollama import ollama
from app.llm.provider import cloud, completion_options
from app.llm.thinking import AnswerStream, clean_answer
from app.agent.chat import chat_events, _active_sessions, _active_tasks
from app.agent.tools import execute_tool, confirm_tool
from app.api.chat import delete_session
from app.interest.profile import current, put_profile
from .conftest import headers


@pytest.mark.parametrize('chunks',[
    ['<thi','nk>秘密','</th','ink>回答'],
    ['<analysis>秘密</analysis>','回答'],
    ['<think>未完成的秘密'],
    ['回答<thi'],
])
def test_split_reasoning_markers_never_leak(chunks):
    parser=AnswerStream()
    assert ''.join(parser.feed(chunk) for chunk in chunks)+parser.finish()==('' if '未完成' in ''.join(chunks) else '回答')
    assert clean_answer('旧版思考</think>最终回答')=='最终回答'


@pytest.mark.parametrize('host,model,mode,effort,expected',[
    ('https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1','deepseek-v4.1-flash','off','auto',{'extra_body':{'enable_thinking':False}}),
    ('https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1','deepseek-v4.1-flash','on','low',{'extra_body':{'enable_thinking':True},'reasoning_effort':'low'}),
    ('https://api.deepseek.com/v1','deepseek-flash','on','max',{'extra_body':{'thinking':{'type':'enabled'}},'reasoning_effort':'max'}),
    ('https://unknown.example/v1','unknown-model','auto','auto',{}),
])
def test_controls_use_provider_protocol(host,model,mode,effort,expected):
    binding={'kind':'cloud','base_url':host,'model':model,'thinking':mode,'reasoning_effort':effort}
    assert request_options(binding)==expected
    if mode=='off':
        assert completion_options({**binding,'feature':'classify'},model,host)['max_tokens']==512
    if mode=='on':
        assert completion_options({**binding,'feature':'classify'},model,host)['max_tokens']==4096


def test_controls_saved_per_feature_and_unsupported_rejected(client,accounts,monkeypatch):
    async def discover(binding):return capabilities(binding)
    monkeypatch.setattr('app.api.model_config.discover',discover)
    admin,other=accounts
    auth=headers(admin)
    config=client.get('/api/admin/models',headers=auth).json()
    config['connections'][1].update(base_url='https://api.deepseek.com/v1',api_key='secret-key-for-test')
    config['routes']['brief']['fallback']['model']='deepseek-flash'
    config['routes']['chat']['primary'].update(model='deepseek-flash',thinking='on',reasoning_effort='max')
    config['routes']['classify']['primary'].update(connection_id='cloud',model='deepseek-flash',thinking='off')
    result=client.put('/api/admin/models',headers=auth,json=config)
    assert result.status_code==200
    assert 'secret-key-for-test' not in result.text
    assert 'secret-key-for-test' not in one("SELECT value FROM app_settings WHERE name='models'")['value']
    assert runtime.selected('chat')['reasoning_effort']=='max'
    assert runtime.selected('classify')['thinking']=='off'
    config['routes']['chat']['primary']['reasoning_effort']='medium'
    assert client.put('/api/admin/models',headers=auth,json=config).status_code==400
    assert client.post('/api/admin/models/capabilities',headers=headers(other),json={'connection':config['connections'][1],'model':'deepseek-flash'}).status_code==403


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,effort,expected',[('off','auto',False),('on','auto',True),('on','high','high')])
async def test_local_chat_thinking_and_native_tool_round(client,monkeypatch,mode,effort,expected):
    requests=[]
    def handle(request):
        if request.url.path=='/api/show':
            return httpx.Response(200,json={'model_info':{}})
        requests.append(json.loads(request.content))
        return httpx.Response(200,text=dumps({'message':{'content':'答案','thinking':'隐藏推理','tool_calls':[{'function':{'name':'show_profile','arguments':{}}}]},'done':True})+'\n')
    original=httpx.AsyncClient
    monkeypatch.setattr('app.llm.ollama.httpx.AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    binding={'kind':'ollama','model':'qwen3:4b' if effort=='auto' else 'gpt-oss:20b','base_url':'http://local.test','thinking':mode,'reasoning_effort':effort}
    messages=[{'role':'user','content':'问题'},{'role':'assistant','content':None,'reasoning_content':'协议需要的思考','tool_calls':[{'id':'call1','function':{'name':'show_profile','arguments':'{}'},'type':'function'}]},{'role':'tool','tool_call_id':'call1','content':'结果'}]
    with runtime.bind(binding):
        deltas=[d async for d in ollama.stream(messages,[])]
    assert requests[0]['think']==expected
    assert requests[0]['messages'][1]['thinking']=='协议需要的思考'
    assert requests[0]['messages'][1]['tool_calls'][0]['function']['arguments']=={}
    assert requests[0]['messages'][2]['tool_name']=='show_profile'
    assert ('/no_think' in requests[0]['messages'][0]['content'])==(mode=='off')
    assert deltas[0].reasoning_content=='隐藏推理'
    assert deltas[0].tool_calls[0].function.arguments=='{}'
    assert messages[1]['content'] is None


def test_reasoning_hidden_in_stream_saved_history_and_next_turn(client,accounts,monkeypatch):
    account=accounts[0];auth=headers(account)
    session=client.post('/api/chat/sessions',headers=auth,json={}).json()['id']
    execute("INSERT INTO chat_messages(session_id,role,content,created_at) VALUES(?,'assistant',?,?)",(session,'<think>旧秘密</think>旧答案',now()))
    captured=[]
    async def stream(messages,tools):
        captured.extend(messages)
        yield N(content='<thi',reasoning_content='独立秘密',tool_calls=[])
        yield N(content='nk>新秘密</think>**最终答案**',tool_calls=[])
    monkeypatch.setattr(cloud,'stream',stream)
    response=client.post(f'/api/chat/sessions/{session}/messages',headers=auth,json={'content':'你好'})
    assert response.status_code==200 and '最终答案' in response.text
    assert '秘密' not in response.text
    assert '旧秘密' not in dumps(captured)
    assert '新秘密' not in dumps(rows('SELECT * FROM chat_messages WHERE session_id=?',(session,)))
    history=client.get(f'/api/chat/sessions/{session}/messages',headers=auth).json()
    assert history[0]['content']=='旧答案'


@pytest.mark.asyncio
@pytest.mark.parametrize('chunks',[
    [{'content':'隐式模板打开的秘密'}, {'content':'</thi'}, {'content':'nk>最终答案'}],
    [{'thinking':'独立的秘密','content':''}, {'content':'最终答案'}],
    [{'thinking':'','content':'最终答案'}],
    [{'content':'隐式模板打开但没有结束的秘密'}],
])
async def test_thinking_only_ollama_never_leaks_and_keeps_final_answer(client,monkeypatch,chunks):
    requests=[]
    def handle(request):
        if request.url.path=='/api/show':
            return httpx.Response(200,json={'model_info':{'general.finetune':'Thinking'}})
        requests.append(json.loads(request.content))
        data=[{'message':message,'done':False} for message in chunks]+[{'message':{},'done':True}]
        return httpx.Response(200,text='\n'.join(dumps(x) for x in data)+'\n')
    original=httpx.AsyncClient
    monkeypatch.setattr('app.llm.ollama.httpx.AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    with runtime.bind({'kind':'ollama','model':'qwen3:4b','base_url':'http://local.test','thinking':'off'}):
        deltas=[d async for d in ollama.stream([{'role':'user','content':'问题'}],[])]
    answer=''.join(d.content for d in deltas)
    assert '秘密' not in answer
    assert answer==('' if len(chunks)==1 and 'thinking' not in chunks[0] else '最终答案')
    assert requests[0]['think'] is True
    assert '/no_think' not in requests[0]['messages'][0]['content']


def test_empty_reasoning_reply_reports_error_instead_of_silent_bubble(client,accounts,monkeypatch):
    sid=client.post('/api/chat/sessions',headers=headers(accounts[0]),json={}).json()['id']
    async def stream(*args):
        yield N(content='',reasoning_content='没有最终回复的秘密',tool_calls=[])
    monkeypatch.setattr(cloud,'stream',stream)
    result=client.post(f'/api/chat/sessions/{sid}/messages',headers=headers(accounts[0]),json={'content':'你好'})
    assert 'event: error' in result.text and '秘密' not in result.text
    assert not one("SELECT * FROM chat_messages WHERE session_id=? AND role='assistant'",(sid,))


@pytest.mark.asyncio
async def test_agent_identity_and_admin_tools_are_enforced(client,accounts):
    admin,other=accounts
    sid=client.post('/api/chat/sessions',headers=headers(admin),json={}).json()['id']
    foreign=client.post('/api/chat/sessions',headers=headers(other),json={}).json()['id']
    for name in ('admin_models','run_pipeline','execute_sql','read_file','shell'):
        with pytest.raises(ValueError,match='未知工具'):
            await execute_tool(name,{},admin['user']['id'],sid,None)
    with pytest.raises(ValueError,match='登录信息'):
        await execute_tool('show_profile',{'user_id':other['user']['id']},admin['user']['id'],sid,None)
    with pytest.raises(HTTPException) as error:
        await execute_tool('show_profile',{},admin['user']['id'],foreign,None)
    assert error.value.status_code==404
    proposal=await execute_tool('add_watch',{'type':'keyword','value':'memory'},admin['user']['id'],sid,None)
    ident=proposal['proposal']['id']
    assert client.post('/api/chat/confirm',headers=headers(other),json={'id':ident,'approve':True}).status_code==404
    for path in ('messages','stop'):
        method=client.get if path=='messages' else client.post
        assert method(f'/api/chat/sessions/{sid}/{path}',headers=headers(other)).status_code==404
    assert client.delete(f'/api/chat/sessions/{sid}',headers=headers(other)).status_code==404
    assert client.delete(f'/api/chat/sessions/{sid}',headers=headers(admin)).status_code==200
    assert not one('SELECT * FROM pending_tools WHERE id=?',(ident,))
    assert client.post('/api/chat/confirm',headers=headers(admin),json={'id':ident,'approve':True}).status_code==404


@pytest.mark.asyncio
async def test_delete_stops_live_reply_and_keeps_confirmed_profile(client,accounts,monkeypatch):
    account=accounts[0];uid=account['user']['id'];auth=headers(account)
    sid=client.post('/api/chat/sessions',headers=auth,json={}).json()['id']
    put_profile(uid,'原兴趣',{},'manual')
    proposal=await execute_tool('update_interest',{'patch':'新兴趣'},uid,sid,current(uid))
    await confirm_tool(proposal['proposal']['id'],uid,True)
    started=asyncio.Event()
    async def stream(messages,tools):
        yield N(content='保留的回答',tool_calls=[])
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(cloud,'stream',stream)
    async def consume():
        async for _ in chat_events(sid,uid,'继续'):
            pass
    _active_sessions.add(sid)
    task=asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(),2)
    await delete_session(sid,account['user'])
    assert task.cancelled() and sid not in _active_sessions and sid not in _active_tasks
    assert not rows('SELECT * FROM chat_messages WHERE session_id=?',(sid,))
    assert not one('SELECT * FROM chat_sessions WHERE id=?',(sid,))
    assert current(uid)['content']=='新兴趣'
