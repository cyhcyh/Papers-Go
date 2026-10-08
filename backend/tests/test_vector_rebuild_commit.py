import asyncio
import json
import threading
from contextvars import Context
from datetime import datetime,timedelta,timezone

import pytest

from app.config import settings
from app.db import connect,execute,one,rows,pack
from app.llm import runtime,vector_rebuild
from app.llm.ollama import ollama
from app.pipeline_control import publish_state,publish_heartbeat,public_state
from .vector_helpers import vector_rows,embedding


def replacement(monkeypatch):
    config=runtime.configuration()
    config['embedding_dim']=8
    config['routes']['embedding']['primary']['model']='replacement'
    vector_rebuild.set_pending(config)
    async def embed(texts):return [[0.,1,0,0,0,0,0,0] for _ in texts]
    monkeypatch.setattr(ollama,'embed',embed)
    monkeypatch.setattr(vector_rebuild,'INDEX_BATCH_SIZE',1)


@pytest.mark.asyncio
async def test_index_batches_leave_live_space_available_and_report_real_phase(client,papers,monkeypatch):
    replacement(monkeypatch)
    original=vector_rebuild._index_batch
    batches=[]
    def batch(*args):
        result=original(*args)
        if result[-1]:
            assert Context().run(runtime.configuration)['embedding_dim']==4
            assert len(vector_rows())==3
            assert len(embedding(papers[0]))==16
            status=vector_rebuild.state()
            assert status['status']=='indexing' and status['index_completed']==result[1]
            batches.append(result[1])
        return result
    monkeypatch.setattr(vector_rebuild,'_index_batch',batch)
    await vector_rebuild.run()
    assert batches==[1,2,3]
    assert vector_rebuild.state() is None and runtime.configuration()['embedding_dim']==8
    assert vector_rows('SELECT paper_id FROM papers_vec WHERE embedding MATCH ? AND k=1',(pack([0.,1,0,0,0,0,0,0]),))
    progress=json.loads(one("SELECT progress FROM source_status WHERE name='build_vectors'")['progress'])
    assert progress['phase']=='complete'


@pytest.mark.asyncio
async def test_index_stop_keeps_generated_vectors_and_resume_does_not_call_model(client,papers,monkeypatch):
    replacement(monkeypatch)
    original=vector_rebuild._index_batch
    entered,release=threading.Event(),threading.Event()
    def batch(*args):
        result=original(*args)
        entered.set();release.wait(5)
        return result
    monkeypatch.setattr(vector_rebuild,'_index_batch',batch)
    task=asyncio.create_task(vector_rebuild.run())
    try:
        assert await asyncio.to_thread(entered.wait,5)
        task.cancel();release.set()
        with pytest.raises(asyncio.CancelledError):await task
    finally:release.set()
    assert vector_rebuild.state()['status']=='stopped'
    assert len(vector_rows('SELECT * FROM embedding_rebuild_papers',staged=True))==3
    assert runtime.configuration()['embedding_dim']==4 and len(vector_rows())==3
    monkeypatch.setattr(vector_rebuild,'_index_batch',original)
    async def forbidden(texts):raise AssertionError('Stored vectors must be reused')
    monkeypatch.setattr(ollama,'embed',forbidden)
    await vector_rebuild.run()
    assert vector_rebuild.pending() is None and runtime.configuration()['embedding_dim']==8


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_model_vectors_and_virtual_table_rename(client,papers,monkeypatch):
    replacement(monkeypatch)
    def fail(*args):raise ValueError('commit merge failed')
    monkeypatch.setattr(vector_rebuild,'with_embedding',fail)
    with pytest.raises(ValueError,match='commit merge failed'):await vector_rebuild.run()
    assert vector_rebuild.state()['status']=='failed'
    assert runtime.configuration()['embedding_dim']==4
    assert all(len(embedding(pid))==16 for pid in papers)
    assert vector_rows('SELECT paper_id FROM papers_vec WHERE embedding MATCH ? AND k=1',(pack([1.,0,0,0]),))
    assert len(vector_rows('SELECT * FROM embedding_rebuild_papers',staged=True))==3


@pytest.mark.asyncio
async def test_final_commit_does_not_block_event_loop_or_worker_heartbeat(client,papers,monkeypatch):
    from app import worker,scheduler
    replacement(monkeypatch)
    settings().pipeline_mode='external'
    publish_state({'pid':123,'busy':False,'active':[],'queued':[],'pipeline':False,'stopping':False})
    stale=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
    execute("UPDATE app_settings SET updated_at=? WHERE name='pipeline_worker'",(stale,))
    original=vector_rebuild.with_embedding
    entered,release=threading.Event(),threading.Event()
    def merge(*args):
        entered.set();release.wait(5)
        return original(*args)
    monkeypatch.setattr(vector_rebuild,'with_embedding',merge)
    monkeypatch.setattr(scheduler,'job_state',lambda:{'busy':True,'active':['build_vectors'],'queued':[],'pipeline':False,'stopping':False})
    heartbeat=asyncio.create_task(worker.heartbeat_loop())
    task=asyncio.create_task(vector_rebuild.run())
    try:
        assert await asyncio.to_thread(entered.wait,5)
        await asyncio.sleep(1.1)
        state=public_state()
        assert state['worker_available'] and state['busy'] and state['active']==['build_vectors']
        assert vector_rebuild.state()['status']=='applying'
        assert Context().run(runtime.configuration)['embedding_dim']==4
        assert not task.done()
        release.set();await task
    finally:
        release.set()
        heartbeat.cancel();await asyncio.gather(heartbeat,return_exceptions=True)
        if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
    assert vector_rebuild.state() is None


def test_stale_or_stopped_file_heartbeat_does_not_claim_ready(client):
    settings().pipeline_mode='external'
    publish_state({'pid':123,'busy':False,'active':[],'queued':[],'pipeline':False,'stopping':False})
    publish_heartbeat({'pid':None})
    assert not public_state()['worker_available']
    path=settings().data_dir/'.pipeline-heartbeat.json'
    stamp=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
    path.write_text(json.dumps({'value':{'pid':123},'updated_at':stamp}),encoding='utf-8')
    execute("UPDATE app_settings SET updated_at=? WHERE name='pipeline_worker'",(stamp,))
    assert not public_state()['worker_available']
