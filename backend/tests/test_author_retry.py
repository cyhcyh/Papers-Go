import asyncio

import httpx
import pytest

from app.config import now, today
from app.db import execute, one, rows, dumps
from app.pipeline import author_impact as module
from app.pipeline import paper_retries
from app.task_settings import configuration
from .test_author_impact import seed, work
from .test_author_missing import linked, transport
from .conftest import headers


@pytest.mark.parametrize('initial_status',[200,400,500])
def test_title_fallback_only_after_error_keeps_strict_identity(initial_status):
    title='LEVER: Adaptive Cost-Aware Proof Search Over AND/OR Graphs'
    paper={'title':title,'authors':dumps(['Alice Researcher','Bob Scientist']),'published':today()}
    candidate={**work(),'title':title}
    calls=[]
    def handle(request):
        calls.append(request.url.params['filter'])
        if len(calls)==1 and initial_status!=200:return httpx.Response(initial_status)
        return httpx.Response(200,json={'results':[candidate]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await module.OpenAlex(client).match(paper)
    ident,authors=asyncio.run(run())
    assert ident=='W1' and len(authors)==2
    assert calls[0]=='title.search:'+module.title_text(title)
    assert len(calls)==(1 if initial_status==200 else 2)
    if len(calls)==2:
        assert calls[1]=='title.search:'+module.literal_title(title)
        assert '"AND" "OR"' in calls[1]


def test_fallback_does_not_accept_a_different_paper():
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(500) if len(calls)==1 else httpx.Response(200,json={'results':[work()]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await module.OpenAlex(client).match({'title':'A different AND/OR paper','authors':dumps(['Alice Researcher','Bob Scientist']),'published':today()})
    assert asyncio.run(run())==(None,[])
    assert len(calls)==2


def test_transient_failure_retries_once_then_skips_and_pauses_only_bad_paper(client,monkeypatch):
    bad=linked(('A1',));good=linked(('A2',));calls=[]
    execute('UPDATE papers SET author_impact=99,author_impact_known=1,quality_score=63 WHERE id=?',(bad,))
    def handle(request):
        author='A1' if 'A1' in request.url.params['filter'] else 'A2'
        calls.append(author)
        return httpx.Response(500) if author=='A1' else httpx.Response(200,json={'meta':{'count':5}})
    transport(monkeypatch,handle)
    for attempt in range(1,4):
        if attempt<3:
            with pytest.raises(RuntimeError,match='HTTP 500'):asyncio.run(module.author_impact())
        else:assert asyncio.run(module.author_impact())==1
        assert one("SELECT failures FROM paper_task_failures WHERE task='author_impact' AND paper_id=?",(bad,))['failures']==attempt
    assert calls.count('A1')==6 and calls.count('A2')==1
    assert one('SELECT status FROM author_work_matches WHERE paper_id=?',(good,))['status']=='matched'
    assert one('SELECT author_impact,quality_score FROM papers WHERE id=?',(bad,))=={'author_impact':99.,'quality_score':63.}
    assert module.pending_papers(configuration()['advanced']['author_impact'],count=True)==0
    assert asyncio.run(module.author_impact())==0 and len(calls)==7


@pytest.mark.parametrize('status',[401,403,429])
def test_authentication_and_quota_errors_do_not_retry_or_count(client,monkeypatch,status):
    seed();calls=[]
    def handle(request):calls.append(request);return httpx.Response(status)
    transport(monkeypatch,handle)
    with pytest.raises(RuntimeError):asyncio.run(module.author_impact())
    assert len(calls)==1
    assert not rows('SELECT * FROM paper_task_failures')


def test_network_failure_has_only_one_retry(client,monkeypatch):
    ident=seed();calls=[]
    def handle(request):calls.append(request);raise httpx.ConnectError('temporary',request=request)
    transport(monkeypatch,handle)
    with pytest.raises(RuntimeError):asyncio.run(module.author_impact())
    assert len(calls)==2
    assert one('SELECT failures FROM paper_task_failures WHERE paper_id=?',(ident,))['failures']==1


def test_stop_during_failed_request_does_not_count(client,monkeypatch):
    seed();stopped=asyncio.Event()
    def handle(request):stopped.set();return httpx.Response(500)
    transport(monkeypatch,handle)
    from app.pipeline_control import cancellation_scope
    async def run():
        with cancellation_scope(stopped):await module.author_impact()
    with pytest.raises(asyncio.CancelledError):asyncio.run(run())
    assert not rows('SELECT * FROM paper_task_failures')


def test_success_resets_budget_and_source_exposes_author_recovery(client,accounts,monkeypatch):
    ident=linked(('A1',));paper_retries.failed('author_impact',[ident])
    item=next(s for s in client.get('/api/admin/sources',headers=headers(accounts[0])).json() if s['name']=='author_impact')
    assert item['author_retries']=={'failed':1,'paused':0,'limit':3} and item['retries'] is None
    transport(monkeypatch,lambda request:httpx.Response(200,json={'meta':{'count':5}}))
    assert asyncio.run(module.author_impact())==1
    assert not rows('SELECT * FROM paper_task_failures')


@pytest.mark.asyncio
async def test_only_manual_run_resets_paper_budget(client,monkeypatch):
    from app import scheduler
    from app.pipeline import author_runs
    ident=seed()
    execute("INSERT INTO paper_task_failures VALUES('author_impact',?,3,?)",(ident,now()))
    observed=[]
    async def author():
        observed.append(one("SELECT COUNT(*) n FROM paper_task_failures WHERE task='author_impact'")['n'])
        execute("INSERT INTO source_status(name,progress) VALUES('author_impact',?) ON CONFLICT(name) DO UPDATE SET progress=excluded.progress",(dumps({'processed':0,'pending':0}),))
        return 0
    monkeypatch.setattr(scheduler,'jobs',{'author_impact':author})
    await scheduler.run_scheduled('author_impact')
    assert author_runs.read()['phase']=='complete' and observed==[1]
    await scheduler.start_manual('author_impact')
    assert observed==[1,0]
