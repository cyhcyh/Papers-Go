import asyncio
import json
import httpx
import pytest

from app import scheduler
from app.db import execute, one, rows
from app.pipeline import fetch
from app.source_catalog import invalidate
from .test_arxiv_sync import atom, install_mock


@pytest.mark.asyncio
async def test_complete_windows_are_reused_only_within_the_same_api_refresh_cycle(client,monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200,text=atom([123] if '202609290000 TO 202609300000' in request.url.params['search_query'] else []))
    install_mock(monkeypatch,respond)
    assert await fetch.fetch_arxiv() == 1
    assert len(calls) == 8  # One merged request for each day, including the partial day.
    calls.clear()
    assert await fetch.fetch_arxiv() == 0 and calls == []
    # DST in October: the next API refresh is at 04:00 UTC.
    monkeypatch.setattr(fetch,'now',lambda:'2026-10-02T04:01:00+00:00')
    assert await fetch.fetch_arxiv() == 0 and len(calls) == 9
    assert 'cat:cs.AI OR cat:math.CO' in calls[0].url.params['search_query']


@pytest.mark.asyncio
async def test_timeout_reduces_page_size_without_skipping_offsets(client,monkeypatch):
    offsets = []
    def respond(request):
        query = request.url.params['search_query']
        offset, size = int(request.url.params['start']), int(request.url.params['max_results'])
        if '202609300000 TO 202610010000' in query:
            offsets.append((offset,size))
            if size==500:
                raise httpx.ReadTimeout('large page timed out',request=request)
            ids = list(range(1,251))
            return httpx.Response(200,text=atom(ids[offset:offset+size],offset,250))
        return httpx.Response(200,text=atom([]))
    install_mock(monkeypatch,respond)
    assert await fetch.fetch_arxiv() == 250
    assert offsets == [(0,500),(0,100),(100,100),(200,100)]


@pytest.mark.asyncio
async def test_deleted_member_of_merged_query_does_not_stop_remaining_category(client,monkeypatch):
    def respond(request):
        if '202609300000 TO 202610010000' in request.url.params['search_query']:
            execute("DELETE FROM source_categories WHERE key='arxiv:math.CO'")
            invalidate()
            return httpx.Response(200,text=atom([7]))
        return httpx.Response(200,text=atom([]))
    install_mock(monkeypatch,respond)
    assert await fetch.fetch_arxiv() == 1
    assert one("SELECT synced_through FROM arxiv_cursors WHERE category='cs.AI'")['synced_through']=='2026-10-01T12:00:00+00:00'
    assert not one("SELECT category FROM arxiv_window_checks WHERE category='math.CO' AND window_start='2026-09-30T00:00:00+00:00'")


@pytest.mark.asyncio
async def test_stopping_during_paging_keeps_saved_papers_without_marking_window_complete(client,monkeypatch):
    started = asyncio.Event()
    async def respond(request):
        if '202609300000 TO 202610010000' in request.url.params['search_query']:
            offset=int(request.url.params['start'])
            if offset==500:
                started.set()
                await asyncio.Event().wait()
            return httpx.Response(200,text=atom(range(1,501),offset,900))
        return httpx.Response(200,text=atom([]))
    install_mock(monkeypatch,respond)
    task=asyncio.create_task(fetch.fetch_arxiv())
    await asyncio.wait_for(started.wait(),3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert one('SELECT COUNT(*) n FROM papers')['n']==500
    assert all(r['synced_through']=='2026-09-30T00:00:00+00:00' for r in rows('SELECT synced_through FROM arxiv_cursors'))


def test_daily_schedule_follows_afternoon_arxiv_refresh(client):
    timer=scheduler.make_scheduler()
    for name,hour,minute in [('pipeline',13,30),('trend_report',17,20)]:
        job=timer.get_job(name)
        assert str(job.trigger.fields[-3])==str(hour) and str(job.trigger.fields[-2])==str(minute)
    assert timer.get_job('fetch_arxiv') is None
    assert timer.get_job('classify') is None
    assert len([job for job in timer.get_jobs() if not job.id.startswith('_')])==6


@pytest.mark.asyncio
async def test_rate_limit_failure_stops_other_windows_and_preserves_cursors(client,monkeypatch):
    calls=[]
    def respond(request):
        calls.append(request)
        return httpx.Response(429,headers={'Retry-After':'5'})
    install_mock(monkeypatch,respond)
    with pytest.raises(RuntimeError):
        await fetch.fetch_arxiv()
    assert len(calls)==3
    assert all(r['synced_through']=='2026-09-24T00:00:00+00:00' for r in rows('SELECT synced_through FROM arxiv_cursors'))
