import asyncio
import json

import pytest
from fastapi import HTTPException
from app.config import now, settings
from app.db import connect, execute, one, rows, init_db
from app.llm import runtime as models
from app.pipeline import paper_retries as retries, tldr, embed, redo, read, reading_queue
from app.pipeline_control import cancellation_scope, publish_state
from .conftest import headers


@pytest.fixture
def configured(client,monkeypatch):
    monkeypatch.setattr(models,'configuration_issue',lambda *a,**k:None)


def fail(task, paper_id, count=3):
    for _ in range(count):retries.failed(task,[paper_id])


@pytest.mark.asyncio
@pytest.mark.parametrize('task,generate', [('tldr_gen',tldr.generate_brief),('assess_quality',embed.score_paper)])
async def test_three_round_cap_survives_restart_and_preserves_old_results(configured,papers,monkeypatch,task,generate):
    paper=one('SELECT * FROM papers WHERE id=?',(papers[0],))
    before=one('SELECT brief_json,tldr,skeleton,scored,quality_score,embedding FROM papers WHERE id=?',(papers[0],))
    calls=[]
    async def invalid(*a,**k):
        calls.append(1)
        raise ValueError('invalid output')
    monkeypatch.setattr(models,'complete',invalid)
    for _ in range(3):
        with pytest.raises(ValueError):await generate(paper)
    init_db(recover=False)
    with pytest.raises(retries.PausedError):await generate(paper)
    assert len(calls)==3
    assert retries.snapshot()[task]=={'failed':1,'paused':1,'limit':3}
    assert one('SELECT brief_json,tldr,skeleton,scored,quality_score,embedding FROM papers WHERE id=?',(papers[0],))==before


@pytest.mark.asyncio
async def test_inner_brief_repair_counts_as_one_round_and_success_clears_only_its_function(configured,papers,monkeypatch):
    paper=one('SELECT * FROM papers WHERE id=?',(papers[0],))
    fail('classify',papers[0])
    async def invalid(feature,messages,**kwargs):
        raw={'title_zh':'译名','problem':'具体研究问题','contribution_result':'长'*1201}
        try:kwargs['validate'](raw)
        except tldr.BriefLengthError as error:
            assert kwargs['repair_validation'](raw,error)
            return kwargs['validate'](raw)
    monkeypatch.setattr(models,'complete',invalid)
    with pytest.raises(tldr.BriefLengthError):await tldr.generate_brief(paper)
    assert one("SELECT failures FROM paper_task_failures WHERE task='tldr_gen'")['failures']==1
    async def valid(feature,messages,**kwargs):
        return kwargs['validate']({'title_zh':'译名','problem':'具体研究问题','contribution_result':'得到更紧的上界'})
    monkeypatch.setattr(models,'complete',valid)
    await tldr.generate_brief(paper)
    assert set(retries.snapshot())=={'classify'}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['cancel','absorbed_cancel','missing'])
async def test_stops_and_missing_setup_do_not_count(client,papers,monkeypatch,mode):
    monkeypatch.setattr(models,'configuration_issue',lambda *a,**k:'尚未选择模型' if mode=='missing' else None)
    requested=asyncio.Event()
    async def invalid(*a,**k):
        if mode=='cancel':raise asyncio.CancelledError
        if mode=='absorbed_cancel':requested.set()
        raise ValueError('not ready')
    monkeypatch.setattr(models,'complete',invalid)
    with cancellation_scope(requested),pytest.raises(ValueError if mode=='missing' else asyncio.CancelledError):
        await tldr.generate_brief(one('SELECT * FROM papers WHERE id=?',(papers[0],)))
    assert retries.snapshot()=={}


def test_input_updates_reset_only_relevant_counts_and_deletion_cascades(configured,papers):
    for task in retries.FEATURES:fail(task,papers[0])
    execute('UPDATE papers SET quality_score=30 WHERE id=?',(papers[0],))
    assert len(retries.snapshot())==5
    execute("UPDATE papers SET pdf_url='https://example.test/revised.pdf' WHERE id=?",(papers[0],))
    assert 'preread' not in retries.snapshot()
    assert 'tldr_gen' in retries.snapshot()
    execute("UPDATE papers SET abstract=abstract||' Revised abstract.' WHERE id=?",(papers[0],))
    assert retries.snapshot()=={}
    from app.pipeline.expire import purge_batch
    fail('tldr_gen',papers[0])
    execute("UPDATE papers SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",(papers[0],))
    assert purge_batch(now())==1
    assert retries.snapshot()=={}


@pytest.mark.asyncio
async def test_daily_briefs_skip_paused_but_process_other_papers(configured,papers,monkeypatch):
    fail('tldr_gen',papers[0]);calls=[]
    async def valid(feature,messages,**kwargs):
        calls.append(messages[-1]['content'])
        return kwargs['validate']({'title_zh':'译名','problem':'具体研究问题','contribution_result':'得到新的边界'})
    monkeypatch.setattr(models,'complete',valid)
    assert await tldr.tldr_gen()==2
    assert len(calls)==2 and all('Graph Memory for Agents' not in text for text in calls)
    assert one('SELECT brief_json FROM papers WHERE id=?',(papers[0],))['brief_json'] is None


@pytest.mark.asyncio
async def test_embedding_batch_cap_is_per_paper_and_success_clears(configured,papers,monkeypatch):
    execute('UPDATE papers SET embedding=NULL')
    fail('build_vectors',papers[0]);calls=[]
    async def invalid(texts):calls.append(texts);raise ValueError('bad vector')
    monkeypatch.setattr(models,'embed',invalid)
    for _ in range(3):
        with pytest.raises(RuntimeError):await embed.build_vectors()
    assert len(calls)==3 and all(len(c)==2 for c in calls)
    assert retries.snapshot()['build_vectors']['paused']==3
    assert await embed.build_vectors()==0
    with connect() as db:ident=retries.create_retry_run(db,'build_vectors')
    async def valid(texts):return [[1.,0.,0.,0.] for _ in texts]
    monkeypatch.setattr(models,'embed',valid)
    assert await redo.run(ident,'build_vectors')==3
    assert not retries.snapshot()


@pytest.mark.asyncio
async def test_model_migration_pauses_failed_papers_without_switching_live_vectors(client,configured,accounts,papers,monkeypatch):
    from app.llm import vector_rebuild
    config=models.configuration();config['embedding_dim']=8
    config['routes']['embedding']['primary']['model']='replacement'
    vector_rebuild.set_pending(config)
    calls=[]
    async def invalid(texts):calls.append(texts);raise ValueError('bad vector')
    monkeypatch.setattr(models,'embed',invalid)
    for _ in range(3):
        with pytest.raises(ValueError):await vector_rebuild.run()
    with pytest.raises(retries.PausedError):await vector_rebuild.run()
    assert len(calls)==3 and models.configuration()['embedding_dim']==4
    assert retries.snapshot()['build_vectors']['paused']==3
    monkeypatch.setattr(settings(),'pipeline_mode','external')
    publish_state({'pid':123,'busy':False,'active':[],'queued':[],'pipeline':False,'stopping':False})
    assert client.post('/api/admin/jobs/build_vectors/retry-failed',headers=headers(accounts[0])).status_code==200
    command=one('SELECT * FROM pipeline_commands')
    assert command['action']=='start' and command['redo_id'] is None
    assert not rows('SELECT * FROM pipeline_redo_runs') and not retries.snapshot()
    assert vector_rebuild.pending() and models.configuration()['embedding_dim']==4


def test_retry_api_queues_only_failed_papers_and_rolls_back_when_busy(client,configured,accounts,papers,monkeypatch):
    admin,user=map(headers,accounts)
    fail('tldr_gen',papers[0]);fail('classify',papers[0])
    path='/api/admin/jobs/tldr_gen/retry-failed'
    assert client.post(path).status_code==401
    assert client.post(path,headers=user).status_code==403
    monkeypatch.setattr(settings(),'pipeline_mode','external')
    state={'pid':123,'busy':True,'active':['classify'],'queued':[],'pipeline':False,'stopping':False}
    publish_state(state)
    assert client.post(path,headers=admin).status_code==409
    assert retries.snapshot()['tldr_gen']['paused']==1
    assert not rows('SELECT * FROM pipeline_redo_runs')
    publish_state({**state,'busy':False,'active':[]})
    monkeypatch.setattr(models,'configuration_issue',lambda *a,**k:'尚未选择模型')
    assert client.post(path,headers=admin).status_code==409
    assert retries.snapshot()['tldr_gen']['paused']==1
    monkeypatch.setattr(models,'configuration_issue',lambda *a,**k:None)
    assert client.post(path,headers=admin).status_code==200
    command=one('SELECT * FROM pipeline_commands')
    assert command['action']=='redo' and command['name']=='tldr_gen'
    assert [v['paper_id'] for v in rows('SELECT * FROM pipeline_redo_items')]==[papers[0]]
    assert set(retries.snapshot())=={'classify'}
    assert redo.state(command['redo_id'])['options']['mode']=='retry'


@pytest.mark.asyncio
async def test_preread_cap_does_not_block_user_requests_or_count_cancellation(configured,papers,monkeypatch):
    monkeypatch.setattr(reading_queue,'dispatch',lambda:None)
    paper_id=papers[0]
    fail('preread',paper_id)
    with pytest.raises(retries.PausedError):reading_queue.enqueue(paper_id,retry=True,source='preread')
    assert not rows('SELECT * FROM reading_jobs')
    reading_queue.enqueue(paper_id,retry=True,source='user')
    job=one('SELECT * FROM reading_jobs')
    async def valid(*a):execute("UPDATE reading_cards SET status='ready',card_json='{}' WHERE paper_id=?",(paper_id,))
    monkeypatch.setattr(read,'run_card',valid)
    await reading_queue._run(job)
    assert retries.snapshot()=={}
    fail('preread',paper_id,1)
    execute("DELETE FROM reading_cards WHERE paper_id=?",(paper_id,))
    reading_queue.enqueue(paper_id,retry=True,source='preread')
    async def cancelled(*a):raise asyncio.CancelledError
    monkeypatch.setattr(read,'run_card',cancelled)
    with pytest.raises(asyncio.CancelledError):await reading_queue._run(one('SELECT * FROM reading_jobs'))
    assert retries.snapshot()['preread']['failed']==1
    assert one('SELECT failures FROM paper_task_failures')['failures']==1


@pytest.mark.asyncio
async def test_automatic_preread_failures_count_once_per_round_and_retry_reuses_queue(configured,papers,monkeypatch):
    monkeypatch.setattr(reading_queue,'dispatch',lambda:None)
    paper_id=papers[0]
    async def invalid(*a):execute("UPDATE reading_cards SET status='failed',error='invalid output' WHERE paper_id=?",(paper_id,))
    monkeypatch.setattr(read,'run_card',invalid)
    for round_number in range(1,4):
        reading_queue.enqueue(paper_id,retry=True,source='preread')
        await reading_queue._run(one('SELECT * FROM reading_jobs'))
        assert one('SELECT failures FROM paper_task_failures')['failures']==round_number
    with pytest.raises(retries.PausedError):reading_queue.enqueue(paper_id,retry=True,source='preread')
    with connect() as db:ident=retries.create_retry_run(db,'preread')
    queued=[]
    original=read.request_card
    def ready(ident,**kwargs):
        queued.append(kwargs)
        value=original(ident,**kwargs)
        execute("UPDATE reading_cards SET status='ready',card_json='{}' WHERE paper_id=?",(ident,))
        return value
    monkeypatch.setattr(read,'request_card',ready)
    assert await redo.run(ident,'preread')==1
    assert queued==[{'retry':True,'source':'preread'}]


@pytest.mark.asyncio
async def test_classification_round_limit_keeps_previous_labels(configured,papers,monkeypatch):
    from app.pipeline import classify
    before=rows('SELECT * FROM paper_topics WHERE paper_id=?',(papers[0],))
    async def invalid(*a):raise ValueError('invalid JSON')
    monkeypatch.setattr(classify,'predict_topic',invalid)
    paper=one('SELECT * FROM papers WHERE id=?',(papers[0],))
    for _ in range(3):
        with pytest.raises(ValueError):await classify.classify_paper(paper)
    with pytest.raises(retries.PausedError):await classify.classify_paper(paper)
    assert rows('SELECT * FROM paper_topics WHERE paper_id=?',(papers[0],))==before
    assert retries.snapshot()['classify']['paused']==1


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',[r'{"reason":"\gamma"}', '{"a":1}{"b":2}', '{"a":1 "b":2}'])
async def test_classification_retry_receives_failed_output_and_position_without_extra_attempts(client,monkeypatch,bad):
    from app.pipeline.topic_decision import predict_topic, ClassificationError
    from .test_research_areas import paper,area,decision
    selected=area('Reinforcement Learning');calls=[]
    async def complete(feature,messages,**kwargs):
        calls.append(messages)
        if len(calls)==1:return json.loads(bad)
        assert messages[-2]=={'role':'assistant','content':bad}
        assert 'column' in messages[-1]['content'] and 'char' in messages[-1]['content']
        return decision(selected['key'])
    monkeypatch.setattr(models,'complete',complete)
    result=await predict_topic(paper(),[selected])
    assert result['standard_key']==selected['key'] and result['attempts']==2
    assert len(calls)==2 and calls[0][0]==calls[1][0]
    calls.clear()
    async def invalid(*a,**k):calls.append(1);return json.loads(bad)
    monkeypatch.setattr(models,'complete',invalid)
    with pytest.raises(ClassificationError):await predict_topic(paper(),[selected])
    assert len(calls)==2
