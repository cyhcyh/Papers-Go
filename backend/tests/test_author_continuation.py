import asyncio
import json
from datetime import datetime, timezone

import pytest

from app import scheduler
from app.db import connect, execute, one, dumps
from app.pipeline import author_runs
from app.pipeline_control import public_state, publish_state, set_job_enabled
from app.config import settings
from .conftest import headers


@pytest.fixture
def runtime(client, monkeypatch):
    monkeypatch.setattr(scheduler, '_pipeline_lock', asyncio.Lock())
    for name in ('_manual_tasks', '_pipeline_tasks', '_stopping', '_restarting'):
        monkeypatch.setattr(scheduler, name, set())
    for name in ('_manual_names', '_job_tasks', '_running_jobs', '_stop_requests'):
        monkeypatch.setattr(scheduler, name, {})
    monkeypatch.setattr(author_runs, 'YIELD_SECONDS', 0)


def batch(processed, pending):
    execute("UPDATE source_status SET progress=? WHERE name='author_impact'",
            (dumps({'processed':processed, 'matched':processed,
                    'authors_updated':processed, 'pending':pending}),))


def due():
    with connect() as db:
        value=author_runs.read(db)
        value['next_run']='2000-01-01T00:00:00+00:00'
        author_runs.write(db,value)


@pytest.mark.asyncio
async def test_batches_yield_to_other_tasks_and_complete_cumulatively(runtime, monkeypatch):
    calls=[];started=asyncio.Event();release=asyncio.Event()
    async def author():
        calls.append('author');batch(1,1 if calls.count('author')==1 else 0);return 1
    async def other():
        calls.append('other');started.set();await release.wait();return 7
    monkeypatch.setattr(scheduler,'jobs',{'author_impact':author,'metrics':other})
    await scheduler.run_job('author_impact')
    assert author_runs.read()['phase']=='waiting'
    assert not one("SELECT last_success FROM source_status WHERE name='author_impact'")['last_success']
    other_task=scheduler.start_manual('metrics')
    await started.wait()
    assert await scheduler.continue_author_if_idle() is None
    release.set();await other_task
    continuation=await scheduler.continue_author_if_idle()
    await continuation
    assert calls==['author','other','author']
    value=author_runs.read()
    assert value['phase']=='complete' and value['processed']==2 and value['pending']==0
    assert one("SELECT last_success FROM source_status WHERE name='author_impact'")['last_success']


@pytest.mark.asyncio
async def test_pending_worker_commands_take_priority_and_waiting_does_not_block_start(runtime, monkeypatch):
    async def author():batch(1,1);return 1
    monkeypatch.setattr(scheduler,'jobs',{'author_impact':author})
    await scheduler.run_job('author_impact')
    settings().pipeline_mode='external'
    publish_state({'busy':False,'active':[],'queued':[],'pipeline':False,'stopping':False,'pid':123})
    assert not public_state()['busy']
    from app.pipeline_control import queue_command
    queue_command('start','metrics')
    assert await scheduler.continue_author_if_idle() is None
    assert author_runs.read()['phase']=='waiting'


@pytest.mark.asyncio
async def test_one_delayed_retry_then_wait_for_next_scheduled_run(runtime, monkeypatch):
    calls=0
    async def author():
        nonlocal calls
        calls+=1
        batch(2 if calls==1 else 0 if calls==2 else 3,3 if calls<3 else 0)
        if calls<3:raise RuntimeError('temporary upstream failure')
        return 3
    monkeypatch.setattr(scheduler,'jobs',{'author_impact':author})
    await scheduler.run_job('author_impact')
    first=author_runs.read()
    seconds=(datetime.fromisoformat(first['next_run'])-datetime.now(timezone.utc)).total_seconds()
    assert first['phase']=='retry' and 590<seconds<=600
    assert first['processed']==2
    assert await scheduler.continue_author_if_idle() is None
    due();await (await scheduler.continue_author_if_idle())
    assert author_runs.read()['phase']=='failed'
    assert author_runs.read()['processed']==2
    assert await scheduler.continue_author_if_idle() is None
    await scheduler.run_scheduled('author_impact')
    value=author_runs.read()
    assert calls==3 and value['phase']=='complete' and value['processed']==3
    assert value['generation']==first['generation']+1


@pytest.mark.asyncio
async def test_successful_retry_resets_failure_count_for_later_batches(runtime, monkeypatch):
    calls=0
    async def author():
        nonlocal calls
        calls+=1;batch(0 if calls in (1,3) else 1,2)
        if calls in (1,3):raise RuntimeError('temporary')
        return 1
    monkeypatch.setattr(scheduler,'jobs',{'author_impact':author})
    await scheduler.run_job('author_impact')
    due();await (await scheduler.continue_author_if_idle())
    assert author_runs.read()['phase']=='waiting' and not author_runs.read()['retried']
    await (await scheduler.continue_author_if_idle())
    assert author_runs.read()['phase']=='retry' and author_runs.read()['processed']==1


@pytest.mark.asyncio
@pytest.mark.parametrize('name',['author_impact','pipeline'])
async def test_stopping_waiting_continuation_cancels_it(runtime, monkeypatch, name):
    async def author():batch(1,1);return 1
    monkeypatch.setattr(scheduler,'jobs',{'author_impact':author})
    await scheduler.run_job('author_impact')
    result=await scheduler.stop_jobs(name)
    assert result['requested'] and author_runs.read()['phase']=='stopped'
    assert await scheduler.continue_author_if_idle() is None
    author_runs.recover()
    assert author_runs.read()['phase']=='stopped'


@pytest.mark.asyncio
async def test_stop_api_accepts_waiting_run_when_worker_is_idle(runtime, accounts, client, monkeypatch):
    async def author():batch(1,1);return 1
    monkeypatch.setattr(scheduler,'jobs',{'author_impact':author})
    await scheduler.run_job('author_impact')
    response=client.post('/api/admin/jobs/author_impact/stop',headers=headers(accounts[0]))
    assert response.status_code==200 and response.json()['requested']
    assert author_runs.read()['phase']=='stopped'


@pytest.mark.asyncio
async def test_stop_running_keeps_saved_results_and_no_continuation(runtime, monkeypatch):
    started=asyncio.Event()
    async def author():
        batch(2,3)
        execute("INSERT INTO app_migrations VALUES('saved-author-result','test')")
        started.set();await asyncio.Event().wait()
    monkeypatch.setattr(scheduler,'jobs',{'author_impact':author})
    task=scheduler.start_manual('author_impact');await started.wait()
    await scheduler.stop_jobs('author_impact')
    assert task.cancelled() and author_runs.read()['phase']=='stopped'
    assert author_runs.read()['processed']==2
    assert one("SELECT name FROM app_migrations WHERE name='saved-author-result'")
    assert await scheduler.continue_author_if_idle() is None


@pytest.mark.asyncio
async def test_disabling_running_batch_prevents_late_completion_from_resuming(runtime, monkeypatch):
    started=asyncio.Event();release=asyncio.Event()
    async def author():batch(1,1);started.set();await release.wait();return 1
    monkeypatch.setattr(scheduler,'jobs',{'author_impact':author})
    task=scheduler.start_manual('author_impact');await started.wait()
    set_job_enabled('author_impact',False)
    release.set();await task
    assert author_runs.read()['phase']=='disabled'
    set_job_enabled('author_impact',True)
    assert await scheduler.continue_author_if_idle() is None


@pytest.mark.asyncio
async def test_disable_cancels_delayed_retry(runtime, monkeypatch):
    async def author():batch(0,1);raise RuntimeError('temporary')
    monkeypatch.setattr(scheduler,'jobs',{'author_impact':author})
    await scheduler.run_job('author_impact')
    set_job_enabled('author_impact',False)
    assert author_runs.read()['phase']=='disabled'
    assert await scheduler.continue_author_if_idle() is None


@pytest.mark.asyncio
async def test_graceful_worker_restart_preserves_partial_batch(runtime, monkeypatch):
    started=asyncio.Event()
    async def author():batch(3,4);started.set();await asyncio.Event().wait()
    monkeypatch.setattr(scheduler,'jobs',{'author_impact':author})
    task=scheduler.start_manual('author_impact');await started.wait()
    await scheduler.stop_jobs('pipeline',preserve_author=True)
    assert task.cancelled() and author_runs.read()['phase']=='waiting'
    assert author_runs.read()['processed']==3
    author_runs.recover()
    assert author_runs.read()['processed']==3


def test_crash_recovery_keeps_retry_budget_and_never_double_counts(runtime):
    generation=author_runs.begin()
    execute("INSERT INTO source_status(name,progress) VALUES('author_impact',?)",(dumps({'processed':2,'pending':3}),))
    with connect() as db:
        value=author_runs.read(db);value['retried']=True;author_runs.write(db,value)
    author_runs.recover();author_runs.recover()
    value=author_runs.read()
    assert value['phase']=='waiting' and value['processed']==2 and value['retried']
    assert value['generation']==generation


def test_cancelled_generation_cannot_restore_continuation(runtime):
    token=author_runs.begin()
    author_runs.cancel()
    author_runs.finish(token,error='late failure')
    assert author_runs.read()['phase']=='stopped' and not author_runs.read()['next_run']


@pytest.mark.asyncio
async def test_admin_counts_running_batch_without_writing_or_double_counting(runtime, accounts, client, monkeypatch):
    started=asyncio.Event();release=asyncio.Event()
    async def author():batch(2,3);started.set();await release.wait();return 2
    monkeypatch.setattr(scheduler,'jobs',{'author_impact':author})
    task=scheduler.start_manual('author_impact');await started.wait()
    try:
        before=author_runs.read()
        for _ in range(2):
            item=next(s for s in client.get('/api/admin/sources',headers=headers(accounts[0])).json() if s['name']=='author_impact')
            assert item['continuation']['processed']==2 and item['continuation']['pending']==3
        assert author_runs.read()==before
    finally:
        release.set();await task
    assert author_runs.read()['processed']==2
