import asyncio
import json

import httpx
import pytest

from app.config import now, settings, today, Settings
from app.db import execute, one, rows
from app.pipeline import author_impact as module
from app.task_settings import AuthorImpact, configuration, update_advanced
from .test_author_impact import seed, cached


def linked(authors=('A1', 'A2')):
    ident=seed()
    execute('INSERT INTO author_work_matches VALUES(?,?,?,?)',(ident,'W1','matched',now()))
    for author in authors:
        execute('INSERT INTO paper_author_links VALUES(?,?,?,?)',(ident,author,author,1))
    return ident


def transport(monkeypatch, handler):
    original=httpx.AsyncClient
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))


def failure(author='A1', count=1, retry_at='2999-01-01T00:00:00+00:00'):
    execute('INSERT OR REPLACE INTO author_query_failures VALUES(?,?,?,?)',(author,count,retry_at,now()))


def test_author_404_continues_other_authors_and_papers_preserving_scores(client, monkeypatch):
    first,second=linked(),linked()
    cached('A1',20)
    execute("UPDATE author_impact_cache SET fetched_at='2000-01-01' WHERE author_id='A1'")
    execute('UPDATE papers SET author_impact=100,author_impact_known=1,quality_score=63')
    calls=[]
    def handle(request):
        calls.append((request.url.path,request.url.params.get('filter','')))
        if request.url.path=='/authors/A1':return httpx.Response(404)
        return httpx.Response(200,json={'meta':{'count':0 if 'A1' in request.url.params['filter'] else 5}})
    transport(monkeypatch,handle)
    assert asyncio.run(module.author_impact())==2
    assert [path for path,_ in calls].count('/authors/A1')==1
    assert len(calls)==3
    assert rows('SELECT author_impact,quality_score FROM papers ORDER BY id')==[{'author_impact':100.,'quality_score':63.}]*2
    assert one("SELECT highly_cited_count,fetched_at FROM author_impact_cache WHERE author_id='A1'")=={'highly_cited_count':20,'fetched_at':'2000-01-01'}
    record=one("SELECT * FROM author_query_failures WHERE author_id='A1'")
    assert record['failure_count']==1 and record['next_retry_at']>now()
    assert module.pending_papers(configuration()['advanced']['author_impact'],count=True)==0
    assert asyncio.run(module.author_impact())==0 and len(calls)==3
    assert {r['paper_id'] for r in rows('SELECT paper_id FROM author_work_matches')}=={first,second}


def test_missing_author_retries_after_cooldown_and_stops_after_three_attempts(client,monkeypatch):
    linked(('A1',));calls=[]
    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(404) if request.url.path=='/authors/A1' else httpx.Response(200,json={'meta':{'count':0}})
    transport(monkeypatch,handle)
    for attempt in range(1,4):
        assert asyncio.run(module.author_impact())==1
        assert one("SELECT failure_count FROM author_query_failures WHERE author_id='A1'")['failure_count']==attempt
        assert asyncio.run(module.author_impact())==0
        execute("UPDATE author_query_failures SET next_retry_at='2000-01-01T00:00:00+00:00'")
    assert asyncio.run(module.author_impact())==0
    assert len(calls)==6
    assert one('SELECT COUNT(*) n FROM author_impact_cache')['n']==0
    assert one('SELECT author_impact_known FROM papers')['author_impact_known']==0
    from app.db import connect
    with connect() as db:module.initialize(db)
    assert asyncio.run(module.author_impact())==0


def test_blocked_author_does_not_block_another_author_needing_refresh(client,monkeypatch):
    linked();failure();calls=[]
    def handle(request):
        calls.append(request.url.params['filter'])
        assert 'A2' in request.url.params['filter']
        return httpx.Response(200,json={'meta':{'count':5}})
    transport(monkeypatch,handle)
    assert asyncio.run(module.author_impact())==1
    assert len(calls)==1
    assert asyncio.run(module.author_impact())==0
    assert one("SELECT failure_count FROM author_query_failures WHERE author_id='A1'")['failure_count']==1


def test_success_clears_missing_author_failures(client,monkeypatch):
    linked(('A1',));failure(count=2,retry_at='2000-01-01T00:00:00+00:00')
    transport(monkeypatch,lambda request:httpx.Response(200,json={'meta':{'count':5}}))
    assert asyncio.run(module.author_impact())==1
    assert one('SELECT COUNT(*) n FROM author_query_failures')['n']==0


@pytest.mark.parametrize('path,status',[('/authors/A1',403),('/authors/A1',500),('/works',404)])
def test_service_failures_remain_errors_with_safe_service_specific_logs(client,monkeypatch,path,status):
    ident=linked(('A1',))
    def handle(request):
        if request.url.path==path:return httpx.Response(status,text='credential-must-not-be-logged')
        return httpx.Response(200,json={'meta':{'count':0}})
    transport(monkeypatch,handle)
    with pytest.raises(RuntimeError,match=f'OpenAlex 作者数据请求失败（HTTP {status}）'):
        asyncio.run(module.author_impact())
    assert one('SELECT COUNT(*) n FROM author_query_failures')['n']==0
    assert one('SELECT status FROM author_work_matches WHERE paper_id=?',(ident,))['status']=='error'
    details=[json.loads(row['detail']) for row in rows("SELECT detail FROM app_logs WHERE message='作者数据查询失败'")]
    assert details and details[-1]['service']=='OpenAlex' and details[-1]['path']==path
    assert details[-1]['status_code']==status and details[-1]['author_id']=='A1'
    assert 'credential-must-not-be-logged' not in json.dumps(details)


def test_stopping_during_404_does_not_consume_retry_attempt(client,monkeypatch):
    linked(('A1',));requested=asyncio.Event()
    def handle(request):
        if request.url.path=='/authors/A1':requested.set();return httpx.Response(404)
        return httpx.Response(200,json={'meta':{'count':0}})
    transport(monkeypatch,handle)
    from app.pipeline_control import cancellation_scope
    async def run():
        with cancellation_scope(requested):await module.author_impact()
    with pytest.raises(asyncio.CancelledError):asyncio.run(run())
    assert one('SELECT COUNT(*) n FROM author_query_failures')['n']==0


def test_author_batch_default_is_500_without_overwriting_saved_or_environment_values(client,monkeypatch):
    monkeypatch.delenv('AUTHOR_IMPACT_BATCH_SIZE',raising=False)
    assert Settings(_env_file=None).author_impact_batch_size==500
    assert AuthorImpact().batch_size==500
    monkeypatch.setattr(settings(),'author_impact_batch_size',500)
    assert configuration()['advanced']['author_impact']['batch_size']==500
    update_advanced('author_impact',{'batch_size':100})
    assert configuration()['advanced']['author_impact']['batch_size']==100
    monkeypatch.setenv('AUTHOR_IMPACT_BATCH_SIZE','25')
    assert Settings(_env_file=None).author_impact_batch_size==25
