import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.config import now, settings, today
from app.db import connect, dumps, execute, one, rows
from .conftest import headers


def test_stats_count_retained_feedback_and_current_day_views(client, accounts, papers):
    account, other = accounts
    auth = headers(account)
    for action in ('like', 'save', 'save'):
        client.post('/api/interactions', headers=auth, json={'paper_id': papers[0], 'action': action})
    execute('UPDATE interactions SET created_at=?', ('2026-01-01T00:00:00+00:00',))
    client.post('/api/interactions', headers=headers(other), json={'paper_id': papers[1], 'action': 'save'})
    stat = client.get('/api/stats/today', headers=auth).json()
    assert (stat['shown'], stat['like'], stat['save']) == (0, 1, 1)
    for duration in (4999, 5000, 9000):
        client.post('/api/interactions', headers=auth, json={'paper_id': papers[0], 'action': 'view', 'dwell_ms': duration})
    client.post('/api/interactions', headers=auth, json={'paper_id': papers[0], 'action': 'remove_save'})
    client.post('/api/interactions', headers=auth, json={'paper_id': papers[0], 'action': 'remove_like'})
    stat = client.get('/api/stats/today', headers=auth).json()
    assert (stat['shown'], stat['like'], stat['save']) == (1, 0, 0)
    saved = client.post('/api/interactions', headers=auth, json={'paper_id': papers[0], 'action': 'save'}).json()
    client.post('/api/interactions', headers=auth, json={'action': 'undo', 'target_id': saved['id']})
    assert client.get('/api/stats/today', headers=auth).json()['save'] == 0
    assert client.get('/api/stats/today', headers=headers(other)).json()['save'] == 1


def test_notification_count_does_not_return_messages(client, accounts):
    account, other = accounts
    for user, read in ((account, 0), (account, 0), (account, 1), (other, 0)):
        execute('INSERT INTO notifications(user_id,type,title,read,created_at) VALUES(?,?,?,?,?)',
                (user['user']['id'], 'test', 'Private notification', read, now()))
    assert client.get('/api/notifications/count').status_code == 401
    assert client.get('/api/notifications/count', headers=headers(account)).json() == {'unread': 2}
    assert client.get('/api/notifications/count', headers=headers(other)).json() == {'unread': 1}


@pytest.mark.parametrize('query', ['Graph', 'graph', 'Author', 'Other', '组合数', '组合', 'A_thor', 'Graph%', '"', '%', 'r", "A', '"alpha"', 'A B', '数😀🧪', 'jörg', 'JÖRG'])
def test_indexed_search_preserves_title_abstract_individual_author_matching(client, papers, query):
    from app.paper_index import search_clause
    execute('UPDATE papers SET title=?,authors=? WHERE id=?', ('组合数学 Graph (A B) "alpha" 数😀🧪', dumps(['Research Author', 'A Person', 'Jörg André']), papers[0]))
    pattern = '%' + query + '%'
    expected = rows('SELECT p.id FROM papers p WHERE p.title LIKE ? OR p.abstract LIKE ? OR EXISTS(SELECT 1 FROM json_each(p.authors) a WHERE a.value LIKE ?) ORDER BY p.id', [pattern] * 3)
    clause, args = search_clause(query)
    actual = rows('SELECT p.id FROM papers p WHERE ' + clause + ' ORDER BY p.id', args)
    assert actual == expected
    assert client.get('/api/browse', params={'query': query}).json()['total'] == len(expected)


def test_search_count_and_date_pagination_share_same_match_set(client, papers):
    items=[]
    for offset in range(3):
        page=client.get('/api/browse',params={'query':'graph','sort':'date','range':'all','offset':offset,'limit':1}).json()
        assert page['total']==2
        items.extend(p['id'] for p in page['items'])
    assert items==list(reversed(papers[:2]))


def test_search_backfill_resumes_and_tracks_edits_during_build(client, papers):
    from app.paper_index import MIGRATION, backfill, ready, search_clause
    with connect() as db:
        for i in range(260):
            db.execute('INSERT INTO papers(title,authors,created_at,ingested_date) VALUES(?,?,?,?)',
                       ('Backfill title ' + str(i), dumps(['Old Author']), now(), today()))
        db.execute('DELETE FROM paper_search')
        db.execute('DELETE FROM paper_search_state')
        db.execute('DELETE FROM paper_names')
        db.execute('DELETE FROM app_migrations WHERE name=?', (MIGRATION,))
    stop = SimpleNamespace(is_set=lambda: False, wait=lambda timeout: True)
    assert backfill(stop) == 250 and not ready()
    clause, args = search_clause('Backfill')
    assert 'paper_search' not in clause
    last = one('SELECT MAX(id) id FROM papers')['id']
    execute('UPDATE papers SET title=?,authors=? WHERE id=?', ('Updated title', dumps(['New Author']), last))
    assert one('SELECT name FROM paper_names WHERE paper_id=?', (last,))['name'] == 'New Author'
    execute('DELETE FROM papers WHERE id=?', (last - 1,))
    assert backfill() == 11 and ready()
    assert one('SELECT COUNT(*) n FROM paper_search_state')['n'] == 262
    clause, args = search_clause('New Author')
    assert rows('SELECT p.id FROM papers p WHERE ' + clause, args) == [{'id': last}]
    execute('UPDATE papers SET authors=? WHERE id=?', (dumps(['Revised Author']), last))
    assert not rows('SELECT p.id FROM papers p WHERE ' + clause, args)
    execute('DELETE FROM papers WHERE id=?', (last,))
    assert not one('SELECT 1 FROM paper_names WHERE paper_id=?', (last,))
    execute("INSERT INTO paper_search(paper_search,rank) VALUES('integrity-check',1)")


def test_external_search_index_upgrades_without_changing_papers(client, papers):
    from app.paper_index import MIGRATION, initialize, backfill, ready
    with connect() as db:
        for name in ('paper_search_insert', 'paper_search_before_update', 'paper_search_update', 'paper_search_delete'):
            db.execute('DROP TRIGGER '+name)
        db.execute('DROP TABLE paper_search')
        db.execute("CREATE VIRTUAL TABLE paper_search USING fts5(title,abstract,authors,content='papers',content_rowid='id',tokenize='trigram',detail='none')")
        db.execute('INSERT INTO paper_search(rowid,title,abstract,authors) SELECT id,title,abstract,authors FROM papers')
        db.execute('DELETE FROM app_migrations WHERE name=?', (MIGRATION,))
        initialize(db)
    assert not ready() and one('SELECT COUNT(*) n FROM paper_search_state')['n']==0
    assert backfill()==len(papers) and ready()
    with connect() as db:
        before=db.execute('SELECT COUNT(*) FROM paper_search_state').fetchone()[0]
        initialize(db)
        assert db.execute('SELECT COUNT(*) FROM paper_search_state').fetchone()[0]==before
    assert [p['id'] for p in rows('SELECT id FROM papers ORDER BY id')]==papers


def test_overview_cache_keeps_pipeline_status_live(client, accounts, monkeypatch):
    from app.api import site
    original = site._compute_overview
    calls = []
    def compute(days):
        calls.append(days)
        return original(days)
    monkeypatch.setattr(site, '_compute_overview', compute)
    execute("INSERT INTO source_status(name) VALUES('fetch_arxiv')")
    auth = headers(accounts[0])
    first = client.get('/api/admin/overview', headers=auth).json()
    execute("UPDATE source_status SET running=1 WHERE name='fetch_arxiv'")
    second = client.get('/api/admin/overview', headers=auth).json()
    assert len(calls) == 1 and first['updated_at'] == second['updated_at']
    assert next(j for j in second['jobs'] if j['name'] == 'fetch_arxiv')['status'] == 'running'
    execute("UPDATE source_status SET running=0,error='已手动停止' WHERE name='fetch_arxiv'")
    assert next(j for j in client.get('/api/admin/overview', headers=auth).json()['jobs'] if j['name'] == 'fetch_arxiv')['status'] == 'stopped'
    key = next(k for k in site._overview_cache if k[0] == str(settings().data_dir.absolute()))
    saved = site._overview_cache[key]
    site._overview_cache[key] = (saved[0] - 11, saved[1])
    client.get('/api/admin/overview', headers=auth)
    assert calls == [7, 7]


def test_home_and_sidebar_share_context_and_feedback_invalidates(client, accounts, papers, monkeypatch):
    from app.pipeline import score
    from app.interest.profile import put_profile
    uid = accounts[0]['user']['id']
    profile = put_profile(uid, '', {'category_selection': {'categories': ['arxiv:cs.AI'], 'topics': {}}}, 'manual')
    calls = []
    original = score._scoring_context
    def context(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(score, '_scoring_context', context)
    first = score.scoring_context(uid)
    assert first is score.scoring_context(uid) and len(calls) == 1
    client.get('/api/feed/today', headers=headers(accounts[0]))
    client.get('/api/recommendations', headers=headers(accounts[0]))
    assert len(calls) == 1
    client.post('/api/interactions', headers=headers(accounts[0]), json={'paper_id': papers[0], 'action': 'like'})
    assert score.scoring_context(uid) is not first and len(calls) == 2
    execute('UPDATE interest_profile SET structured=? WHERE id=?', (dumps({'category_selection': {'categories': ['arxiv:math.CO'], 'topics': {}}}), profile['id']))
    assert score.scoring_context(uid)[4]['categories'] == ['arxiv:math.CO'] and len(calls) == 3


def test_metrics_dirty_dates_include_timezone_and_undo_target_date(client, accounts, papers):
    from app.pipeline.metrics import metrics
    uid = accounts[0]['user']['id']
    event = execute('INSERT INTO interactions(user_id,paper_id,action,view_rule,created_at) VALUES(?,?,?,?,?)',
                    (uid, papers[0], 'like', 0, '2026-01-01T16:30:00+00:00'))
    metrics()
    assert one('SELECT date,shown,liked FROM daily_metrics') == {'date': '2026-01-02', 'shown': 1, 'liked': 1}
    execute('INSERT INTO interactions(user_id,paper_id,action,target_id,created_at) VALUES(?,?,?,?,?)',
            (uid, papers[0], 'undo', event, '2026-01-03T01:00:00+00:00'))
    metrics()
    assert not one("SELECT 1 FROM daily_metrics WHERE date='2026-01-02'")
    assert not one('SELECT 1 FROM metric_dirty')
    # A repeated run must not rewrite unrelated historical totals.
    execute("INSERT INTO daily_metrics(date,user_id,shown) VALUES('2025-01-01',?,123)", (uid,))
    metrics()
    assert one("SELECT shown FROM daily_metrics WHERE date='2025-01-01'")['shown'] == 123
    execute('UPDATE interactions SET action=? WHERE id=?', ('view', event))
    metrics()
    assert not one("SELECT 1 FROM daily_metrics WHERE date='2026-01-02'")


def test_incremental_trend_late_links_quality_dates_and_deletion(client, papers):
    from app.pipeline.trends import trend_stats
    trend_stats()
    topic = one('SELECT topic_id FROM paper_topics WHERE paper_id=?', (papers[0],))['topic_id']
    execute('UPDATE papers SET quality_score=90,published=? WHERE id=?', ('2026-01-01', papers[0]))
    trend_stats()
    assert one('SELECT paper_count,avg_quality FROM topic_daily_stats WHERE date=? AND topic_id=?', ('2026-01-01', topic)) == {'paper_count': 1, 'avg_quality': 90}
    assert not one('SELECT 1 FROM topic_daily_stats WHERE date=? AND topic_id=?', (today(), topic))
    execute('INSERT INTO paper_topics VALUES(?,?,.8)', (papers[1], topic))
    trend_stats()
    assert one('SELECT paper_count FROM topic_daily_stats WHERE date=? AND topic_id=?', (today(), topic))['paper_count'] == 1
    execute('DELETE FROM paper_topics WHERE paper_id=? AND topic_id=?', (papers[1], topic))
    trend_stats()
    assert not one('SELECT 1 FROM topic_daily_stats WHERE date=? AND topic_id=?', (today(), topic))
    execute('DELETE FROM paper_topics WHERE paper_id=?', (papers[0],))
    execute('DELETE FROM papers WHERE id=?', (papers[0],))
    trend_stats()
    assert not one('SELECT 1 FROM topic_daily_stats WHERE date=? AND topic_id=?', ('2026-01-01', topic))
    assert not one('SELECT 1 FROM trend_dirty')


def test_saved_author_movements_use_exact_names_and_track_edits(client, accounts, papers):
    from app.pipeline.trends import trend_data
    auth = headers(accounts[0])
    assert trend_data(accounts[0]['user']['id'])['movements'] == []
    client.post('/api/interactions', headers=auth, json={'paper_id': papers[0], 'action': 'save'})
    execute('UPDATE papers SET authors=? WHERE id=?', (dumps(['Research Author Junior']), papers[2]))
    movements = trend_data(accounts[0]['user']['id'])['movements']
    assert [p['id'] for p in movements] == [papers[0]]
    execute('UPDATE papers SET authors=? WHERE id=?', (dumps(['Research Author']), papers[2]))
    assert {p['id'] for p in trend_data(accounts[0]['user']['id'])['movements']} == {papers[0], papers[2]}


@pytest.mark.asyncio
async def test_community_cache_resume_quota_and_quality_without_models(client, papers, monkeypatch):
    from app.pipeline import community, score
    calls = []
    responses = iter([httpx.Response(200, json={'stargazers_count': 1000}, headers={'etag': 'version1', 'x-ratelimit-remaining': '0'}),
                      httpx.Response(403, json={'message': 'quota'}, headers={'x-ratelimit-remaining': '0'}),
                      httpx.Response(200, json={'stargazers_count': 50})])
    def handler(request):
        calls.append(str(request.url))
        if request.url.host == 'huggingface.co':
            return httpx.Response(200, json=[{'paper': {'id': '2609.00000', 'upvotes': 10}}])
        return next(responses)
    original = httpx.AsyncClient
    monkeypatch.setattr(community.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(settings(), 'github_token', '')
    execute('UPDATE papers SET abstract=? WHERE id=?', ('https://github.com/Shared/Repo.git', papers[0]))
    execute('UPDATE papers SET abstract=? WHERE id=?', ('https://github.com/shared/repo', papers[1]))
    execute('UPDATE papers SET abstract=? WHERE id=?', ('https://github.com/other/repo', papers[2]))
    execute('UPDATE papers SET quality_score=61,base_quality_score=60,author_impact=20,scored=1 WHERE id=?', (papers[0],))
    await community.fetch_community()
    assert len([c for c in calls if 'api.github' in c]) == 1
    first = one('SELECT * FROM papers WHERE id=?', (papers[0],))
    expected = round(60 + .02 * score.community_score(first) + .03 * 20, 2)
    assert first['base_quality_score'] == 60 and first['quality_score'] == expected and first['scored'] == 1
    assert one('SELECT github_stars FROM papers WHERE id=?', (papers[1],))['github_stars'] == 1000
    # Second run skips the cached shared repo; quota response is a normal partial result.
    await community.fetch_community()
    assert len([c for c in calls if 'api.github' in c]) == 2
    revision = one("SELECT value FROM app_settings WHERE name='paper_revision'")['value']
    await community.fetch_community()
    assert one('SELECT github_stars FROM papers WHERE id=?', (papers[2],))['github_stars'] == 50
    await community.fetch_community()
    assert len([c for c in calls if 'api.github' in c]) == 3
    assert one('SELECT scored FROM papers WHERE id=?', (papers[0],))['scored'] == 1
    latest = one("SELECT value FROM app_settings WHERE name='paper_revision'")['value']
    await community.fetch_community()
    assert one("SELECT value FROM app_settings WHERE name='paper_revision'")['value'] == latest and latest != revision


@pytest.mark.asyncio
async def test_agent_references_derive_readable_titles_without_new_model_calls(client, accounts, papers, monkeypatch):
    from app.agent import chat, tools
    from app.agent.references import reference
    paper = {'title': 'Graph Memory: A reliable approach', 'brief_json': dumps({'title_zh': '图记忆：可靠方法'}), 'abs_url': 'https://example.org/paper'}
    assert reference(paper)['short_title'] == '图记忆'
    execute('UPDATE papers SET brief_json=?,abs_url=? WHERE id=?', (paper['brief_json'], paper['abs_url'], papers[0]))
    uid = accounts[0]['user']['id']
    session = execute('INSERT INTO chat_sessions(user_id,title,created_at) VALUES(?,?,?)', (uid, 'Test', now()))
    results = await tools.execute_tool('search_papers', {'query': 'Research Author'}, uid, session, None)
    assert results[0]['short_title'] and results[0]['display_title']
    captured = []
    async def stream(messages, definitions):
        captured.append(messages)
        yield SimpleNamespace(content='参考《图记忆》。', tool_calls=None, reasoning_content=None)
    monkeypatch.setattr(chat.models, 'stream', stream)
    result = [e async for e in chat.chat_events(session, uid, 'Discuss this paper', papers[0])]
    assert '参考《图记忆》' in ''.join(result)
    assert '图记忆' in captured[0][0]['content'] and '数字 ID 仅供工具参数使用' in captured[0][0]['content']


@pytest.mark.parametrize('kind,expected', [('cloud', 3), ('ollama', 1)])
@pytest.mark.asyncio
async def test_brief_cloud_is_bounded_and_local_stays_serial(client, papers, monkeypatch, kind, expected):
    from app.pipeline import tldr
    active = peak = 0
    completed = []
    async def generate(paper):
        nonlocal active, peak
        active += 1; peak = max(peak, active)
        try:
            await asyncio.sleep(.01)
            completed.append(paper['id'])
        finally:
            active -= 1
    monkeypatch.setattr(tldr.models, 'selected', lambda feature: {'kind': kind,'model':'test-model'})
    monkeypatch.setattr(tldr, 'generate_brief', generate)
    await tldr.tldr_gen()
    assert peak == expected and sorted(completed) == sorted(papers)
    started = asyncio.Event()
    async def pending(paper):
        nonlocal active
        active += 1; started.set()
        try: await asyncio.Future()
        finally: active -= 1
    monkeypatch.setattr(tldr, 'generate_brief', pending)
    task = asyncio.create_task(tldr.tldr_gen())
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert active == 0
