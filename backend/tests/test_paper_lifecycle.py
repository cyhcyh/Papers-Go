from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app.config import now
from app.db import connect, execute, one, rows, dumps
from app.paper_lifecycle import expiry_after, initialize, MIGRATION
from app.pipeline.expire import purge_batch
from app.pipeline.fetch import upsert_paper
from .conftest import headers


def feedback(client, account, paper, action):
    response = client.post('/api/interactions', headers=headers(account),
                           json={'paper_id': paper, 'action': action})
    assert response.status_code == 200, response.text
    return response.json()


def social(paper):
    return one('SELECT like_count,save_count,expires_at FROM papers WHERE id=?', (paper,))


def test_first_fetch_one_year_and_recrawls_do_not_refresh(client, papers):
    paper = one('SELECT * FROM papers WHERE id=?', (papers[0],))
    assert paper['expires_at'] == expiry_after(paper['created_at'], 365)
    imported = dict(paper, authors=['Research Author'], categories=['cs.AI'], arxiv_version=2)
    assert upsert_paper(imported) is False
    assert social(papers[0])['expires_at'] == paper['expires_at']
    # Publication age has no bearing on the first-ingestion lifetime.
    execute("UPDATE papers SET published='2000-01-01' WHERE id=?", (papers[1],))
    assert purge_batch(now()) == 0


def test_two_year_lifetime_and_shared_five_repeats_per_user(client, accounts, papers, monkeypatch):
    from app.api import content
    base = datetime.now(timezone.utc) + timedelta(hours=1)
    tick = [0]
    monkeypatch.setattr(content, 'now', lambda: (base+timedelta(days=tick[0])).isoformat())
    uid = accounts[0]['user']['id']
    paper = papers[0]
    result = feedback(client, accounts[0], paper, 'like')
    assert result['life_extended'] is True
    assert result['expires_at'] == expiry_after(base.isoformat(), 730)
    tick[0] += 1
    assert feedback(client, accounts[0], paper, 'save')['life_extended'] is True
    for action in ('like', 'save', 'like', 'save', 'like'):
        before = social(paper)['expires_at']
        tick[0] += 1
        assert feedback(client, accounts[0], paper, 'remove_'+action)['expires_at'] == before
        tick[0] += 1
        assert feedback(client, accounts[0], paper, action)['life_extended'] is True
    assert one('SELECT life_renewals FROM user_paper_state WHERE user_id=? AND paper_id=?', (uid,paper))['life_renewals'] == 5
    expires = social(paper)['expires_at']
    for action in ('like','save'):
        tick[0] += 1
        feedback(client, accounts[0], paper, 'remove_'+action)
        tick[0] += 1
        result = feedback(client, accounts[0], paper, action)
        assert result['life_extended'] is False
        assert result['expires_at'] == expires
        assert result['like_count'] == result['save_count'] == 1
    tick[0] += 1
    assert feedback(client, accounts[1], paper, 'like')['life_extended'] is True
    assert social(paper)['like_count'] == 2
    # The limit belongs to this user-paper pair, not to the user or the whole paper.
    assert feedback(client, accounts[0], papers[1], 'like')['life_extended'] is True


def test_first_save_still_refreshes_after_five_like_repeats(client, accounts, papers):
    feedback(client, accounts[0], papers[0], 'like')
    for _ in range(5):
        feedback(client, accounts[0], papers[0], 'remove_like')
        feedback(client, accounts[0], papers[0], 'like')
    assert feedback(client, accounts[0], papers[0], 'save')['life_extended'] is True
    feedback(client, accounts[0], papers[0], 'remove_save')
    assert feedback(client, accounts[0], papers[0], 'save')['life_extended'] is False


def test_concurrent_duplicate_adds_count_once(client, accounts, papers):
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: feedback(client, accounts[0], papers[0], 'like'), range(6)))
    assert sum(r['id'] is not None for r in results) == 1
    assert social(papers[0])['like_count'] == 1
    assert one('SELECT life_renewals FROM user_paper_state WHERE user_id=? AND paper_id=?',
               (accounts[0]['user']['id'],papers[0]))['life_renewals'] == 0


def test_undo_counts_without_rolling_back_lifetime_or_budget(client, accounts, papers):
    result = feedback(client, accounts[0], papers[0], 'like')
    extended = result['expires_at']
    response = client.post('/api/interactions', headers=headers(accounts[0]),
                           json={'action':'undo','target_id':result['id']})
    assert response.status_code == 200, response.text
    assert response.json()['like_count'] == 0
    assert response.json()['expires_at'] == extended
    assert feedback(client, accounts[0], papers[0], 'like')['life_extended'] is True
    assert one('SELECT life_renewals FROM user_paper_state WHERE user_id=? AND paper_id=?',
               (accounts[0]['user']['id'],papers[0]))['life_renewals'] == 1


def test_counts_on_all_list_and_detail_endpoints_without_global_ranking_reset(client, accounts, papers):
    revision = one("SELECT value FROM app_settings WHERE name='paper_revision'")['value']
    feedback(client, accounts[0], papers[0], 'like')
    feedback(client, accounts[0], papers[0], 'save')
    feedback(client, accounts[1], papers[0], 'like')
    assert one("SELECT value FROM app_settings WHERE name='paper_revision'")['value'] == revision
    for path in ('/api/feed/today','/api/browse?range=all','/api/recommendations',
                 '/api/library?type=like','/api/library?type=save'):
        response = client.get(path, headers=headers(accounts[0]) if '/library' in path else {})
        assert response.status_code == 200
        paper = next(p for p in response.json()['items'] if p['id'] == papers[0])
        assert (paper['like_count'],paper['save_count']) == (2,1)
    assert client.get('/api/papers/'+str(papers[0])).json()['like_count'] == 2
    # Ranking-relevant edits still invalidate the existing cache.
    execute('UPDATE papers SET title=? WHERE id=?', ('An updated title',papers[0]))
    assert one("SELECT value FROM app_settings WHERE name='paper_revision'")['value'] != revision


def test_deleting_user_decrements_totals_without_shortening_lifetime(client, accounts, papers):
    feedback(client, accounts[0], papers[0], 'like')
    feedback(client, accounts[1], papers[0], 'like')
    feedback(client, accounts[1], papers[0], 'save')
    expires = social(papers[0])['expires_at']
    response = client.delete('/api/admin/users/'+str(accounts[1]['user']['id']), headers=headers(accounts[0]))
    assert response.status_code == 200, response.text
    assert social(papers[0]) == {'like_count':1,'save_count':0,'expires_at':expires}


def test_legacy_backfill_and_restart_do_not_reset_counts_or_lifetimes(client, accounts, papers):
    uid = accounts[0]['user']['id']
    feedback(client, accounts[0], papers[0], 'like')
    feedback(client, accounts[0], papers[0], 'remove_like')
    latest = feedback(client, accounts[0], papers[0], 'like')['expires_at']
    feedback(client, accounts[0], papers[0], 'save')
    with connect() as db:
        db.execute('DELETE FROM app_migrations WHERE name=?', (MIGRATION,))
        db.execute('UPDATE user_paper_state SET liked_ever=0,saved_ever=0,life_renewals=0')
        db.execute("UPDATE papers SET like_count=0,save_count=0,expires_at=strftime('%Y-%m-%dT%H:%M:%S+00:00',created_at,'+365 days')")
        initialize(db)
    assert social(papers[0]) == {'like_count':1,'save_count':1,'expires_at':latest}
    assert one('SELECT life_renewals FROM user_paper_state WHERE user_id=? AND paper_id=?', (uid,papers[0]))['life_renewals'] == 1
    with connect() as db:
        initialize(db)
    assert social(papers[0])['expires_at'] == latest


def test_purge_only_expired_related_data_preserves_other_papers_and_global_content(client, accounts, papers):
    uid = accounts[0]['user']['id']
    expired, busy, fresh = papers
    feedback(client, accounts[0], expired, 'like')
    feedback(client, accounts[0], expired, 'save')
    feedback(client, accounts[0], fresh, 'save')
    before = one('SELECT * FROM papers WHERE id=?', (fresh,))
    before_state = rows('SELECT * FROM user_paper_state WHERE paper_id=?', (fresh,))
    stamp = now()
    with connect() as db:
        for pid in (expired,busy):
            db.execute("UPDATE papers SET expires_at='2020-01-01T00:00:00+00:00' WHERE id=?", (pid,))
            db.execute('INSERT INTO reading_cards(paper_id,status,created_at) VALUES(?,?,?)', (pid,'ready' if pid==expired else 'pending',stamp))
        db.execute("INSERT INTO reading_jobs(paper_id,level,kind,status,queued_at) VALUES(?,'L2','cloud','queued',?)", (busy,stamp))
        db.execute('INSERT INTO paper_sources(source_id,paper_id) VALUES(?,?)', ('expired-source',expired))
        db.execute("INSERT INTO notifications(user_id,type,title,paper_id,created_at) VALUES(?,'watch','Expired',?,?)", (uid,expired,stamp))
        db.execute("INSERT INTO fulltext_cache(paper_id,cached_at,accessed_at,bytes) VALUES(?,?,?,0)", (expired,stamp,stamp))
        db.execute("INSERT INTO paper_classifications(paper_id,status,updated_at) VALUES(?,'classified',?)", (expired,stamp))
        db.execute('INSERT INTO topic_pending_papers VALUES(?,?,.8,?)', (expired,15,stamp))
        run = db.execute("INSERT INTO pipeline_redo_runs(name,options,created_at,updated_at) VALUES('classify','{}',?,?)", (stamp,stamp)).lastrowid
        db.execute("INSERT INTO pipeline_redo_items(run_id,paper_id,component) VALUES(?,?,'classification')", (run,expired))
        db.execute("INSERT INTO direction_trends(audience,profile_version,summary,items_json) VALUES('guest',0,'Keep this trend','[]')")
        db.execute("INSERT INTO trend_reference_cache(scope_key,papers,fetched_at) VALUES('unrelated','[]',?)", (stamp,))
        session = db.execute("INSERT INTO chat_sessions(user_id,title,created_at) VALUES(?,'Keep this chat',?)", (uid,stamp)).lastrowid
        db.execute("INSERT INTO chat_messages(session_id,role,content,created_at) VALUES(?,'assistant','Historical conversation',?)", (session,stamp))
    assert purge_batch(stamp) == 1
    assert one('SELECT id FROM papers WHERE id=?', (expired,)) is None
    for table in ('user_paper_state','interactions','notifications','reading_cards','papers_vec',
                  'paper_topics','paper_sources','paper_categories','paper_classifications',
                  'fulltext_cache','topic_pending_papers','pipeline_redo_items'):
        assert one('SELECT COUNT(*) n FROM '+table+' WHERE paper_id=?', (expired,))['n'] == 0
    assert one('SELECT * FROM papers WHERE id=?', (fresh,)) == before
    assert rows('SELECT * FROM user_paper_state WHERE paper_id=?', (fresh,)) == before_state
    assert one('SELECT id FROM papers WHERE id=?', (busy,)) is not None
    assert one("SELECT summary FROM direction_trends WHERE audience='guest'")['summary'] == 'Keep this trend'
    assert one("SELECT papers FROM trend_reference_cache WHERE scope_key='unrelated'")['papers'] == '[]'
    assert one('SELECT content FROM chat_messages WHERE session_id=?', (session,))['content'] == 'Historical conversation'
    assert not upsert_paper({'arxiv_id':'2609.00000','title':'Graph Memory for Agents'})
    assert not upsert_paper({'arxiv_id':'new-venue-id','source_id':'expired-source','title':'Graph Memory for Agents'})
    assert one('SELECT id FROM papers WHERE arxiv_id=?', ('2609.00000',)) is None
    with connect() as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        plan = [r['detail'] for r in db.execute('EXPLAIN QUERY PLAN SELECT id FROM papers WHERE expires_at<=? ORDER BY expires_at,id LIMIT 50', (stamp,))]
    assert any('idx_papers_expiry' in detail for detail in plan)
    # Foreign-key cascades must not scan all historical redo entries per paper.
    with connect() as db:
        plan = [r['detail'] for r in db.execute(
            'EXPLAIN QUERY PLAN SELECT id FROM pipeline_redo_items WHERE paper_id=?', (expired,))]
    assert any('idx_redo_paper' in detail for detail in plan)


def test_live_fulltext_pin_defers_deletion_and_positive_feedback_can_rescue(client, accounts, papers):
    past = '2020-01-01T00:00:00+00:00'
    execute('UPDATE papers SET expires_at=? WHERE id=?', (past,papers[0]))
    execute('INSERT INTO fulltext_cache_pins VALUES(?,?,?)', ('active-reading',papers[0],expiry_after(now(),1)))
    assert purge_batch(now()) == 0
    execute("DELETE FROM fulltext_cache_pins WHERE token='active-reading'")
    feedback(client, accounts[0], papers[0], 'like')
    assert purge_batch(now()) == 0


@pytest.mark.asyncio
async def test_purge_batch_progress_and_noop_are_bounded(client, papers, monkeypatch):
    from app.pipeline import expire
    monkeypatch.setattr(expire, 'BATCH_SIZE', 1)
    execute("UPDATE papers SET expires_at='2020-01-01T00:00:00+00:00' WHERE id=?", (papers[0],))
    assert await expire.expire_papers() == 1
    import json
    progress = json.loads(one("SELECT progress FROM source_status WHERE name='paper_expiry'")['progress'])
    assert progress['completed'] == progress['total'] == 1
    assert await expire.expire_papers() == 0


@pytest.mark.asyncio
async def test_expiry_job_schedule_switch_and_permissions(client, accounts, papers):
    from app import scheduler
    from app.pipeline_control import set_job_enabled
    timer = scheduler.make_scheduler()
    job = timer.get_job('paper_expiry')
    assert str(job.trigger.fields[-3]) == '7'
    assert str(job.trigger.fields[-2]) == '15'
    sources = client.get('/api/admin/sources', headers=headers(accounts[0])).json()
    assert any(source['name']=='paper_expiry' for source in sources)
    assert client.post('/api/admin/jobs/paper_expiry',headers=headers(accounts[1])).status_code == 403
    set_job_enabled('paper_expiry',False)
    execute("UPDATE papers SET expires_at='2020-01-01T00:00:00+00:00' WHERE id=?", (papers[0],))
    await scheduler.run_job('paper_expiry')
    assert one('SELECT id FROM papers WHERE id=?',(papers[0],)) is not None
    assert client.post('/api/admin/jobs/paper_expiry',headers=headers(accounts[0])).status_code == 409
