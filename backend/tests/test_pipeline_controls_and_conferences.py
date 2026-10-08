import asyncio
import json
import threading
import httpx
import pytest

from app.config import now, settings, today
from app.db import execute, one, rows, dumps, set_paper_vector
from app.pipeline.fetch import parse_conference_feed, upsert_paper, fetch_conf
from app.pipeline_control import publish_state, queue_command, public_state
from .conftest import headers


def atom(title='Actual Conference Paper', year=2026):
    return f'''<feed xmlns="http://www.w3.org/2005/Atom"><title>AAAI.{year}</title>
      <entry><id>https://papers.cool/venue/36958@AAAI</id><title>{title}</title>
      <summary>Research abstract with real results.</summary><author><name>Alice</name></author>
      <updated>2099-01-01T00:00:00Z</updated></entry></feed>'''


def test_feed_year_metadata_and_reject_numbered_title(client):
    year,papers=parse_conference_feed(atom(),'AAAI')
    assert year==2026 and papers[0]['title']=='Actual Conference Paper'
    assert papers[0]['venue']=='AAAI' and papers[0]['primary_category'] is None
    assert papers[0]['published'] is None and papers[0]['authors']==['Alice']
    with pytest.raises(ValueError,match='无效论文标题'): parse_conference_feed(atom('#12'),'AAAI')
    with pytest.raises(ValueError,match='最新届次'): parse_conference_feed(atom(),'ICML')


def test_legacy_repair_keeps_id_favorites_and_only_venue_membership(client,accounts):
    _,paper_list=parse_conference_feed(atom(),'AAAI');paper=paper_list[0]
    ident=execute('INSERT INTO papers(arxiv_id,title,authors,venue,primary_category,published,created_at,ingested_date,brief_json,classified) VALUES(?,?,?,?,?,?,?,?,?,1)',
                  ('ICML.2026:36958@AAAI','#12','[]','ICLR.2026','cs.AI',today(),now(),today(),'{"old":"bad"}'))
    set_paper_vector(ident,[1,0,0,0])
    execute('INSERT INTO user_paper_state(user_id,paper_id,saved) VALUES(?,?,1)',(accounts[0]['user']['id'],ident))
    assert upsert_paper(paper) is False
    saved=one('SELECT * FROM papers WHERE id=?',(ident,))
    assert saved['title']==paper['title'] and saved['venue']=='AAAI' and saved['venue_year']==2026
    assert saved['primary_category'] is None and saved['brief_json'] is None and saved['embedding'] is None
    assert saved['classified']==0
    assert one('SELECT saved FROM user_paper_state WHERE paper_id=?',(ident,))['saved']==1
    assert client.get('/api/browse?category=arxiv:cs.AI&range=all').json()['total']==0
    # An edition year is not a publication date in the last seven days.
    assert client.get('/api/browse?category=venue:AAAI&range=all').json()['total']==1
    assert client.get('/api/browse?category=venue:AAAI').json()['total']==0
    assert client.get('/api/recommendations').json()['items'][0]['source_label']=='AAAI'
    execute('UPDATE papers SET scored=1 WHERE id=?',(ident,))
    revision=one("SELECT value FROM app_settings WHERE name='paper_revision'")['value']
    assert not upsert_paper(paper)
    assert one("SELECT value FROM app_settings WHERE name='paper_revision'")['value']==revision
    assert one('SELECT scored FROM papers WHERE id=?',(ident,))['scored']==1
    assert one('SELECT COUNT(*) n FROM papers')['n']==1


@pytest.mark.asyncio
async def test_conference_feed_incremental_headers_and_no_year_override(client,monkeypatch):
    settings().conference_sources='AAAI.1999'
    execute("UPDATE source_categories SET fetch_enabled=(code='AAAI') WHERE kind='venue'")
    from app.source_catalog import invalidate
    invalidate()
    execute('INSERT INTO conference_editions(source_key,venue,year,date_month,source_url,checked_at,dates_ready) VALUES(?,?,?,?,?,?,1)',
            ('venue:AAAI','AAAI',2026,'2026-01','https://ojs.aaai.org/index.php/AAAI/index',now()))
    calls=[]
    def handler(request):
        calls.append(request)
        assert str(request.url)=='https://papers.cool/venue/AAAI/feed'
        if request.headers.get('if-none-match')=='test-etag':return httpx.Response(304)
        return httpx.Response(200,text=atom(),headers={'etag':'test-etag'})
    original=httpx.AsyncClient
    monkeypatch.setattr('app.pipeline.fetch.httpx.AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    assert await fetch_conf()==1
    assert await fetch_conf()==0 and len(calls)==2
    assert one('SELECT year FROM conference_sync')['year']==2026
    assert json.loads(one("SELECT progress FROM source_status WHERE name='fetch_conf'")['progress'])=={'processed':1}


@pytest.mark.asyncio
async def test_disable_queued_stage_does_not_stop_running_stage(client,accounts,monkeypatch):
    import app.scheduler as scheduler
    from app.pipeline_control import set_job_enabled
    monkeypatch.setattr(scheduler,'_pipeline_lock',asyncio.Lock())
    monkeypatch.setattr(scheduler,'_job_tasks',{})
    monkeypatch.setattr(scheduler,'_running_jobs',{})
    started,release=asyncio.Event(),asyncio.Event();calls=[]
    async def first():
        calls.append('first');started.set();await release.wait()
    async def second():calls.append('second')
    monkeypatch.setattr(scheduler,'jobs',{'first':first,'second':second})
    task=asyncio.create_task(scheduler._stages(['first','second']))
    await asyncio.wait_for(started.wait(),1)
    set_job_enabled('first',False);set_job_enabled('second',False)
    assert not task.done()
    release.set();await task
    assert calls==['first']
    set_job_enabled('second',True)
    await scheduler.run_job('second')
    assert calls==['first','second']


def test_job_switch_persists_and_manual_disabled_guard(client,accounts):
    admin,regular=accounts
    path='/api/admin/jobs/fetch_conf'
    assert client.patch(path,json={'enabled':False},headers=headers(regular)).status_code==403
    assert client.patch(path,json={'enabled':False},headers=headers(admin)).status_code==200
    assert client.post(path,headers=headers(admin)).status_code==409
    assert next(s for s in client.get('/api/admin/sources',headers=headers(admin)).json() if s['name']=='fetch_conf')['enabled'] is False
    assert one("SELECT value FROM app_settings WHERE name='pipeline_enabled'")


def test_worker_commands_are_shared_and_only_one_start_is_queued(client):
    settings().pipeline_mode='external'
    publish_state({'busy':False,'active':[],'queued':[],'pipeline':False,'stopping':False,'pid':123})
    queue_command('start','fetch_conf')
    state=public_state()
    assert state['busy'] and state['queued']==['fetch_conf']
    from fastapi import HTTPException
    with pytest.raises(HTTPException):queue_command('start','classify')
    queue_command('stop','fetch_conf')
    assert [r['action'] for r in rows('SELECT action FROM pipeline_commands ORDER BY id')]==['start','stop']


def test_bulk_chat_rejects_foreign_sessions_before_deletion(client,accounts):
    admin,other=accounts
    own=[client.post('/api/chat/sessions',headers=headers(admin),json={'title':t}).json()['id'] for t in ('One','Two')]
    foreign=client.post('/api/chat/sessions',headers=headers(other),json={'title':'Private'}).json()['id']
    assert client.post('/api/chat/sessions/batch-delete',headers=headers(admin),json={'ids':[own[0],foreign]}).status_code==404
    assert len(client.get('/api/chat/sessions',headers=headers(admin)).json())==2
    result=client.post('/api/chat/sessions/batch-delete',headers=headers(admin),json={'ids':own}).json()
    assert result['deleted']==own and client.get('/api/chat/sessions',headers=headers(admin)).json()==[]
    assert client.get('/api/chat/sessions',headers=headers(other)).json()[0]['id']==foreign


def test_bulk_topics_state_rules_and_delete_unblocks_standard(client,accounts):
    from app.standard_topics import catalog
    entry=catalog()['RA-MATH-060']
    ident=execute('INSERT INTO topics(name_zh,name_en,status,created_at,standard_key,standard_system,standard_code,standard_path,category_keys) VALUES(?,?,?,?,?,?,?,?,?)',
                  ('Ramsey理论',entry['label'],'proposed',now(),entry['key'],entry['system'],entry['code'],entry['path'],dumps(['arxiv:math.CO'])))
    endpoint='/api/admin/topics/batch';auth=headers(accounts[0])
    assert client.post(endpoint,headers=headers(accounts[1]),json={'ids':[ident],'action':'approve'}).status_code==403
    result=client.post(endpoint,headers=auth,json={'ids':[ident],'action':'approve'}).json()
    assert result['completed']==[ident] and one('SELECT status FROM topics WHERE id=?',(ident,))['status']=='active'
    assert client.post(endpoint,headers=auth,json={'ids':[ident],'action':'delete'}).json()['failed']
    assert client.post(endpoint,headers=auth,json={'ids':[ident],'action':'disable'}).json()['completed']==[ident]
    assert client.post(endpoint,headers=auth,json={'ids':[ident],'action':'delete'}).json()['completed']==[ident]
    assert not one('SELECT id FROM topics WHERE standard_key=?',(entry['key'],))


def test_recommendation_cache_uses_ids_and_reacts_to_scope_changes(client,accounts,papers,monkeypatch):
    import app.pipeline.score as score
    calls=0;original=score.candidate_pool
    def counted(*args,**kwargs):
        nonlocal calls
        calls+=1
        return original(*args,**kwargs)
    monkeypatch.setattr(score,'candidate_pool',counted)
    auth=headers(accounts[0])
    first=client.get('/api/feed/today',headers=auth).json()
    second=client.get('/api/feed/today',headers=auth).json()
    # The short first page also checks history once. The second request reuses both.
    assert calls==2 and [p['id'] for p in first['items']]==[p['id'] for p in second['items']]
    execute('UPDATE papers SET venue=? WHERE id=?',('AAAI',papers[0]))
    assert client.get('/api/feed/today',headers=auth).status_code==200 and calls==4
    entry=list(score._ranking_cache.values())[-1]
    assert 'abstract' not in entry and 'items' not in entry


@pytest.mark.parametrize('authenticated',[False,True])
def test_browse_streamed_ranking_preserves_scores_and_all_pages(client,accounts,papers,authenticated):
    from app.pipeline.score import scored_papers
    uid=accounts[0]['user']['id'] if authenticated else None
    expected=scored_papers(uid,rows('SELECT * FROM papers'))
    expected.sort(key=lambda p:(p['score'],p['published'] or p['ingested_date'],p['id']),reverse=True)
    auth=headers(accounts[0]) if authenticated else {}
    actual=[]
    for offset in range(0,len(expected),2):
        page=client.get(f'/api/browse?range=all&sort=score&limit=2&offset={offset}',headers=auth).json()
        assert page['total']==len(expected)
        actual.extend(page['items'])
    assert [(p['id'],p['score']) for p in actual]==[(p['id'],p['score']) for p in expected]
    assert all(isinstance(p['authors'],list) and 'abstract' in p for p in actual)
