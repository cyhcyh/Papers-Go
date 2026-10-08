import asyncio
import threading

import pytest

import app.scheduler as scheduler
from app.db import execute, one
from .conftest import headers


@pytest.fixture
def task_runtime(client, monkeypatch):
    monkeypatch.setattr(scheduler, '_pipeline_lock', asyncio.Lock())
    for name in ('_manual_tasks', '_pipeline_tasks', '_stopping'):
        monkeypatch.setattr(scheduler, name, set())
    for name in ('_manual_names', '_job_tasks', '_running_jobs', '_stop_requests'):
        monkeypatch.setattr(scheduler, name, {})
    return scheduler


@pytest.mark.asyncio
async def test_restart_does_not_resume_manually_stopped_stages(task_runtime,papers,monkeypatch):
    execute('INSERT INTO source_status(name,error) VALUES(?,?)',('classify',scheduler.STOP_MESSAGE))
    execute('INSERT INTO source_status(name,error) VALUES(?,?)',('build_vectors',scheduler.STOP_MESSAGE))
    called=[]
    async def run_job(name):called.append(name)
    async def status():return {'ready':True}
    monkeypatch.setattr(scheduler,'run_job',run_job)
    monkeypatch.setattr(scheduler.ollama,'status',status)
    await scheduler.bootstrap()
    assert 'classify' not in called and 'build_vectors' not in called
    assert 'tldr_gen' in called


@pytest.mark.asyncio
async def test_stop_pipeline_preserves_completed_and_partial_results(task_runtime, monkeypatch):
    started = asyncio.Event()
    calls = []

    async def first():
        calls.append('first')
        return 7

    async def second():
        calls.append('second')
        execute("INSERT INTO app_migrations(name,applied_at) VALUES('partial-result','test')")
        started.set()
        await asyncio.Event().wait()

    async def third():
        calls.append('third')

    monkeypatch.setattr(scheduler, 'jobs', {'first': first, 'second': second, 'third': third})
    task = scheduler.start_manual('pipeline')
    await asyncio.wait_for(started.wait(), 1)
    stopped = await scheduler.stop_jobs()
    assert stopped['requested'] and not stopped['busy']
    assert task.cancelled() and calls == ['first', 'second']
    assert one("SELECT added,running,error FROM source_status WHERE name='first'") == {'added': 7, 'running': 0, 'error': None}
    assert one("SELECT running,error FROM source_status WHERE name='second'") == {'running': 0, 'error': scheduler.STOP_MESSAGE}
    assert one("SELECT name FROM app_migrations WHERE name='partial-result'")
    assert one("SELECT error FROM source_status WHERE name='third'")['error']==scheduler.STOP_MESSAGE


@pytest.mark.asyncio
async def test_global_stop_cancels_queued_jobs(task_runtime, monkeypatch):
    started = asyncio.Event()
    calls = []

    async def active():
        calls.append('active')
        started.set()
        await asyncio.Event().wait()

    async def queued():
        calls.append('queued')

    monkeypatch.setattr(scheduler, 'jobs', {'active': active, 'queued': queued})
    running = asyncio.create_task(scheduler.run_job('active'))
    await asyncio.wait_for(started.wait(), 1)
    waiting = asyncio.create_task(scheduler.run_job('queued'))
    await asyncio.sleep(0)
    assert scheduler.job_state()['queued'] == ['queued']
    await scheduler.stop_jobs()
    assert running.cancelled() and waiting.cancelled()
    assert calls == ['active'] and not scheduler.job_state()['busy']


@pytest.mark.asyncio
async def test_named_stop_can_restart_task(task_runtime, monkeypatch):
    started = asyncio.Event()
    attempts = 0

    async def classify():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            started.set()
            await asyncio.Event().wait()
        return 3

    monkeypatch.setattr(scheduler, 'jobs', {'classify': classify})
    task = scheduler.start_manual('classify')
    await asyncio.wait_for(started.wait(), 1)
    assert (await scheduler.stop_jobs('classify'))['requested']
    assert task.cancelled()
    assert not (await scheduler.stop_jobs('classify'))['requested']
    await scheduler.start_manual('classify')
    assert one("SELECT added,running,error FROM source_status WHERE name='classify'") == {'added': 3, 'running': 0, 'error': None}


@pytest.mark.asyncio
async def test_classify_stop_does_not_claim_more_papers_when_request_absorbs_cancel(task_runtime,papers,monkeypatch):
    from app.pipeline import classify as classification
    monkeypatch.setitem(scheduler.jobs,'classify',classification.classify)
    monkeypatch.setattr(classification.models,'concurrency',lambda feature:2)
    execute('UPDATE papers SET classified=0')
    started=asyncio.Event();calls=[]
    async def ready():return
    async def request(paper):
        calls.append(paper['id'])
        if len(calls)==2:started.set()
        try:await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Some async transports suppress a cancellation during connection cleanup.
            asyncio.current_task().uncancel()
            return
    monkeypatch.setattr(classification,'check_service',ready)
    monkeypatch.setattr(classification,'ensure_index',ready)
    monkeypatch.setattr(classification,'classify_paper',request)
    task=scheduler.start_manual('classify')
    await asyncio.wait_for(started.wait(),1)
    stopped=await asyncio.wait_for(scheduler.stop_jobs('classify'),1)
    assert task.cancelled() and not stopped['busy'] and not stopped['stopping']
    assert len(calls)==2  # The third paper must remain unclaimed.
    assert not scheduler._stop_requests
    assert one("SELECT running,error FROM source_status WHERE name='classify'")=={'running':0,'error':scheduler.STOP_MESSAGE}


@pytest.mark.asyncio
@pytest.mark.parametrize('feature',['classify','brief','quality','embedding'])
async def test_model_result_is_not_published_after_transport_absorbs_stop(task_runtime,monkeypatch,feature):
    from app.llm import runtime as models
    from app.llm.provider import cloud
    import copy
    config=copy.deepcopy(models.configuration())
    config['routes'][feature]={'primary':{'connection_id':'cloud','model':'test-model','thinking':'off','reasoning_effort':'auto'},'fallback':None}
    started=asyncio.Event();returned=[];calls=[]
    async def request(*args,**kwargs):
        calls.append(1);started.set()
        try:await asyncio.Event().wait()
        except asyncio.CancelledError:
            asyncio.current_task().uncancel()
            return [[1.,0,0,0]] if feature=='embedding' else {'result':'late result'}
    monkeypatch.setattr(cloud,'complete',request)
    monkeypatch.setattr(cloud,'embed',request)
    async def stage():
        with models.model_snapshot(config,replace=True):
            value=await models.embed(['Research']) if feature=='embedding' else await models.complete(feature,[])
            returned.append(value)
    monkeypatch.setitem(scheduler.jobs,'classify',stage)
    task=scheduler.start_manual('classify')
    await asyncio.wait_for(started.wait(),1)
    await asyncio.wait_for(scheduler.stop_jobs('classify'),1)
    assert task.cancelled() and returned==[] and calls==[1]
    assert not scheduler.job_state()['busy']


@pytest.mark.asyncio
@pytest.mark.parametrize('name',['tldr_gen','assess_quality'])
async def test_concurrent_jobs_do_not_claim_more_after_absorbed_stop(task_runtime,papers,monkeypatch,name):
    from app.pipeline import tldr,embed
    module,function,request_name=(tldr,tldr.tldr_gen,'generate_brief') if name=='tldr_gen' else (embed,embed.assess_quality,'score_paper')
    execute('UPDATE papers SET brief_json=NULL,scored=0')
    monkeypatch.setattr(module.models,'concurrency',lambda feature:2)
    started=asyncio.Event();calls=[]
    async def request(paper):
        calls.append(paper['id'])
        if len(calls)==2:started.set()
        try:await asyncio.Event().wait()
        except asyncio.CancelledError:asyncio.current_task().uncancel()
    monkeypatch.setattr(module,request_name,request)
    monkeypatch.setitem(scheduler.jobs,name,function)
    task=scheduler.start_manual(name)
    await asyncio.wait_for(started.wait(),1)
    stopped=await asyncio.wait_for(scheduler.stop_jobs(name),1)
    assert task.cancelled() and len(calls)==2
    assert not stopped['busy'] and not stopped['stopping']
    assert one('SELECT running,error FROM source_status WHERE name=?',(name,))=={'running':0,'error':scheduler.STOP_MESSAGE}


@pytest.mark.asyncio
@pytest.mark.parametrize('name',['classify','tldr_gen','assess_quality','build_vectors'])
async def test_concurrent_redo_keeps_pending_work_after_absorbed_stop(task_runtime,papers,monkeypatch,name):
    from app.pipeline import redo
    from app.db import rows
    options=redo.RedoOptions(components=['embedding']) if name=='build_vectors' else redo.RedoOptions()
    ident=redo.create_run(name,options)
    monkeypatch.setattr(redo.models,'concurrency',lambda feature:2)
    monkeypatch.setattr(redo.models,'embedding_parallelism',lambda **kwargs:2)
    started=asyncio.Event();calls=[]
    async def process(item):
        calls.append(item['paper_id'])
        if len(calls)==2:started.set()
        try:await asyncio.Event().wait()
        except asyncio.CancelledError:asyncio.current_task().uncancel()
    monkeypatch.setattr(redo,'process',process)
    task=scheduler.start_manual(name,redo_id=ident)
    await asyncio.wait_for(started.wait(),1)
    stopped=await asyncio.wait_for(scheduler.stop_jobs(name),1)
    assert task.cancelled() and len(calls)==2 and not stopped['busy']
    assert redo.state(ident)['status']=='stopped'
    unclaimed=next(iter(set(papers)-set(calls)))
    assert one('SELECT status FROM pipeline_redo_items WHERE run_id=? AND paper_id=?',(ident,unclaimed))['status']=='pending'
    assert not rows("SELECT id FROM pipeline_redo_items WHERE run_id=? AND status='running'",(ident,))


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['daily','redo','rebuild'])
async def test_stopped_vector_results_are_not_saved(task_runtime,papers,monkeypatch,mode):
    from app.pipeline import embed,redo
    from app.llm import runtime as models,vector_rebuild
    from app.pipeline_control import cancellation_scope
    from .vector_helpers import embedding,vector_rows
    old=[embedding(p) for p in papers]
    requested=asyncio.Event();calls=[]
    async def vectors(texts):
        calls.extend(texts);requested.set()
        return [[0.,1,0,0] for _ in texts]
    monkeypatch.setattr(models,'embed',vectors)
    if mode=='daily':
        execute('UPDATE papers SET embedding=NULL')
        old=[embedding(p) for p in papers]
        function=embed.build_vectors
    elif mode=='redo':
        ident=redo.create_run('build_vectors',redo.RedoOptions(components=['embedding']))
        async def function():await redo.run(ident,'build_vectors')
    else:
        config=models.configuration();config['routes']['embedding']['primary']['model']='replacement'
        vector_rebuild.set_pending(config)
        function=vector_rebuild.run
    with cancellation_scope(requested):
        with pytest.raises(asyncio.CancelledError):await function()
    assert calls and [embedding(p) for p in papers]==old
    if mode=='daily':assert not one('SELECT id FROM papers WHERE embedding IS NOT NULL')
    if mode=='redo':assert redo.state(ident)['status']=='stopped' and redo.state(ident)['completed']==0
    if mode=='rebuild':
        assert vector_rebuild.state()['status']=='stopped'
        assert not vector_rows('SELECT * FROM embedding_rebuild_papers',staged=True)
        assert models.configuration()['routes']['embedding']['primary']['model']!='replacement'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['daily','redo','rebuild'])
async def test_stopped_interest_vector_results_are_not_saved(task_runtime,accounts,papers,monkeypatch,mode):
    from app.pipeline import embed,redo
    from app.llm import runtime as models,vector_rebuild
    from app.interest import profile as profiles
    from app.pipeline_control import cancellation_scope
    from app.db import pack
    from .vector_helpers import vector_rows
    profile=profiles.put_profile(accounts[0]['user']['id'],'## 核心兴趣\n- [w:1] 图论',{},'manual')
    requested=asyncio.Event()
    async def vector(content):
        requested.set();return pack([0.,1,0,0])
    monkeypatch.setattr(profiles,'profile_embedding',vector)
    if mode=='daily':function=embed.build_vectors
    elif mode=='redo':
        ident=redo.create_run('build_vectors',redo.RedoOptions(components=['profile_embedding']))
        async def function():await redo.run(ident,'build_vectors')
    else:
        config=models.configuration();config['routes']['embedding']['primary']['model']='replacement'
        vector_rebuild.set_pending(config)
        async def vectors(texts):return [[0.,1,0,0] for _ in texts]
        monkeypatch.setattr(models,'embed',vectors)
        function=vector_rebuild.run
    with cancellation_scope(requested):
        with pytest.raises(asyncio.CancelledError):await function()
    assert one('SELECT embedding,embedding_parts FROM interest_profile WHERE id=?',(profile['id'],))=={'embedding':None,'embedding_parts':None}
    if mode=='redo':assert redo.state(ident)['status']=='stopped' and redo.state(ident)['completed']==0
    if mode=='rebuild':
        assert vector_rebuild.state()['status']=='stopped'
        assert not vector_rows('SELECT * FROM embedding_rebuild_profiles',staged=True)


@pytest.mark.asyncio
async def test_stop_before_manual_task_starts(task_runtime, monkeypatch):
    calls = []

    async def stage():
        calls.append('stage')

    monkeypatch.setattr(scheduler, 'jobs', {'classify': stage})
    task = scheduler.start_manual('classify')
    assert scheduler.job_state()['busy']
    await scheduler.stop_jobs()
    assert task.cancelled() and not calls
    assert not scheduler.job_state()['busy']


@pytest.mark.asyncio
async def test_bootstrap_waiting_for_models_is_stoppable(task_runtime, papers, monkeypatch):
    started = asyncio.Event()
    calls = []

    async def status():
        started.set()
        return {'ready': False}

    async def stage(name):
        calls.append(name)

    monkeypatch.setattr(scheduler.ollama, 'status', status)
    monkeypatch.setattr(scheduler, 'run_job', stage)
    task = asyncio.create_task(scheduler.bootstrap())
    await asyncio.wait_for(started.wait(), 1)
    assert scheduler.job_state()['pipeline']
    await scheduler.stop_jobs()
    assert task.cancelled() and not calls
    assert not scheduler.job_state()['busy']


@pytest.mark.asyncio
async def test_sync_transaction_finishes_before_stop_unlocks(task_runtime, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def transaction():
        started.set()
        assert release.wait(2)
        execute("INSERT INTO app_migrations(name,applied_at) VALUES('sync-result','test')")
        return 1

    monkeypatch.setattr(scheduler, 'jobs', {'metrics': transaction})
    task = scheduler.start_manual('metrics')
    assert await asyncio.to_thread(started.wait, 1)
    stop = asyncio.create_task(scheduler.stop_jobs('metrics'))
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert scheduler.job_state()['stopping']
        assert scheduler.job_state()['stopping_names']==['metrics']
        assert not scheduler.job_state()['stopping_pipeline']
        assert scheduler._pipeline_lock.locked()
        assert one("SELECT running FROM source_status WHERE name='metrics'")['running'] == 1
        # Repeating the stop request must not interrupt the transaction a second time.
        repeated = asyncio.create_task(scheduler.stop_jobs('metrics'))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(stop, repeated)
        assert task.cancelled() and not scheduler._pipeline_lock.locked()
        assert one("SELECT name FROM app_migrations WHERE name='sync-result'")
        assert one("SELECT running,error FROM source_status WHERE name='metrics'") == {'running': 0, 'error': scheduler.STOP_MESSAGE}
    finally:
        release.set()
        await asyncio.gather(task, stop, return_exceptions=True)


def test_stop_api_auth_state_and_restart_guard(task_runtime, client, accounts, monkeypatch):
    admin, regular = accounts
    for method, path in [('get', '/api/admin/jobs'), ('post', '/api/admin/jobs/pipeline/stop')]:
        assert getattr(client, method)(path).status_code == 401
        assert getattr(client, method)(path, headers=headers(regular)).status_code == 403
    assert client.post('/api/admin/jobs/unknown/stop', headers=headers(admin)).status_code == 404
    empty = client.post('/api/admin/jobs/pipeline/stop', headers=headers(admin)).json()
    assert empty['requested'] is False and empty['busy'] is False
    started = threading.Event()

    async def stage():
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setitem(scheduler.jobs, 'classify', stage)
    assert client.post('/api/admin/jobs/classify', headers=headers(admin)).status_code == 200
    assert started.wait(1)
    assert client.get('/api/admin/jobs', headers=headers(admin)).json()['active'] == ['classify']
    assert client.post('/api/admin/jobs/pipeline', headers=headers(admin)).status_code == 409
    stopped = client.post('/api/admin/jobs/classify/stop', headers=headers(admin)).json()
    assert stopped['requested'] and not stopped['busy']
    source = next(s for s in client.get('/api/admin/sources', headers=headers(admin)).json() if s['name'] == 'classify')
    assert source['stopped'] and not source['running'] and not source['queued']


@pytest.mark.asyncio
async def test_stop_running_and_queued_stages_keeps_pipeline_going(task_runtime, monkeypatch):
    started=asyncio.Event()
    following=asyncio.Event()
    finish=asyncio.Event()
    calls=[]
    async def first():
        calls.append('first')
        started.set()
        await asyncio.Event().wait()
    async def omitted():
        calls.append('omitted')
    async def third():
        calls.append('third')
        following.set()
        await finish.wait()
        return 2
    monkeypatch.setattr(scheduler,'jobs',{'first':first,'omitted':omitted,'third':third})
    owner=scheduler.start_manual('pipeline')
    await asyncio.wait_for(started.wait(),1)
    assert scheduler.job_state()['queued']==['omitted','third']
    await scheduler.stop_jobs('omitted')
    assert not owner.done() and one("SELECT error FROM source_status WHERE name='omitted'")['error']==scheduler.STOP_MESSAGE
    await scheduler.stop_jobs('first')
    await asyncio.wait_for(following.wait(),1)
    assert not owner.cancelled() and calls==['first','third']
    finish.set()
    await owner
    assert one("SELECT added,error FROM source_status WHERE name='third'")=={'added':2,'error':None}


@pytest.mark.asyncio
async def test_global_stop_pipeline_waits_for_sync_stage_transaction(task_runtime, monkeypatch):
    started=threading.Event()
    release=threading.Event()
    calls=[]
    def transaction():
        started.set()
        assert release.wait(2)
        execute("INSERT INTO app_migrations(name,applied_at) VALUES('pipeline-sync','test')")
    async def queued():
        calls.append('queued')
    monkeypatch.setattr(scheduler,'jobs',{'metrics':transaction,'next':queued})
    owner=scheduler.start_manual('pipeline')
    assert await asyncio.to_thread(started.wait,1)
    stop=asyncio.create_task(scheduler.stop_jobs())
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert scheduler._pipeline_lock.locked() and not owner.done()
        release.set()
        await stop
        assert owner.cancelled() and not calls
        assert one("SELECT name FROM app_migrations WHERE name='pipeline-sync'")
    finally:
        release.set()
        await asyncio.gather(owner,stop,return_exceptions=True)


@pytest.mark.asyncio
async def test_stopping_preread_keeps_partial_cards_and_does_not_auto_retry(task_runtime, papers, monkeypatch):
    from app.pipeline import read
    started=asyncio.Event()
    async def generate(paper_id,level):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(read,'generate_card',generate)
    owner=scheduler.start_manual('preread')
    await asyncio.wait_for(started.wait(),1)
    paper_id=next(iter(read._tasks))
    await scheduler.stop_jobs('preread')
    assert owner.cancelled() and not read._tasks
    cached=read.request_card(paper_id)
    assert cached['status']=='failed' and not read._tasks
