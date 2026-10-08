import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import pytest
from app.config import now,settings
from app.db import execute,one,rows,dumps
from app.llm import runtime as models
from app.pipeline import read,reading_queue
from app.llm.reading_json import ReadingJSONStream
from .conftest import headers


def route(kind='cloud'):
    config=models.defaults()
    for feature in ('reading_l2','reading_l3'):
        config['routes'][feature]={'primary':{'connection_id':'cloud' if kind=='cloud' else 'local','model':'qwen3:4b','thinking':'auto'},'fallback':None}
    execute("INSERT OR REPLACE INTO app_settings VALUES('models',?,?)",(dumps(config),now()))
    return config


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,limit',[('cloud',2),('ollama',1)])
async def test_fifo_and_bounded_concurrency_survive_closed_subscriptions(client,papers,monkeypatch,kind,limit):
    route(kind);settings().pipeline_mode='external'
    calls=[];release=asyncio.Event();active=peak=0
    async def generate(paper_id,level):
        nonlocal active,peak
        active+=1;peak=max(peak,active);calls.append((paper_id,level))
        try:await release.wait()
        finally:active-=1
        execute("UPDATE reading_cards SET status='ready',card_json=? WHERE paper_id=?",(dumps({'reading_level':level}),paper_id))
    monkeypatch.setattr(read,'generate_card',generate)
    for i,paper_id in enumerate(papers):read.request_card(paper_id,level='L3' if i==1 else 'L2')
    assert not read._tasks and len(rows('SELECT * FROM reading_jobs'))==3
    assert read.card_response(read.request_card(papers[-1]))['progress']['queue_ahead']==2
    events=read.card_events(papers[-1]);assert 'queued' in await anext(events);await events.aclose()
    # The consumer runs independently of every HTTP/SSE connection.
    reading_queue.dispatch();await asyncio.sleep(0)
    assert len(calls)==limit and peak==limit
    assert read.card_response(read.request_card(papers[-1]))['progress']['queue_ahead']==2-limit
    read.request_card(papers[-1])
    assert len(rows('SELECT * FROM reading_jobs'))==3
    try:
        release.set();await asyncio.gather(*list(read._tasks.values()))
        reading_queue.dispatch();await asyncio.gather(*list(read._tasks.values()))
        reading_queue.dispatch();await asyncio.gather(*list(read._tasks.values()))
        assert [p for p,_ in calls]==papers and calls[1][1]=='L3' and peak==limit
        assert not read._tasks and not rows('SELECT * FROM reading_jobs')
        assert all(read.request_card(p)['status']=='ready' for p in papers)
    finally:
        release.set()
        for task in list(read._tasks.values()):task.cancel()
        await asyncio.gather(*list(read._tasks.values()),return_exceptions=True)


def test_queue_is_persistent_and_deduplicates_concurrent_users(client,papers,accounts):
    route();settings().pipeline_mode='external'
    with ThreadPoolExecutor(max_workers=4) as pool:
        result=list(pool.map(lambda _:reading_queue.enqueue(papers[0]),range(8)))
    assert all(r['status']=='pending' for r in result)
    job=one('SELECT * FROM reading_jobs')
    execute("UPDATE reading_cards SET created_at='2026-01-01T00:00:00+00:00'")
    reading_queue.recover()
    assert one('SELECT * FROM reading_jobs')==job
    for account in accounts:
        response=client.get(f'/api/papers/{papers[0]}/card',headers=headers(account))
        assert response.status_code==202 and response.json()['progress']['stage']=='queued'
        assert response.json()['progress']['queue_ahead']==0
    assert len(rows('SELECT * FROM reading_jobs'))==1 and not read._tasks
    execute('DELETE FROM reading_cards WHERE paper_id=?',(papers[0],))
    assert not rows('SELECT * FROM reading_jobs')


@pytest.mark.asyncio
async def test_stopping_preread_does_not_stop_a_task_adopted_by_a_user(client,papers,monkeypatch):
    route();release=asyncio.Event();started=asyncio.Event()
    async def generate(paper_id,level):
        started.set();await release.wait()
        execute("UPDATE reading_cards SET status='ready',card_json=? WHERE paper_id=?",(dumps({'reading_level':level}),paper_id))
    monkeypatch.setattr(read,'generate_card',generate)
    read.request_card(papers[0],source='preread');task=read._tasks[papers[0]]
    try:
        await started.wait();read.request_card(papers[0])
        await reading_queue.stop_preread(papers[0])
        assert not task.done() and one('SELECT source FROM reading_jobs')['source']=='user'
        release.set();await task
        assert read.request_card(papers[0])['status']=='ready'
    finally:
        release.set();task.cancel();await asyncio.gather(task,return_exceptions=True)


@pytest.mark.asyncio
async def test_user_adopted_card_is_independent_of_pipeline_stop(client,papers,monkeypatch):
    from app.pipeline_control import cancellation_scope,check_cancelled
    route();release=asyncio.Event();started=asyncio.Event();pipeline_stop=asyncio.Event()
    async def generate(paper_id,level):
        started.set();await release.wait()
        check_cancelled()
        execute("UPDATE reading_cards SET status='ready',card_json=? WHERE paper_id=?",(dumps({'reading_level':level}),paper_id))
    monkeypatch.setattr(read,'generate_card',generate)
    with cancellation_scope(pipeline_stop):
        read.request_card(papers[0],source='preread')
    task=read._tasks[papers[0]]
    try:
        await asyncio.wait_for(started.wait(),1)
        read.request_card(papers[0])
        pipeline_stop.set()
        await reading_queue.stop_preread(papers[0])
        release.set();await asyncio.wait_for(task,1)
        assert one('SELECT status FROM reading_cards WHERE paper_id=?',(papers[0],))['status']=='ready'
        assert not reading_queue._stop_requests
    finally:
        release.set();task.cancel();await asyncio.gather(task,return_exceptions=True)


@pytest.mark.asyncio
async def test_preread_stream_cannot_publish_after_absorbing_cancel(client,papers,monkeypatch):
    from app.llm.provider import cloud
    from types import SimpleNamespace
    route();started=asyncio.Event();delivered=[]
    async def stream(*args,**kwargs):
        started.set()
        try:await asyncio.Event().wait()
        except asyncio.CancelledError:asyncio.current_task().uncancel()
        yield SimpleNamespace(content='late result',tool_calls=None)
    async def generate(paper_id,level):
        async for delta in models.stream([],[],feature='reading_l2'):
            delivered.append(delta.content)
        execute("UPDATE reading_cards SET status='ready' WHERE paper_id=?",(paper_id,))
    monkeypatch.setattr(cloud,'stream',stream)
    monkeypatch.setattr(read,'generate_card',generate)
    read.request_card(papers[0],source='preread');task=read._tasks[papers[0]]
    try:
        await asyncio.wait_for(started.wait(),1)
        await asyncio.wait_for(reading_queue.stop_preread(papers[0]),1)
        assert task.cancelled() and not delivered
        assert one('SELECT status FROM reading_cards WHERE paper_id=?',(papers[0],))['status']=='failed'
        assert not rows('SELECT * FROM reading_jobs') and not reading_queue._stop_requests
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)


def test_question_progress_counts_only_closed_answer_strings():
    parser=ReadingJSONStream()
    parser.feed('{"answers":{"problem":"still arriving')
    assert parser.answers['problem'] and not parser.completed_answers
    assert parser.feed('","related_work":"second')
    assert parser.completed_answers=={'problem'}
    parser.feed(' answer","method":""}}')
    assert parser.completed_answers=={'problem','related_work'}


def test_admin_can_change_shared_reading_limits(client,accounts,monkeypatch):
    from app.api import model_config
    from app.llm.controls import capabilities
    async def discover(binding):return capabilities(binding)
    monkeypatch.setattr(model_config,'discover',discover)
    admin,user=accounts
    config=client.get('/api/admin/models',headers=headers(admin)).json()
    assert (config['reading_cloud_concurrency'],config['reading_local_concurrency'])==(2,1)
    config.update(reading_cloud_concurrency=3,reading_local_concurrency=2)
    assert client.put('/api/admin/models',headers=headers(user),json=config).status_code==403
    result=client.put('/api/admin/models',headers=headers(admin),json=config)
    assert result.status_code==200 and result.json()['reading_cloud_concurrency']==3
    assert models.configuration()['reading_local_concurrency']==2
    config['reading_cloud_concurrency']=0
    assert client.put('/api/admin/models',headers=headers(admin),json=config).status_code==422


async def wait_for(predicate):
    async def wait():
        while not predicate():await asyncio.sleep(.01)
    await asyncio.wait_for(wait(),2)


@pytest.mark.asyncio
async def test_consumer_survives_startup_and_dispatch_errors_then_accepts_later_requests(client,papers,monkeypatch):
    route();settings().pipeline_mode='external'
    recover=reading_queue.recover;dispatch=reading_queue.dispatch
    recovery_calls=dispatch_errors=0;calls=[]
    def recovering():
        nonlocal recovery_calls
        recovery_calls+=1
        if recovery_calls==1:raise sqlite3.OperationalError('database is locked')
        recover()
    def dispatching():
        nonlocal dispatch_errors
        if one("SELECT id FROM reading_jobs WHERE status='queued'") and not dispatch_errors:
            dispatch_errors+=1
            raise sqlite3.OperationalError('database is locked')
        dispatch()
    async def generate(paper_id,level):
        calls.append(paper_id)
        execute("UPDATE reading_cards SET status='ready',card_json=? WHERE paper_id=?",(dumps({'reading_level':level}),paper_id))
    monkeypatch.setattr(reading_queue,'recover',recovering)
    monkeypatch.setattr(reading_queue,'dispatch',dispatching)
    monkeypatch.setattr(read,'generate_card',generate)
    consumer=asyncio.create_task(reading_queue.serve(poll_seconds=.01,retry_seconds=.01))
    try:
        await wait_for(lambda:recovery_calls==2)
        assert not consumer.done()
        read.request_card(papers[0]);read.request_card(papers[0])
        await wait_for(lambda:one('SELECT status FROM reading_cards')['status']=='ready')
        assert calls==[papers[0]] and not rows('SELECT * FROM reading_jobs')
        assert len(rows("SELECT * FROM app_logs WHERE job='reading_queue' AND level='error'"))==2
    finally:
        consumer.cancel();await asyncio.gather(consumer,return_exceptions=True)


@pytest.mark.asyncio
async def test_dispatch_error_does_not_cancel_an_active_card(client,papers,monkeypatch):
    route();settings().pipeline_mode='external'
    dispatch=reading_queue.dispatch;release=asyncio.Event();fault=asyncio.Event();calls=[]
    async def generate(paper_id,level):
        calls.append(paper_id);await release.wait()
        execute("UPDATE reading_cards SET status='ready',card_json=? WHERE paper_id=?",(dumps({'reading_level':level}),paper_id))
    def dispatching():
        if calls and len(rows("SELECT id FROM reading_jobs WHERE status='queued'")) and not fault.is_set():
            fault.set();raise sqlite3.OperationalError('database is locked')
        dispatch()
    monkeypatch.setattr(read,'generate_card',generate)
    monkeypatch.setattr(reading_queue,'dispatch',dispatching)
    read.request_card(papers[0])
    consumer=asyncio.create_task(reading_queue.serve(poll_seconds=.01,retry_seconds=.01))
    try:
        await wait_for(lambda:len(calls)==1)
        task=read._tasks[papers[0]]
        read.request_card(papers[1])
        await asyncio.wait_for(fault.wait(),2)
        assert not task.done() and not task.cancelling()
        await wait_for(lambda:len(calls)==2)
        release.set()
        await wait_for(lambda:not rows('SELECT * FROM reading_jobs'))
        assert all(one('SELECT status FROM reading_cards WHERE paper_id=?',(p,))['status']=='ready' for p in papers[:2])
    finally:
        release.set();consumer.cancel();await asyncio.gather(consumer,return_exceptions=True)


def test_empty_consumer_does_not_require_model_configuration(client,monkeypatch):
    def unavailable():raise ValueError('configuration temporarily unavailable')
    monkeypatch.setattr(models,'configuration',unavailable)
    reading_queue.dispatch()


@pytest.mark.asyncio
async def test_worker_monitors_and_restarts_a_stopped_consumer(client,monkeypatch):
    from app import worker
    release=asyncio.Event();starts=0
    async def consume():
        nonlocal starts
        starts+=1
        if starts==1:raise RuntimeError('unexpected consumer exit')
        await release.wait()
    monkeypatch.setattr(reading_queue,'serve',consume)
    task=worker.ensure_reading_consumer()
    await asyncio.gather(task,return_exceptions=True)
    restarted=worker.ensure_reading_consumer(task)
    try:
        await asyncio.sleep(0)
        assert starts==2 and not restarted.done()
        assert worker.ensure_reading_consumer(restarted) is restarted
        assert one("SELECT detail FROM app_logs WHERE job='reading_queue'")['detail']=='{"error_type": "RuntimeError"}'
    finally:
        release.set();await restarted
