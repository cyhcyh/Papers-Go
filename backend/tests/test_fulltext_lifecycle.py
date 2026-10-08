import asyncio
import json
from datetime import datetime, timedelta, timezone
import httpx
import pymupdf
import pytest
from app.config import now, settings
from app.db import execute, one, rows, dumps
from app.pipeline import fulltext_cache as cache, fulltext_sources as sources, parse
from .conftest import headers


def pdf_bytes(title='Graph Memory for Agents', author='Research Author'):
    with pymupdf.open() as doc:
        page=doc.new_page()
        page.insert_textbox((40,40,550,750),title+'\n'+author+'\nIntroduction\nVerified research findings.',fontsize=12)
        return doc.tobytes()


def mock_http(monkeypatch,handler):
    original=httpx.AsyncClient
    monkeypatch.setattr(parse.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    sources._indexes.clear()


def paper_url(ident):
    execute("UPDATE papers SET venue='ICML',venue_year=2026,pdf_url='https://openreview.net/pdf?id=graph',abs_url='https://openreview.net/forum?id=graph' WHERE id=?",(ident,))
    return one('SELECT * FROM papers WHERE id=?',(ident,))


def policy(**changes):
    from app.site_settings import configuration
    execute("UPDATE app_settings SET value=? WHERE name='site'",(dumps({**configuration(),**changes}),))


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',[403,200])
async def test_official_fallback_and_cached_reuse_leave_no_pdf(client,papers,monkeypatch,failure):
    paper=paper_url(papers[0]);calls=[];progress=[]
    root='<li><a href="/v306/">Volume 306</a> ICML 2026</li>'
    toc='<div class="paper"><p class="title">Graph Memory for Agents</p><p class="authors">Research Author</p><a href="/v306/research26a.html">abs</a><a href="https://raw.githubusercontent.com/mlresearch/v306/main/assets/research26a/research26a.pdf">Download PDF</a></div>'
    def handler(request):
        calls.append(str(request.url))
        if request.url.host=='openreview.net':return httpx.Response(failure,text='<html>Unavailable</html>')
        if request.url.host=='proceedings.mlr.press':return httpx.Response(200,text=root if request.url.path=='/' else toc)
        if request.url.host=='raw.githubusercontent.com':return httpx.Response(200,content=pdf_bytes())
        pytest.fail('Unexpected fulltext request: '+str(request.url))
    mock_http(monkeypatch,handler)
    with sources.reporting(lambda **v:progress.append(v)):
        text=await parse.ensure_fulltext(paper)
    assert 'Verified research findings' in text['text']
    assert text['_source']['name']=='PMLR' and text['_source']['version']=='会议正式稿'
    assert text['_source']['url']=='https://proceedings.mlr.press/v306/research26a.html'
    assert any(p['stage']=='resolving' for p in progress)
    assert not list((settings().data_dir/'pdf').iterdir())
    count=len(calls)
    assert await parse.ensure_fulltext(paper)==text and len(calls)==count
    assert one('SELECT pdf_path FROM papers WHERE id=?',(paper['id'],))['pdf_path'] is None


@pytest.mark.asyncio
async def test_arxiv_fallback_checks_authors_and_records_version(client,papers,monkeypatch):
    paper=paper_url(papers[0]);calls=[]
    async def no_source(*args,**kwargs):return None
    async def no_locations(*args,**kwargs):return []
    monkeypatch.setattr(sources,'official',no_source)
    monkeypatch.setattr(sources,'document',no_source)
    monkeypatch.setattr(sources,'openalex_locations',no_locations)
    atom='<feed xmlns="http://www.w3.org/2005/Atom">'+''.join(
        f'<entry><id>http://arxiv.org/abs/{ident}</id><title>Graph Memory for Agents</title><summary>Research.</summary><published>2026-09-01T00:00:00Z</published><author><name>{author}</name></author></entry>'
        for ident,author in [('2609.99999v1','Unrelated Author'),('2609.12345v2','Research Author')])+'</feed>'
    def handler(request):
        calls.append(str(request.url))
        if request.url.host=='openreview.net':return httpx.Response(403)
        if request.url.host=='export.arxiv.org':return httpx.Response(200,text=atom)
        if request.url.path=='/pdf/2609.12345v2':return httpx.Response(200,content=pdf_bytes())
        pytest.fail('Wrong paper downloaded: '+str(request.url))
    mock_http(monkeypatch,handler)
    text=await parse.ensure_fulltext(paper)
    assert text['_source']['preprint'] and text['_source']['version']=='2609.12345v2'
    assert text['_source']['url']=='https://arxiv.org/abs/2609.12345v2'
    assert not any('99999' in url for url in calls)
    assert not list((settings().data_dir/'pdf').iterdir())


@pytest.mark.asyncio
async def test_download_failure_does_not_keep_files(client,papers,monkeypatch):
    paper=paper_url(papers[0])
    mock_http(monkeypatch,lambda request:httpx.Response(403))
    with pytest.raises(RuntimeError,match='HTTP 403.*尚未调用精读模型'):
        await parse.ensure_fulltext(paper,resolve_pdf=False)
    assert not list((settings().data_dir/'pdf').iterdir())
    assert cache.get(paper['id']) is None


@pytest.mark.asyncio
async def test_cancelled_download_releases_pin_and_removes_partial_file(client,papers,monkeypatch):
    paper=paper_url(papers[0])
    started=asyncio.Event()
    class SlowPDF(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'%PDF-1.7\n'
            started.set()
            await asyncio.Event().wait()
    mock_http(monkeypatch,lambda request:httpx.Response(200,stream=SlowPDF()))
    @cache.using_fulltext
    async def download(ident):return await parse.ensure_fulltext(paper,resolve_pdf=False)
    task=asyncio.create_task(download(paper['id']))
    try:
        await asyncio.wait_for(started.wait(),2)
        assert len(rows('SELECT * FROM fulltext_cache_pins'))==1
        assert len(list((settings().data_dir/'pdf').iterdir()))==1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert not rows('SELECT * FROM fulltext_cache_pins')
        assert not list((settings().data_dir/'pdf').iterdir())
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)


def test_cache_expiry_capacity_active_protection_and_card_retention(client,papers):
    policy(fulltext_cache_mb=1)
    for ident in papers:cache.store(ident,{'text':'x'*600000})
    expired=(datetime.now(timezone.utc)-timedelta(days=10)).isoformat()
    execute('UPDATE fulltext_cache SET cached_at=?,accessed_at=? WHERE paper_id=?',(expired,expired,papers[0]))
    execute('INSERT INTO fulltext_cache_pins VALUES(?,?,?)',('active',papers[0],(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()))
    execute("INSERT INTO reading_cards(paper_id,status,card_json,created_at) VALUES(?,'ready',?,?)",(papers[0],dumps({'tldr':'Keep my card'}),now()))
    result=cache.cleanup()
    assert result['removed_texts']==2 and result['remaining_bytes']<1024*1024
    assert one('SELECT fulltext FROM papers WHERE id=?',(papers[0],))['fulltext'] is not None
    execute('DELETE FROM fulltext_cache_pins')
    assert cache.cleanup()['removed_texts']==1
    assert one('SELECT card_json FROM reading_cards')['card_json']==dumps({'tldr':'Keep my card'})


@pytest.mark.asyncio
async def test_zero_cache_policy_waits_for_active_work_then_cleans_legacy_pdf(client,papers):
    policy(fulltext_cache_days=0)
    folder=settings().data_dir/'pdf';folder.mkdir()
    (folder/f'{papers[0]}.pdf').write_bytes(b'legacy')
    (folder/'unrelated.pdf').write_bytes(b'unrelated')
    @cache.using_fulltext
    async def analyze(ident):
        cache.store(ident,{'text':'active analysis'})
        assert cache.cleanup()['removed_texts']==0
        assert (folder/f'{ident}.pdf').exists()
        assert one('SELECT fulltext FROM papers WHERE id=?',(ident,))['fulltext']
    await analyze(papers[0])
    assert one('SELECT fulltext FROM papers WHERE id=?',(papers[0],))['fulltext'] is None
    assert not (folder/f'{papers[0]}.pdf').exists() and (folder/'unrelated.pdf').exists()


def test_cache_settings_are_admin_only_persist_and_enforce_policy(client,accounts,papers):
    admin,regular=accounts;auth=headers(admin)
    original=client.get('/api/admin/site',headers=auth).json()
    assert (original['fulltext_cache_days'],original['fulltext_cache_mb'])==(7,500)
    public=client.get('/api/site').json()
    assert 'fulltext_cache_days' not in public and 'fulltext_cache_mb' not in public
    assert client.put('/api/admin/site',headers=headers(regular),json=original).status_code==403
    assert client.put('/api/admin/site',headers=auth,json={**original,'fulltext_cache_mb':-1}).status_code==422
    response=client.put('/api/admin/site',headers=auth,json={**original,'fulltext_cache_days':3,'fulltext_cache_mb':25})
    assert response.status_code==200
    omitted={k:v for k,v in original.items() if not k.startswith('fulltext_cache_')}
    saved=client.put('/api/admin/site',headers=auth,json=omitted).json()
    assert (saved['fulltext_cache_days'],saved['fulltext_cache_mb'])==(3,25)
    cache.store(papers[0],{'text':'already cached'})
    assert client.put('/api/admin/site',headers=auth,json={**saved,'fulltext_cache_days':0}).status_code==200
    assert not one('SELECT fulltext FROM papers WHERE id=?',(papers[0],))['fulltext']


@pytest.mark.parametrize('status',['ready','failed'])
def test_regular_user_cannot_replace_cached_card_by_any_flags(client,accounts,papers,status):
    settings().pipeline_mode='external'
    admin,regular=accounts;url=f'/api/papers/{papers[0]}/card'
    old=dumps({'tldr':'old result','reading_level':'L2'})
    execute('INSERT INTO reading_cards(paper_id,status,card_json,created_at) VALUES(?,?,?,?)',(papers[0],status,old,now()))
    for query,body in [('regenerate=true',{'regenerate':True}),('retry=true',{'retry':True}),('level=L3',{'level':'L3'})]:
        assert client.get(url+'?'+query,headers=headers(regular)).status_code==403
        assert client.post(url+'/stream',headers=headers(regular),json=body).status_code==403
    assert client.get(url,headers=headers(regular)).json()['card']['tldr']=='old result'
    assert not rows('SELECT * FROM reading_jobs')
    response=client.get(url+'?regenerate=true',headers=headers(admin))
    assert response.status_code==202 and response.json()['card']['tldr']=='old result'
    assert len(rows('SELECT * FROM reading_jobs'))==1


def test_regular_user_can_retry_first_failure_without_cached_card(client,accounts,papers):
    settings().pipeline_mode='external'
    execute("INSERT INTO reading_cards(paper_id,status,error,created_at) VALUES(?,'failed','download failed',?)",(papers[0],now()))
    response=client.get(f'/api/papers/{papers[0]}/card?retry=true',headers=headers(accounts[1]))
    assert response.status_code==202 and len(rows('SELECT * FROM reading_jobs'))==1


@pytest.mark.asyncio
async def test_chat_tool_respects_shared_card_regeneration_permission(client,accounts,papers):
    from app.agent.tools import execute_tool
    settings().pipeline_mode='external'
    regular=accounts[1]
    session=client.post('/api/chat/sessions',headers=headers(regular),json={}).json()['id']
    old=dumps({'tldr':'old result','reading_level':'L2'})
    execute("INSERT INTO reading_cards(paper_id,status,card_json,created_at) VALUES(?,'failed',?,?)",(papers[0],old,now()))
    result=await execute_tool('read_now',{'paper_id':papers[0]},regular['user']['id'],session,None)
    assert result['card']['tldr']=='old result' and not rows('SELECT * FROM reading_jobs')
    execute('UPDATE reading_cards SET card_json=NULL WHERE paper_id=?',(papers[0],))
    assert (await execute_tool('read_now',{'paper_id':papers[0]},regular['user']['id'],session,None))['status']=='pending'


def test_title_and_author_match_rejects_unrelated_preprint():
    paper={'title':'Graph Memory for Agents','authors':dumps(['Ada Lovelace','Alan Turing','Grace Hopper'])}
    assert sources.matches(paper,'Graph Memory for Agents',['Ada Lovelace','Alan Turing','Grace Hopper'])
    assert not sources.matches(paper,'Graph Memory for Agents',['Ada Lovelace','Other Author'])
    assert not sources.matches(paper,'Graph Memory for Animals',['Ada Lovelace','Alan Turing','Grace Hopper'])
