import asyncio
import json
from types import SimpleNamespace
import pytest
from app.config import now,today
from app.db import execute,one,rows,dumps,connect
from app.llm import runtime as models
from app.pipeline import tldr,embed,read
from .conftest import headers


def card_value():
    return {'tldr':'总结','method_summary':'方法概述','key_results':[], 'limitations':[],
            'read_priority':'worth_reading','paper_kind':'theoretical',
            'answers':dict(problem='研究一个图论问题。',related_work='比较已有方法。',method='使用归纳证明。',
                           evaluation='给出定理与适用条件。',future='作者明确提出的方向：未提及。\n基于论文的进一步建议：研究更一般的图。',summary='总结研究问题及结论。')}


def test_speed_settings_are_admin_editable_and_local_remains_serial(client,accounts):
    admin,user=accounts
    config=client.get('/api/admin/models',headers=headers(admin)).json()
    assert config['brief_cloud_concurrency']==4
    config['brief_cloud_concurrency']=6
    assert client.put('/api/admin/models',headers=headers(user),json=config).status_code==403
    assert client.put('/api/admin/models',headers=headers(admin),json=config).json()['brief_cloud_concurrency']==6
    assert models.concurrency('brief')==1
    config['routes']['brief']['primary']={'connection_id':'cloud','model':'qwen-turbo','thinking':'off'}
    assert client.put('/api/admin/models',headers=headers(admin),json=config).status_code==200
    assert models.concurrency('brief')==6
    config['brief_cloud_concurrency']=9
    assert client.put('/api/admin/models',headers=headers(admin),json=config).status_code==422


@pytest.mark.asyncio
async def test_speed_reading_processes_over_100_with_bounded_concurrency_and_snapshot(client,monkeypatch):
    with connect() as db:
        for i in range(111):db.execute('INSERT INTO papers(title,abstract,created_at,quality_score,ingested_date) VALUES(?,?,?,?,?)',(f'Paper {i}','Abstract',now(),i%3,today()))
    config=models.legacy_defaults();config['routes']['brief']['primary']={'connection_id':'cloud','model':'test'}
    execute("INSERT OR REPLACE INTO app_settings VALUES('models',?,?)",(dumps(config),now()))
    active=peak=0;called=[];extra=None
    async def generate(p):
        nonlocal active,peak,extra
        called.append(p['id']);active+=1;peak=max(peak,active)
        if extra is None:extra=execute('INSERT INTO papers(title,abstract,created_at,ingested_date) VALUES(?,?,?,?)',('Arrived later','Later',now(),today()))
        await asyncio.sleep(.001)
        execute('UPDATE papers SET brief_json=? WHERE id=?',(dumps({'title_zh':'译名'}),p['id']))
        active-=1
    monkeypatch.setattr(tldr,'generate_brief',generate)
    assert await tldr.tldr_gen()==111
    progress=json.loads(one("SELECT progress FROM source_status WHERE name='tldr_gen'")['progress'])
    assert progress['total']==progress['completed']==111 and progress['failed']==progress['pending']==0
    assert peak==4 and len(set(called))==111 and extra not in called
    assert one('SELECT brief_json FROM papers WHERE id=?',(extra,))['brief_json'] is None


@pytest.mark.asyncio
async def test_speed_reading_cursor_crosses_multiple_unknown_quality_batches(client,monkeypatch):
    with connect() as db:
        for i in range(200):
            db.execute('INSERT INTO papers(title,abstract,created_at,quality_score,scored,ingested_date) VALUES(?,?,?,?,1,?)',
                       (f'Paper {i}','Abstract',now(),60 if i<70 else None,today()))
    called=[]
    async def generate(p):
        called.append(p['id'])
        execute('UPDATE papers SET brief_json=? WHERE id=?',(dumps({'title_zh':'译名'}),p['id']))
    monkeypatch.setattr(tldr,'generate_brief',generate)
    assert await tldr.tldr_gen()==200
    assert len(called)==len(set(called))==200
    assert one('SELECT COUNT(*) n FROM papers WHERE brief_json IS NULL')['n']==0


@pytest.mark.asyncio
async def test_speed_reading_stop_cancels_requests_and_leaves_unfinished_for_next_run(client,papers,monkeypatch):
    config=models.legacy_defaults();config['routes']['brief']['primary']={'connection_id':'cloud','model':'test'}
    execute("INSERT OR REPLACE INTO app_settings VALUES('models',?,?)",(dumps(config),now()))
    started=asyncio.Event();active=0
    async def blocked(p):
        nonlocal active
        active+=1;started.set()
        try:await asyncio.Event().wait()
        finally:active-=1
    monkeypatch.setattr(tldr,'generate_brief',blocked)
    task=asyncio.create_task(tldr.tldr_gen());await started.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    progress=json.loads(one("SELECT progress FROM source_status WHERE name='tldr_gen'")['progress'])
    assert active==0 and progress['completed']==0 and progress['pending']==3 and progress['in_flight']==0
    assert all(p['brief_json'] is None for p in rows('SELECT brief_json FROM papers'))


def test_two_field_brief_accepts_legacy_output_but_rejects_incomplete_output():
    value=tldr.PaperBrief.model_validate({'title_zh':'译名','problem':'问题','contribution':'贡献','result':'结果'})
    assert value.model_dump()=={'title_zh':'译名','problem':'问题','contribution_result':'贡献 结果'}
    with pytest.raises(ValueError):tldr.PaperBrief.model_validate({'title_zh':'译名','problem':'问题'})


@pytest.mark.asyncio
async def test_vector_progress_retains_finished_stage_when_quality_is_stopped(client,papers,monkeypatch):
    execute('UPDATE papers SET embedding=NULL,scored=0');execute('DELETE FROM papers_vec')
    started=asyncio.Event()
    async def blocked(p):started.set();await asyncio.Event().wait()
    monkeypatch.setattr(embed,'score_paper',blocked)
    await embed.build_vectors()
    task=asyncio.create_task(embed.assess_quality());await started.wait()
    current=json.loads(one("SELECT progress FROM source_status WHERE name='assess_quality'")['progress'])
    assert current['stage']=='quality' and current['completed']==0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    progress=json.loads(one("SELECT progress FROM source_status WHERE name='build_vectors'")['progress'])
    assert progress['completed']==3 and progress['pending']==0
    assert json.loads(one("SELECT progress FROM source_status WHERE name='assess_quality'")['progress'])['pending']==3
    assert all(p['embedding'] and not p['scored'] for p in rows('SELECT embedding,scored FROM papers'))


@pytest.mark.asyncio
async def test_quality_failure_is_counted_without_losing_completed_vectors(client,papers,monkeypatch):
    execute('UPDATE papers SET embedding=NULL,scored=0');execute('DELETE FROM papers_vec')
    async def quality(p):
        if p['id']==papers[1]:raise ValueError('invalid')
        execute('UPDATE papers SET scored=1 WHERE id=?',(p['id'],))
    monkeypatch.setattr(embed,'score_paper',quality)
    await embed.build_vectors()
    with pytest.raises(RuntimeError):await embed.assess_quality()
    progress=json.loads(one("SELECT progress FROM source_status WHERE name='assess_quality'")['progress'])
    assert progress['completed']==2 and progress['failed']==1 and progress['pending']==0
    assert progress['stages'][0]['failed']==1
    assert json.loads(one("SELECT progress FROM source_status WHERE name='build_vectors'")['progress'])['completed']==3


@pytest.mark.asyncio
async def test_six_question_generation_covers_related_work_and_verifies_quotes(client,papers,monkeypatch):
    quote='Our main theorem holds for every simple connected graph.'
    sections=[{'section':name,'text':quote+' '+name} for name in ['Introduction','Related Work','Definitions','Method','Main theorem','Proof','Conclusion']]
    async def parse(p):return {'text':'\n'.join(s['text'] for s in sections),'sections':sections,'pages':[]}
    captured=[]
    async def stream(messages,tools,**kwargs):
        captured.append(messages[-1]['content']);value=card_value()
        value['key_results']=[{'claim':'一个定理','evidence':{'section':'Proof','quote':quote}}]
        yield SimpleNamespace(content=dumps(value),tool_calls=None)
    monkeypatch.setattr(read,'ensure_fulltext',parse);monkeypatch.setattr(models,'stream',stream)
    execute("INSERT INTO reading_cards(paper_id,status,created_at) VALUES(?,'pending',?)",(papers[0],now()))
    result=await read.generate_card(papers[0])
    assert result['schema_version']==2 and len(result['answers'])==6 and result['key_results'][0]['verified']
    assert all(section['section'] in captured[0] for section in sections)


@pytest.mark.asyncio
async def test_long_reading_extracts_every_section_with_two_requests_at_most(client,monkeypatch):
    fulltext={'sections':[{'section':name,'text':name+' x'*8000} for name in ['Related Work','Method','Experiments']]}
    called=[];active=peak=0
    async def complete(feature,messages,**kwargs):
        nonlocal active,peak
        active+=1;peak=max(peak,active);called.append(json.loads(messages[-1]['content'])['text'])
        await asyncio.sleep(.001);active-=1;return 'Extracted evidence'
    monkeypatch.setattr(models,'complete',complete)
    materials=await read.reading_materials(fulltext,'L2')
    assert peak==2 and len(materials)==3
    assert all(name in ''.join(called) for name in ('Related Work','Method','Experiments'))


@pytest.mark.asyncio
async def test_explicit_regeneration_keeps_old_card_and_does_not_spawn_duplicates(client,papers,monkeypatch):
    config=models.configuration()
    config['connections'][1]['api_key']='offline-test-key'
    execute("UPDATE app_settings SET value=? WHERE name='models'",(dumps(config),))
    old={'tldr':'旧结果','method_summary':'旧方法','key_results':[],'limitations':[],'read_priority':'skim','reading_level':'L2'}
    execute("INSERT INTO reading_cards(paper_id,status,card_json,created_at) VALUES(?,'ready',?,?)",(papers[0],dumps(old),now()))
    monkeypatch.setattr(read,'_tasks',{})
    async def blocked(*args):await asyncio.Event().wait()
    monkeypatch.setattr(read,'run_card',blocked)
    assert read.request_card(papers[0])['status']=='ready' and not read._tasks
    pending=read.request_card(papers[0],regenerate=True);task=read._tasks[papers[0]]
    assert pending['status']=='pending' and json.loads(pending['card_json'])==old
    read.request_card(papers[0],regenerate=True)
    assert read._tasks[papers[0]] is task
    task.cancel();await asyncio.gather(task,return_exceptions=True)


def test_pending_and_failed_card_api_returns_previous_content(client,accounts,papers):
    old={'tldr':'仍可阅读'}
    execute("INSERT INTO reading_cards(paper_id,status,card_json,created_at) VALUES(?,'pending',?,?)",(papers[0],dumps(old),now()))
    response=client.get(f'/api/papers/{papers[0]}/card',headers=headers(accounts[1]))
    assert response.status_code==202 and response.json()['card']==old
    execute("UPDATE reading_cards SET status='failed',error='Failed' WHERE paper_id=?",(papers[0],))
    response=client.get(f'/api/papers/{papers[0]}/card',headers=headers(accounts[1]))
    assert response.json()['status']=='failed' and response.json()['card']==old
