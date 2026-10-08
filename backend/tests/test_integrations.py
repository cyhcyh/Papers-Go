import json
import httpx
import pytest
from openai import AsyncOpenAI
from app.config import settings, now, today
from app.db import one, rows, execute, dumps
from app.llm.provider import cloud
from .conftest import headers


@pytest.mark.parametrize('json_mode,answer,expected', [(True, '{"ok":true}', {'ok': True}), (False, '两句研究概述。方法继续演进。', '两句研究概述。方法继续演进。')])
@pytest.mark.asyncio
async def test_qwen3_non_thinking_output(client, monkeypatch, json_mode, answer, expected):
    import importlib
    module = importlib.import_module('app.llm.ollama')
    settings().ollama_model = 'qwen3:4b'
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload['messages'][0]['content'].endswith('\n/no_think')
        assert payload['think'] is False
        return httpx.Response(200, json={'message': {'content': '<think>Internal reasoning.</think>\n' + answer}})
    async_client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kwargs: async_client(transport=httpx.MockTransport(handler), **kwargs))
    assert await module.ollama.chat('Summarize research.', json_mode=json_mode) == expected
    assert len(requests) == 1


@pytest.mark.parametrize('endpoint',['https://compatible-a.example/v1','https://compatible-b.example/v1'])
@pytest.mark.asyncio
async def test_cloud_protocol_switch_cache_and_usage(client,monkeypatch,endpoint):
    settings().llm_base_url=endpoint
    requests=[]
    def handler(request):
        requests.append(request)
        data=json.loads(request.content)
        assert data['model']==settings().llm_model_precise
        assert data['response_format']=={'type':'json_object'}
        return httpx.Response(200,json={'id':'chat-1','object':'chat.completion','created':1,'model':data['model'],
          'choices':[{'index':0,'message':{'role':'assistant','content':'{"ok":true}'},'finish_reason':'stop'}],
          'usage':{'prompt_tokens':12,'completion_tokens':4,'total_tokens':16}})
    monkeypatch.setattr(cloud,'client',lambda:AsyncOpenAI(base_url=settings().llm_base_url,api_key='test',http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    messages=[{'role':'user','content':'Return JSON'}]
    assert await cloud.complete(messages,json_mode=True,cache_seconds=60)=={'ok':True}
    assert await cloud.complete(messages,json_mode=True,cache_seconds=60)=={'ok':True}
    assert len(requests)==1 and str(requests[0].url)==endpoint+'/chat/completions'
    assert one('SELECT input_tokens,output_tokens FROM llm_usage')=={'input_tokens':12,'output_tokens':4}


@pytest.mark.asyncio
async def test_openai_stream_function_call_fragments(client,monkeypatch):
    chunks=[{'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'id':'call_1','type':'function','function':{'name':'show_profile','arguments':'{'}}]},'finish_reason':None}]},
            {'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'function':{'arguments':'}'}}]},'finish_reason':'tool_calls'}]}]
    def handler(request):
        payload=json.loads(request.content)
        assert payload['stream'] and payload['tools']
        content=''.join('data: '+json.dumps({'id':'1','object':'chat.completion.chunk','created':1,'model':'test',**chunk})+'\n\n' for chunk in chunks)+'data: [DONE]\n\n'
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=content)
    monkeypatch.setattr(cloud,'client',lambda:AsyncOpenAI(api_key='test',http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    from app.agent.tools import TOOLS
    deltas=[delta async for delta in cloud.stream([{'role':'user','content':'profile'}],TOOLS)]
    assert ''.join(d.tool_calls[0].function.arguments for d in deltas)=='{}'


@pytest.mark.asyncio
async def test_reading_task_generation_reuses_single_job(client,papers,monkeypatch):
    from app.pipeline import read
    calls=0
    async def generate(paper_id,level):
        nonlocal calls
        calls+=1
        execute("UPDATE reading_cards SET status='ready',card_json=? WHERE paper_id=?",(dumps({'tldr':'cached'}),paper_id))
    monkeypatch.setattr(read,'generate_card',generate)
    first=read.request_card(papers[0]);second=read.request_card(papers[0])
    assert first['status']=='pending' and second['status']=='pending'
    await read._tasks[papers[0]]
    assert calls==1 and read.request_card(papers[0])['status']=='ready'


@pytest.mark.asyncio
async def test_reading_failure_then_explicit_retry(client,papers,monkeypatch):
    from app.pipeline import read
    attempts=0
    async def generate(paper_id,level):
        nonlocal attempts
        attempts+=1
        raise RuntimeError('provider unavailable')
    monkeypatch.setattr(read,'generate_card',generate)
    read.request_card(papers[0]);await read._tasks[papers[0]]
    assert read.request_card(papers[0])['status']=='failed' and attempts==1
    read.request_card(papers[0],retry=True);await read._tasks[papers[0]]
    assert attempts==2


def test_published_backlog_exclusions_and_metrics_local_date(client,accounts,papers):
    a,b=accounts
    from app.interest.profile import put_profile
    put_profile(a['user']['id'],'## 核心兴趣\n- memory\n## 明确排除\n- Ramsey theory',{},'init')
    result=client.get('/api/feed/today',headers=headers(a)).json()
    assert papers[1] not in [p['id'] for p in result['items']]
    from datetime import date,timedelta
    yesterday=(date.fromisoformat(today())-timedelta(days=1)).isoformat()
    execute('UPDATE papers SET ingested_date=?,published=? WHERE id=?',(yesterday,yesterday,papers[0]))
    assert papers[0] in [p['id'] for p in client.get('/api/feed/backlog',headers=headers(a)).json()['items']]


def test_baseline_benchmark_and_collision_notifications(client,accounts,papers):
    a,b=accounts
    from app.interest.profile import put_profile
    from app.db import pack
    from app.pipeline.alerts import evaluate_alerts
    client.post('/api/interactions',headers=headers(a),json={'paper_id':papers[0],'action':'save'})
    client.post('/api/watches',headers=headers(a),json={'type':'benchmark','value':'LoCoBench'})
    put_profile(a['user']['id'],'## 在研方向\n- [w:0.9] agent evaluation',{},'init',pack([1.,0.,0.,0.]))
    claim={'claim':'LoCoBench 上超越 Graph Memory for Agents','verified':True,'evidence':{'section':'4','quote':'Evidence'}}
    execute("INSERT INTO reading_cards(paper_id,status,card_json,created_at) VALUES(?,'ready',?,?)",(papers[2],dumps({'key_results':[claim]}),now()))
    evaluate_alerts()
    kinds={n['type'] for n in client.get('/api/notifications',headers=headers(a)).json()['items']}
    assert {'baseline_beaten','watch_hit','collision'}<=kinds
    assert client.get('/api/notifications',headers=headers(b)).json()['items']==[]


def test_private_api_routes_require_auth(client):
    paths=['/library','/trends','/notifications','/chat/sessions','/profile','/watches','/me','/admin/users','/admin/topics','/admin/sources','/admin/llm-status','/admin/metrics/daily']
    for path in paths:
        assert client.get('/api'+path).status_code==401,path


@pytest.mark.asyncio
async def test_classification_confidence_and_proposals(client,papers,monkeypatch):
    from app.pipeline.classify import classify_paper
    from app.llm import runtime
    from app.standard_topics import candidates
    paper=one('SELECT * FROM papers WHERE id=?',(papers[0],))
    chosen=candidates(paper)[0]
    confidence=.5
    async def complete(*args,**kwargs):
        return {'standard_key':chosen['key'],'confidence':confidence,'name_zh':chosen['name_zh'],'reason':'研究智能体','evidence':paper['title'],'no_suitable_topic':False}
    monkeypatch.setattr(runtime,'complete',complete)
    await classify_paper(paper)
    assert not rows('SELECT * FROM paper_topics WHERE paper_id=?',(paper['id'],))
    assert one('SELECT classification_state FROM papers WHERE id=?',(paper['id'],))['classification_state']=='awaiting_approval'
    assert one('SELECT * FROM topics WHERE standard_key=?',(chosen['key'],))['status']=='proposed'
    confidence=.9
    await classify_paper(paper)
    assert one('SELECT * FROM topics WHERE standard_key=?',(chosen['key'],))['status']=='proposed'
    assert one('SELECT * FROM topic_pending_papers WHERE paper_id=?',(paper['id'],))
    assert not rows('SELECT * FROM paper_topics WHERE paper_id=?',(paper['id'],))


@pytest.mark.asyncio
async def test_offline_classification_fails_before_relabeling_papers(client,papers,monkeypatch):
    from app.pipeline import classify as module
    from app.llm.ollama import ollama
    async def fail(*args,**kwargs):raise RuntimeError('offline')
    monkeypatch.setattr(module,'check_service',fail)
    with pytest.raises(RuntimeError):await module.classify()
    assert one('SELECT classified FROM papers WHERE id=?',(papers[0],))['classified']==0
    assert not one('SELECT * FROM paper_topics WHERE paper_id=? AND topic_id=16',(papers[0],))
    assert one('SELECT * FROM paper_topics WHERE paper_id=? AND topic_id=3',(papers[0],))


@pytest.mark.parametrize('level',['L2','L3'])
@pytest.mark.asyncio
async def test_full_reading_generation_evidence_and_levels(client,papers,monkeypatch,level):
    from app.pipeline.read import generate_card
    import app.pipeline.read as module
    text='Our proposed method improves accuracy by 11 percentage points.'
    fulltext={'text':text,'sections':[{'section':'4 Experiments','text':text}],
              'pages':[{'section':'page 3','text':text}]}
    async def parse(paper):return fulltext
    monkeypatch.setattr(module,'ensure_fulltext',parse)
    requests=[]
    async def stream(messages,tools,**kwargs):
        requests.append(messages)
        value={'tldr':'方法摘要','method_summary':'方法步骤','key_results':[{'claim':'提升11百分点','evidence':{'section':'4','quote':text}}],
                'limitations':['样本有限'],'read_priority':'worth_reading','paper_kind':'empirical',
                'answers':{'problem':'问题定义','related_work':'相关方法比较','method':'方法步骤','evaluation':'提升11百分点','future':'作者明确提出的方向：未提及。基于论文的进一步建议：检验泛化。','summary':'方法与实验总结'}}
        from types import SimpleNamespace
        yield SimpleNamespace(content=dumps(value),tool_calls=None)
    monkeypatch.setattr(cloud,'stream',stream)
    execute("INSERT INTO reading_cards(paper_id,status,created_at) VALUES(?,'pending',?)",(papers[0],now()))
    card=await generate_card(papers[0],level)
    assert card['key_results'][0]['verified'] and card['reading_level']==level
    assert one('SELECT status FROM reading_cards WHERE paper_id=?',(papers[0],))['status']=='ready'
    assert len(requests)==1


@pytest.mark.asyncio
async def test_bootstrap_resumes_unfinished_existing_papers(client,papers,monkeypatch):
    import app.scheduler as module
    calls=[]
    async def run_job(name):calls.append(name)
    async def status():return {'ready':True}
    monkeypatch.setattr(module,'run_job',run_job)
    monkeypatch.setattr(module.ollama,'status',status)
    await module.bootstrap()
    assert 'fetch_arxiv' not in calls
    assert calls==['classify','build_vectors','assess_quality','tldr_gen','preread','trend_stats','alert_eval','metrics']
