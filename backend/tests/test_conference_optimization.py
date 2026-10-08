import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import now, today
from app.db import connect, dumps, execute, one, rows
from app.pipeline import conferences as conf
from app.pipeline.fetch import fetch_conf, parse_conference_feed, upsert_paper
from app.source_catalog import invalidate, sources_to_fetch
from .test_pipeline_controls_and_conferences import atom


@pytest.fixture
def source(client):
    execute("UPDATE source_categories SET fetch_enabled=(code='AAAI') WHERE kind='venue'")
    invalidate()
    return sources_to_fetch('venue')[0]


def calendar(source,year=2026):
    execute('INSERT INTO conference_editions(source_key,venue,year,date_month,source_url,checked_at,dates_ready) VALUES(?,?,?,?,?,?,1)',
            (source['key'],'AAAI',year,f'{year}-01','https://ojs.aaai.org/index.php/AAAI/index',now()))


def transport(monkeypatch, handler):
    original=httpx.AsyncClient
    monkeypatch.setattr(conf.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))


def test_latest_index_ignores_subtracks_and_other_venues():
    html='<a href="/venue/AAAI.2025">old</a><a href="/venue/AAAI.2026">new</a><a href="/venue/AAAI.2099/Track">track</a><a href="/venue/ICML.2026">other</a>'
    assert conf.latest_editions(html,['AAAI'])=={'AAAI':2026}


@pytest.mark.parametrize('venue,html,expected',[
    ('ICML','<h3>ICML 2026 Meeting Dates</h3><table><tr><td>Main Conference</td><td>Tue Jul 7th through Thu the 9th</td></tr></table>','2026-07'),
    ('ICLR','<h3>ICLR 2026 Meeting Dates</h3><p><strong>Main Conference</strong>: Thursday April 23 through Saturday April 25<br><strong>Workshops</strong>: Sunday April 26</p>','2026-04'),
    ('NeurIPS','<h3>NeurIPS 2026 Meeting Dates</h3><table><tr><td>Conference Sessions</td><td>Wed Dec 3rd through Fri Dec 5th</td></tr></table>','2026-12'),
    ('AAAI','The Fortieth AAAI Conference on Artificial Intelligence was held on January 20 -- January 27, 2026, Singapore. Published: 2026-03-17','2026-01'),
    ('AAAI','AAAI Conference on Artificial Intelligence was held on January 27, 2025. Published: 2026-03-17',None),
    ('ICML','<h3>ICML 2026</h3><tr><td>Main Conference Paper Submission</td><td>Jan 20</td></tr>',None),
    ('ICLR','<nav>2026</nav><h3>ICLR 2025 Meeting Dates</h3><p><strong>Main Conference</strong>: April 23</p>',None),
])
def test_month_parser_uses_meeting_dates_not_deadlines_or_issue_publication(venue,html,expected):
    assert conf.parse_calendar_month(html,venue,2026)==expected


@pytest.mark.asyncio
async def test_daily_only_downloads_one_small_index_and_does_not_advance_full_check(source,monkeypatch):
    calendar(source)
    stamp=now()
    execute('INSERT INTO conference_sync(venue,year,checked_at) VALUES(?,?,?)',('AAAI',2026,stamp))
    calls=[]
    def handler(request):
        calls.append(str(request.url));assert calls[-1]=='https://papers.cool/'
        return httpx.Response(200,text='<a href="/venue/AAAI.2026">AAAI.2026</a>')
    transport(monkeypatch,handler)
    assert await fetch_conf(force=False)==0
    assert calls==['https://papers.cool/']
    assert one('SELECT checked_at FROM conference_sync')['checked_at']==stamp
    assert not one('SELECT 1 FROM conference_entry_versions')


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['weekly','manual','new_edition'])
async def test_full_check_due_or_forced_or_new_edition(source,monkeypatch,mode):
    year=2027 if mode=='new_edition' else 2026
    calendar(source,year)
    checked=(datetime.now(timezone.utc)-timedelta(days=8)).isoformat() if mode=='weekly' else now()
    execute('INSERT INTO conference_sync(venue,year,checked_at) VALUES(?,?,?)',('AAAI',2026,checked))
    calls=[]
    def handler(request):
        calls.append(str(request.url))
        if calls[-1]=='https://papers.cool/':return httpx.Response(200,text=f'<a href="/venue/AAAI.{year}">edition</a>')
        assert calls[-1]==source['feed_url']
        return httpx.Response(200,text=atom(year=year))
    transport(monkeypatch,handler)
    assert await fetch_conf(force=mode=='manual')==1
    assert calls==([source['feed_url']] if mode=='manual' else ['https://papers.cool/',source['feed_url']])
    assert one('SELECT year,checked_at FROM conference_sync')['year']==year
    assert one('SELECT COUNT(*) n FROM conference_entry_versions')['n']==1


@pytest.mark.asyncio
async def test_generated_updated_timestamp_does_not_write_or_invalidate_results(source,monkeypatch):
    calendar(source);responses=iter([atom(),atom().replace('2099-01-01','2099-01-02')])
    transport(monkeypatch,lambda request:httpx.Response(200,text=next(responses)))
    assert await fetch_conf()==1
    ident=one('SELECT id FROM papers')['id']
    execute("UPDATE papers SET brief_json='{}',classified=1,scored=1 WHERE id=?",(ident,))
    before=one('SELECT ingested_date,expires_at,brief_json,classified,scored FROM papers')
    revision=one("SELECT value FROM app_settings WHERE name='paper_revision'")
    def forbidden(*args):raise AssertionError('unchanged paper entered upsert')
    monkeypatch.setattr('app.pipeline.fetch.upsert_paper',forbidden)
    assert await fetch_conf()==0
    assert before==one('SELECT ingested_date,expires_at,brief_json,classified,scored FROM papers')
    assert revision==one("SELECT value FROM app_settings WHERE name='paper_revision'")


def test_first_signature_seed_skips_existing_upsert_then_content_correction_updates(source,monkeypatch):
    _,papers=parse_conference_feed(atom(),'AAAI');upsert_paper(papers[0])
    original=upsert_paper;calls=[]
    def counted(*args):calls.append(args[0]['title']);return original(*args)
    monkeypatch.setattr('app.pipeline.fetch.upsert_paper',counted)
    assert conf.reconcile_batch(papers,source)==(0,0) and calls==[]
    ident=one('SELECT id FROM papers')['id']
    lifetime=one('SELECT ingested_date,expires_at FROM papers')
    papers[0]['title']='Corrected real title'
    assert conf.reconcile_batch(papers,source)==(0,1) and calls==['Corrected real title']
    assert one('SELECT id,title FROM papers')=={'id':ident,'title':'Corrected real title'}
    assert lifetime==one('SELECT ingested_date,expires_at FROM papers')


def test_disabled_source_and_retired_paper_are_not_recreated(source):
    _,papers=parse_conference_feed(atom(),'AAAI')
    execute('UPDATE source_categories SET fetch_enabled=0 WHERE key=?',(source['key'],))
    assert conf.reconcile_batch(papers,source) is None and not one('SELECT id FROM papers')
    execute('UPDATE source_categories SET fetch_enabled=1 WHERE key=?',(source['key'],))
    execute('INSERT INTO retired_paper_sources(source_id,retired_at) VALUES(?,?)',(papers[0]['source_id'],now()))
    assert conf.reconcile_batch(papers,source)==(0,0) and not one('SELECT id FROM papers')


@pytest.mark.asyncio
async def test_partial_date_refresh_resumes_without_changing_lifetime(client,source,monkeypatch):
    _,papers=parse_conference_feed(atom(),'AAAI');upsert_paper(papers[0])
    before=one('SELECT id,ingested_date,expires_at,brief_json FROM papers')
    revision=one("SELECT value FROM app_settings WHERE name='paper_revision'")
    assert client.get('/api/browse?month=2026-01&sort=score').json()['total']==0
    # Metadata saved before an interruption; display materialization is still pending.
    ids=conf.save_calendar(source,2026,'2026-01','official-url')
    assert ids==[before['id']]
    assert one('SELECT dates_ready FROM conference_editions')['dates_ready']==0
    transport(monkeypatch,lambda request:pytest.fail('cached month must not be fetched again'))
    async with conf.httpx.AsyncClient() as http:
        await conf.ensure_calendar(http,source,2026)
    assert one('SELECT paper_date,paper_date_basis FROM papers')=={'paper_date':'2026-01','paper_date_basis':'conference'}
    assert one('SELECT id,ingested_date,expires_at,brief_json FROM papers')==before
    assert one('SELECT dates_ready FROM conference_editions')['dates_ready']==1
    assert one("SELECT value FROM app_settings WHERE name='paper_revision'")==revision
    assert client.get('/api/browse?month=2026-01&sort=score').json()['total']==1


@pytest.mark.asyncio
async def test_cancel_waits_for_current_transaction():
    started=threading.Event();release=threading.Event();committed=threading.Event()
    def write():
        started.set();release.wait(2);committed.set()
    task=asyncio.create_task(conf.finish_commit(write))
    assert await asyncio.to_thread(started.wait,1)
    task.cancel();await asyncio.sleep(.02)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):await task
    assert committed.is_set()


@pytest.mark.asyncio
async def test_failed_index_does_not_download_full_feeds_or_advance_sync(source,monkeypatch):
    execute('INSERT INTO conference_sync(venue,year,checked_at) VALUES(?,?,?)',('AAAI',2026,now()))
    before=one('SELECT * FROM conference_sync');calls=[]
    def handler(request):calls.append(str(request.url));return httpx.Response(503)
    transport(monkeypatch,handler)
    with pytest.raises(RuntimeError,match='届次检查失败'):await fetch_conf(force=False)
    assert calls==['https://papers.cool/'] and before==one('SELECT * FROM conference_sync')


def test_date_precision_sorting_filters_and_index(client,source):
    calendar(source)
    def paper(ident,published,year=2026,venue='AAAI'):
        return execute('INSERT INTO papers(arxiv_id,title,authors,published,venue,venue_year,created_at,ingested_date) VALUES(?,?,?,?,?,?,?,?)',
                       (ident,ident,'[]',published,venue,year,now(),today()))
    a=paper('a','2026-01-15');b=paper('b',None);c=paper('c',None,2026,'ICML');d=paper('d','2025-12-31',2025);e=paper('e',None,None,None)
    page=client.get('/api/browse?range=all&sort=date&year=2026').json()
    assert [p['id'] for p in page['items']]==[a,b,c]
    assert [(p['paper_date'],p['paper_date_basis']) for p in page['items']]==[('2026-01-15','publication'),('2026-01','conference'),('2026','year')]
    assert client.get('/api/browse?month=2026-01&sort=date').json()['total']==2
    assert client.get('/api/browse?month=2026-02').json()['total']==0
    assert client.get('/api/browse?range=today').json()['total']==0
    assert client.get('/api/browse?range=today&date_basis=ingested&sort=date').json()['total']==5
    assert client.get('/api/browse?month=2026-99').status_code==422
    with connect() as db:
        plan=' '.join(r['detail'] for r in db.execute('EXPLAIN QUERY PLAN SELECT id FROM papers INDEXED BY idx_papers_paper_date ORDER BY paper_date_sort DESC,id DESC LIMIT 30'))
    assert 'idx_papers_paper_date' in plan and 'TEMP B-TREE' not in plan


def test_real_arxiv_date_is_preserved_and_source_cache_cascades_on_delete(client,source):
    _,papers=parse_conference_feed(atom(),'AAAI');p=papers[0]
    ident=execute('INSERT INTO papers(arxiv_id,title,title_key,authors,abstract,published,created_at,ingested_date) VALUES(?,?,?,?,?,?,?,?)',
                  ('2601.12345',p['title'],'actual conference paper','[]',p['abstract'],'2026-01-10',now(),today()))
    # Link explicitly, matching the real cross-source case.
    execute('INSERT INTO paper_sources VALUES(?,?,?,?)',(p['source_id'],ident,'AAAI',2026))
    assert conf.reconcile_batch(papers,source)[0]==0
    assert one('SELECT published,paper_date FROM papers')['published']=='2026-01-10'
    calendar(source)
    execute('DELETE FROM paper_sources WHERE source_id=?',(p['source_id'],))
    assert not one('SELECT * FROM conference_entry_versions')
    execute('DELETE FROM source_categories WHERE key=?',(source['key'],))
    assert not one('SELECT * FROM conference_editions')


@pytest.mark.asyncio
async def test_scheduler_is_lightweight_but_manual_stage_and_pipeline_force_full_check(client,monkeypatch):
    import app.scheduler as scheduler
    flags=[]
    async def fetch(force=True):flags.append(force);return 0
    monkeypatch.setattr(scheduler,'jobs',{'fetch_conf':fetch})
    monkeypatch.setattr(scheduler,'_pipeline_lock',asyncio.Lock())
    monkeypatch.setattr(scheduler,'_job_tasks',{})
    monkeypatch.setattr(scheduler,'_running_jobs',{})
    await scheduler.run_job('fetch_conf')
    await scheduler.start_manual('fetch_conf')
    await scheduler.start_manual('pipeline')
    assert flags==[False,True,True]
