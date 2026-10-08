import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from app.config import now, today
from app.db import connect, execute, one
from app.llm import runtime
from app.llm.provider import cloud, completion_options
from app.pipeline import classify as module


def cloud_config():
    config = runtime.defaults()
    config['connections'][1].update(base_url='https://test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1', api_key='test-key')
    for feature in ('classify', 'reading_l2'):
        config['routes'][feature] = {'primary': {'connection_id':'cloud', 'model':'deepseek-v4.1-flash'}, 'fallback':None}
    return config


@pytest.mark.asyncio
async def test_classification_sdk_disables_thinking_without_changing_reading_thinking(client, monkeypatch):
    requests = []
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={'id':'test', 'object':'chat.completion', 'created':0,
            'model':body['model'], 'choices':[{'index':0,'finish_reason':'stop',
            'message':{'role':'assistant','content':'{"topics":[],"new_topic_hint":null}'}}],
            'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}})
    def test_client():
        binding = runtime.current_binding()
        return AsyncOpenAI(base_url=binding['base_url'], api_key=binding['api_key'],
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(cloud, 'client', test_client)
    with runtime.model_snapshot(cloud_config()):
        await runtime.complete('classify', [{'role':'user','content':'classify'}], json_mode=True)
        await runtime.complete('reading_l2', [{'role':'user','content':'read'}], json_mode=True)
    assert requests[0]['enable_thinking'] is False and requests[0]['max_tokens']==512
    assert requests[0]['response_format']=={'type':'json_object'}
    assert 'enable_thinking' not in requests[1] and requests[1]['max_tokens']==32768


def test_provider_specific_parameters_are_not_sent_to_unknown_hosts():
    binding = {'feature':'classify'}
    assert completion_options(binding,'deepseek-flash','https://api.deepseek.com/v1')['extra_body']=={'thinking':{'type':'disabled'}}
    assert completion_options(binding,'deepseek-v4.1-flash','https://proxy.example/v1')=={'max_tokens':512}
    assert completion_options(binding,'deepseek-r1','https://dashscope.aliyuncs.com/compatible-mode/v1')=={'max_tokens':512}


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,expected', [('cloud',4),('ollama',1)])
async def test_classification_concurrency_limit_and_exactly_once(client, monkeypatch, kind, expected):
    with connect() as db:
        db.executemany('INSERT INTO papers(title,created_at,ingested_date) VALUES(?,?,?)', [('test',now(),today()) for _ in range(12)])
    active, peak, seen = 0, 0, []
    async def ready(): pass
    async def process(paper):
        nonlocal active, peak
        active += 1
        peak = max(peak,active)
        try:
            await asyncio.sleep(.01)
            seen.append(paper['id'])
            execute('UPDATE papers SET classified=1 WHERE id=?', (paper['id'],))
        finally:
            active -= 1
    monkeypatch.setattr(module,'check_service',ready)
    monkeypatch.setattr(module,'classify_paper',process)
    config = cloud_config() if kind=='cloud' else runtime.defaults()
    with runtime.model_snapshot(config):
        assert await module.classify(limit=9)==9
    assert peak==expected and len(seen)==len(set(seen))==9
    assert one('SELECT COUNT(*) n FROM papers WHERE classified=0')['n']==3
    progress = json.loads(one("SELECT progress FROM source_status WHERE name='classify'")['progress'])
    assert progress['completed']==9 and progress['pending']==0
    assert progress['concurrency']==expected and progress['in_flight']==0
    assert progress['current_paper'] is None and progress['estimated_remaining_seconds']==0


@pytest.mark.asyncio
async def test_parallel_stop_cancels_all_requests_and_preserves_completed(client, monkeypatch):
    for _ in range(8):
        execute('INSERT INTO papers(title,created_at,ingested_date) VALUES(?,?,?)', ('test',now(),today()))
    started = asyncio.Event()
    waiting, cancelled = set(), set()
    async def ready(): pass
    async def process(paper):
        if paper['id']==1:
            execute('UPDATE papers SET classified=1 WHERE id=1')
            return
        waiting.add(paper['id'])
        if len(waiting)==4:
            started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.add(paper['id'])
            raise
        execute('UPDATE papers SET classified=1 WHERE id=?', (paper['id'],))
    monkeypatch.setattr(module,'check_service',ready)
    monkeypatch.setattr(module,'classify_paper',process)
    with runtime.model_snapshot(cloud_config()):
        task = asyncio.create_task(module.classify())
        await asyncio.wait_for(started.wait(),1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert waiting==cancelled and len(cancelled)==4
    assert one('SELECT COUNT(*) n FROM papers WHERE classified=1')['n']==1
    progress = json.loads(one("SELECT progress FROM source_status WHERE name='classify'")['progress'])
    assert progress['completed']==1 and progress['pending']==7 and progress['in_flight']==0


@pytest.mark.asyncio
async def test_failed_papers_remain_retryable_with_parallel_workers(client, monkeypatch):
    for _ in range(7):
        execute('INSERT INTO papers(title,created_at,ingested_date) VALUES(?,?,?)', ('test',now(),today()))
    seen = []
    async def ready(): pass
    async def process(paper):
        seen.append(paper['id'])
        await asyncio.sleep(0)
        if paper['id']==3:
            raise ValueError('invalid classification')
        execute('UPDATE papers SET classified=1 WHERE id=?', (paper['id'],))
    monkeypatch.setattr(module,'check_service',ready)
    monkeypatch.setattr(module,'classify_paper',process)
    with runtime.model_snapshot(cloud_config()):
        with pytest.raises(RuntimeError, match='1 篇分类失败'):
            await module.classify()
    assert len(seen)==len(set(seen))==7
    assert one('SELECT classified FROM papers WHERE id=3')['classified']==0
    progress = json.loads(one("SELECT progress FROM source_status WHERE name='classify'")['progress'])
    assert progress['completed']==6 and progress['failed']==1 and progress['pending']==1
