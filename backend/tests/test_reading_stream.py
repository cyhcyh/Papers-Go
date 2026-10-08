import asyncio
import json
from types import SimpleNamespace
import httpx
import pytest
from openai import AsyncOpenAI
from app.config import now
from app.db import execute,one,rows,dumps
from app.llm import runtime as models
from app.llm.provider import cloud
from app.llm.ollama import ollama
from app.llm.reading_json import ReadingJSONStream,ANSWER_KEYS
from app.pipeline import read
from .conftest import headers
from .test_reading_v2 import card_value


def use_cloud(model='deepseek-v4.1-flash',thinking='off'):
    config=models.legacy_defaults()
    config['connections'][1]['api_key']='test-key'
    for feature in ('reading_l2','reading_l3'):
        config['routes'][feature]={'primary':{'connection_id':'cloud','model':model,'thinking':thinking},'fallback':None}
    execute("INSERT OR REPLACE INTO app_settings VALUES('models',?,?)",(dumps(config),now()))


@pytest.mark.parametrize('level',['L2','L3'])
@pytest.mark.asyncio
async def test_large_paper_is_read_once_and_all_pages_and_appendices_are_present(client,papers,monkeypatch,level):
    use_cloud()
    pages=[{'section':f'page {i+1}','text':f'Page {i+1} '+'full paper '*1500} for i in range(6)]
    pages[-1]['text']+=' APPENDIX PROOF MARKER'
    async def parse(p):return {'text':'\n'.join(p['text'] for p in pages),'pages':pages}
    called=[]
    async def stream(messages,tools,**kwargs):
        called.append((messages,kwargs))
        yield SimpleNamespace(content=dumps(card_value()),tool_calls=None)
    async def forbidden(*args,**kwargs):pytest.fail('Direct reading must not summarize sections')
    monkeypatch.setattr(read,'ensure_fulltext',parse)
    monkeypatch.setattr(models,'stream',stream);monkeypatch.setattr(models,'complete',forbidden)
    execute("INSERT INTO reading_cards(paper_id,status,created_at) VALUES(?,'pending',?)",(papers[0],now()))
    card=await read.generate_card(papers[0],level)
    assert len(called)==1 and called[0][1]['feature']==('reading_l3' if level=='L3' else 'reading_l2')
    text=called[0][0][-1]['content']
    assert all(page['text'] in text for page in pages)
    assert card['reading_level']==level
    progress=json.loads(one('SELECT progress_json FROM reading_cards')['progress_json'])
    assert progress['mode']=='direct' and progress['stage']=='ready'


def test_incremental_json_strings_are_visible_before_the_closing_quote():
    value=card_value();value['answers']['method']='Unicode \U0001f600 与公式 $x^2$。\n| 方法 | 结果 |\n| --- | --- |\n| A | 1 |'
    raw=json.dumps({'answers':value['answers'],'paper_kind':'theoretical'},ensure_ascii=True)
    parser=ReadingJSONStream();seen=False
    for char in raw:
        parser.feed(char)
        if parser.answers['method']=='Unicode ':seen=True
    assert seen and parser.answers==value['answers'] and parser.paper_kind=='theoretical'


@pytest.mark.asyncio
async def test_partial_answer_precedes_completion_and_disconnect_does_not_cancel_shared_job(client,papers,monkeypatch):
    use_cloud();started=asyncio.Event();release=asyncio.Event();calls=0
    async def parse(p):return {'text':'public fulltext','pages':[{'section':'page 1','text':'public fulltext'}]}
    value={'answers':card_value()['answers'],**{k:v for k,v in card_value().items() if k!='answers'}}
    raw=dumps(value);cut=raw.index('比较已有方法')
    async def stream(*args,**kwargs):
        nonlocal calls
        calls+=1
        yield SimpleNamespace(content='<thi',tool_calls=None)
        yield SimpleNamespace(content='nk>hidden reasoning</think>'+raw[:cut],tool_calls=None)
        await asyncio.sleep(.3)
        yield SimpleNamespace(content='',reasoning_content='another secret',tool_calls=None)
        started.set();await release.wait()
        yield SimpleNamespace(content=raw[cut:],tool_calls=None)
    monkeypatch.setattr(read,'ensure_fulltext',parse);monkeypatch.setattr(models,'stream',stream)
    read.request_card(papers[0]);task=read._tasks[papers[0]]
    try:
        await started.wait()
        assert one('SELECT status FROM reading_cards')['status']=='pending'
        events=read.card_events(papers[0]);first=await anext(events)
        assert '研究一个图论问题' in first and 'hidden reasoning' not in first and 'another secret' not in first
        await events.aclose()
        assert not task.done()
        read.request_card(papers[0]);assert read._tasks[papers[0]] is task and calls==1
        release.set();await task
        assert read.request_card(papers[0])['status']=='ready'
    finally:
        release.set();task.cancel();await asyncio.gather(task,return_exceptions=True)


@pytest.mark.asyncio
async def test_broken_stream_keeps_old_card_and_partial_content_and_requires_explicit_retry(client,papers,monkeypatch):
    use_cloud()
    old=card_value();old['reading_level']='L2'
    execute("INSERT INTO reading_cards(paper_id,status,card_json,created_at) VALUES(?,'ready',?,?)",(papers[0],dumps(old),now()))
    async def parse(p):return {'text':'fulltext','pages':[{'section':'page 1','text':'fulltext'}]}
    async def stream(*args,**kwargs):
        yield SimpleNamespace(content='{"answers":{"problem":"已经开始生成',tool_calls=None)
        raise TimeoutError()
    monkeypatch.setattr(read,'ensure_fulltext',parse);monkeypatch.setattr(models,'stream',stream)
    read.request_card(papers[0],regenerate=True);await read._tasks[papers[0]]
    result=read.card_response(read.request_card(papers[0]))
    assert result['status']=='failed' and '超时' in result['error']
    assert result['card']==old and result['progress']['answers']['problem']=='已经开始生成'
    assert papers[0] not in read._tasks


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['forbidden','timeout','network'])
async def test_fulltext_failures_are_distinct_from_model_failures_and_logged(client,papers,monkeypatch,failure):
    use_cloud()
    async def parse(p):
        request=httpx.Request('GET','https://openreview.net/pdf?id=paper')
        if failure=='timeout':raise httpx.ReadTimeout('timeout',request=request)
        if failure=='network':raise httpx.ConnectError('network',request=request)
        response=httpx.Response(403,request=request);response.raise_for_status()
    async def forbidden(*args,**kwargs):pytest.fail('Fulltext failure must not invoke a model')
    monkeypatch.setattr(read,'ensure_fulltext',parse);monkeypatch.setattr(models,'stream',forbidden)
    read.request_card(papers[0]);await read._tasks[papers[0]]
    result=read.card_response(read.request_card(papers[0]))
    assert result['status']=='failed' and '论文全文' in result['error'] and '尚未调用精读模型' in result['error']
    assert '模型配置' not in result['error'] and 'API' not in result['error']
    if failure=='forbidden':assert 'HTTP 403' in result['error']
    log=one("SELECT detail FROM app_logs WHERE kind='reading' AND level='error'")
    assert json.loads(log['detail'])['stage']=='parsing'


def test_stream_endpoint_requires_login_and_serves_cached_card_without_generation(client,accounts,papers,monkeypatch):
    value=card_value();value['reading_level']='L2'
    execute("INSERT INTO reading_cards(paper_id,status,card_json,created_at) VALUES(?,'ready',?,?)",(papers[0],dumps(value),now()))
    response=client.post(f'/api/papers/{papers[0]}/card/stream',json={})
    assert response.status_code==401
    for account in accounts:
        response=client.post(f'/api/papers/{papers[0]}/card/stream',headers=headers(account),json={})
        assert response.headers['content-type'].startswith('text/event-stream') and 'event: card' in response.text
        assert json.loads(response.text.split('data: ',1)[1])['card']==value
    assert papers[0] not in read._tasks


@pytest.mark.asyncio
async def test_cloud_reading_stream_uses_its_route_and_json_budget_and_keeps_token_usage(client,monkeypatch):
    use_cloud(thinking='off');captured=[];original_client=cloud.client
    def handler(request):
        payload=json.loads(request.content);captured.append(payload)
        chunks=[{'choices':[{'index':0,'delta':{'content':'{"answers":{}'},'finish_reason':None}]},
                {'choices':[{'index':0,'delta':{'content':'}'},'finish_reason':'stop'}]},
                {'choices':[],'usage':{'prompt_tokens':30,'completion_tokens':10,'total_tokens':40}}]
        data=''.join('data: '+json.dumps({'id':'1','object':'chat.completion.chunk','created':1,'model':'test',**chunk})+'\n\n' for chunk in chunks)+'data: [DONE]\n\n'
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=data)
    monkeypatch.setattr(cloud,'client',lambda:AsyncOpenAI(api_key='test',http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),max_retries=0))
    assert ''.join([d.content or '' async for d in models.stream([],[],feature='reading_l2',json_mode=True)])=='{"answers":{}}'
    payload=captured[0]
    assert payload['model']=='deepseek-v4.1-flash' and payload['stream'] and 'tools' not in payload
    assert payload['response_format']=={'type':'json_object'} and payload['max_tokens']==16384
    assert payload['enable_thinking'] is False
    assert one('SELECT input_tokens,output_tokens,feature FROM llm_usage')=={'input_tokens':30,'output_tokens':10,'feature':'reading_l2'}
    with models.bind({**models.selected('reading_l2'),'feature':'reading_l2'}):
        provider_client=original_client()
        assert provider_client.max_retries==0
        await provider_client.close()


@pytest.mark.asyncio
async def test_native_reading_stream_keeps_thinking_hidden_and_uses_json_context_budget(client,monkeypatch):
    config=models.legacy_defaults()
    config['routes']['reading_l2']={'primary':{'connection_id':'local','model':'qwen3:4b','thinking':'on'},'fallback':None}
    execute("INSERT OR REPLACE INTO app_settings VALUES('models',?,?)",(dumps(config),now()))
    requests=[];raw=dumps(card_value())
    def handler(request):
        if request.url.path=='/api/show':return httpx.Response(200,json={'model_info':{}})
        requests.append(json.loads(request.content))
        chunks=[{'message':{'thinking':'hidden local reasoning'},'done':False},
                {'message':{'content':raw[:40]},'done':False},
                {'message':{'content':raw[40:]},'done':True,'done_reason':'stop'}]
        return httpx.Response(200,text='\n'.join(dumps(chunk) for chunk in chunks)+'\n')
    original=httpx.AsyncClient
    monkeypatch.setattr('app.llm.ollama.httpx.AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    schema=read.SixQuestionCard.model_json_schema()
    visible=''.join([delta.content or '' async for delta in models.stream([],[],feature='reading_l2',json_mode=True,schema=schema)])
    assert json.loads(visible)==card_value() and 'hidden local reasoning' not in visible
    assert requests[0]['stream'] and requests[0]['think'] is True and requests[0]['format']==schema
    assert requests[0]['options']['num_ctx']==16384 and requests[0]['options']['num_predict']==16384//3
    assert not rows('SELECT * FROM llm_usage')
