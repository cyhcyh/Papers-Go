import json
import pytest
from datetime import date, timedelta
from app.config import now, today
from app.db import execute, one, rows
from .conftest import headers, finish_interest_updates


def test_guest_feed_stays_empty_until_administrator_selects_sources(client,accounts,papers):
    from app.source_catalog import invalidate
    execute('UPDATE source_categories SET guest_default=0');invalidate()
    assert not client.get('/api/feed/today').json()['items']
    assert not client.get('/api/recommendations').json()['items']
    assert one('SELECT COUNT(*) AS n FROM papers')['n']==len(papers)
    response=client.patch('/api/admin/source-categories/batch',headers=headers(accounts[0]),
                          json={'keys':['arxiv:cs.AI'],'guest_default':True})
    assert response.status_code==200
    assert papers[0] in {p['id'] for p in client.get('/api/feed/today').json()['items']}


def test_guest_can_browse_without_reading_or_writing_account_data(client, accounts, papers):
    auth = headers(accounts[0])
    client.post('/api/profile/init', headers=auth, json={'description':'private research direction', 'exclusions':['Graph']})
    finish_interest_updates(client)
    client.post('/api/interactions', headers=auth, json={'paper_id':papers[0],'action':'save'})
    before = one('SELECT COUNT(*) AS n FROM interactions')['n']
    assert client.get('/api/categories').status_code == 200
    assert client.get('/api/topics').status_code == 200
    for path in ('/api/feed/today','/api/browse?range=all','/api/recommendations'):
        response = client.get(path)
        assert response.status_code == 200
        public = response.json()['items']
        assert papers[0] in {p['id'] for p in public}
        assert all(not p['liked'] and not p['saved'] for p in public)
        assert all('embedding' not in p and 'fulltext' not in p for p in public)
        assert 'private research direction' not in response.text
    assert client.get('/api/papers/'+str(papers[0])).status_code == 200
    assert one('SELECT COUNT(*) AS n FROM interactions')['n'] == before
    assert one('SELECT COUNT(*) AS n FROM users')['n'] == 2


def test_private_actions_still_require_login_and_invalid_tokens_are_rejected(client, papers):
    for path in ('/api/profile','/api/library','/api/notifications','/api/stats/today','/api/trends','/api/admin/topics',f'/api/papers/{papers[0]}/card'):
        assert client.get(path).status_code == 401
    assert client.post('/api/interactions', json={'paper_id':papers[0],'action':'save'}).status_code == 401
    assert client.post('/api/profile/init', json={'description':'research'}).status_code == 401
    finish_interest_updates(client)
    assert client.get('/api/feed/today', headers={'Authorization':'Bearer invalid-token'}).status_code == 401
    assert one('SELECT COUNT(*) AS n FROM interactions')['n'] == 0
    assert one('SELECT COUNT(*) AS n FROM reading_cards')['n'] == 0


def test_guest_feed_batches_twenty_and_keeps_recent_papers_between_syncs(client, papers):
    yesterday = (date.fromisoformat(today())-timedelta(days=1)).isoformat()
    execute('UPDATE papers SET ingested_date=?', (yesterday,))
    for i in range(42):
        execute('INSERT INTO papers(arxiv_id,title,abstract,primary_category,published,created_at,ingested_date) VALUES(?,?,?,?,?,?,?)',
                (f'batch:{i}',f'Paper {i}','Abstract','math.CO' if i%4==0 else 'cs.AI',today(),now(),yesterday))
    first = client.get('/api/feed/today').json()
    assert len(first['items']) == 20 and first['total'] == 45
    assert {'cs.AI','math.CO'} <= {p['primary_category'] for p in first['items']}
    ids = {p['id'] for p in first['items']}
    second = client.get('/api/feed/today?exclude='+','.join(map(str,ids))).json()
    assert len(second['items']) == 20 and second['total'] == 25
    assert not ids & {p['id'] for p in second['items']}
    ids.update(p['id'] for p in second['items'])
    third = client.get('/api/feed/today?exclude='+','.join(map(str,ids))).json()
    assert len(third['items']) == 5 and third['total'] == 5


def test_signed_in_feed_survives_midnight_with_scope_and_user_isolation(client, accounts, papers, monkeypatch):
    from app.interest.profile import put_profile
    from app.pipeline import score
    a, b = accounts
    selection = {'categories': ['arxiv:cs.AI'], 'topics': {}}
    put_profile(a['user']['id'], '', {'category_selection': selection}, 'manual')
    execute('UPDATE papers SET primary_category=? WHERE id=?', ('math.CO', papers[1]))
    viewed = client.post('/api/interactions', headers=headers(a), json={'paper_id': papers[0], 'action': 'view', 'dwell_ms': 5000})
    assert viewed.status_code == 200
    assert [p['id'] for p in client.get('/api/feed/today', headers=headers(a)).json()['items']] == [papers[2]]
    tomorrow = (date.fromisoformat(today()) + timedelta(days=1)).isoformat()
    monkeypatch.setattr(score, 'today', lambda: tomorrow)
    result = client.get('/api/feed/today', headers=headers(a)).json()
    assert result['total'] == 1 and [p['id'] for p in result['items']] == [papers[2]]
    assert {p['id'] for p in client.get('/api/feed/today', headers=headers(b)).json()['items']} == set(papers)
    assert client.get(f'/api/feed/today?exclude={papers[2]}', headers=headers(a)).json()['items'] == []


def test_feed_includes_recent_publications_and_recently_collected_older_papers(client, accounts, papers):
    end = date.fromisoformat(today())
    yesterday, older = (end - timedelta(days=1)).isoformat(), (end - timedelta(days=30)).isoformat()
    execute('UPDATE papers SET published=?,ingested_date=? WHERE id=?', (older, yesterday, papers[0]))
    execute('UPDATE papers SET published=?,ingested_date=? WHERE id=?', (yesterday, older, papers[1]))
    execute('UPDATE papers SET published=?,ingested_date=? WHERE id=?', (older, older, papers[2]))
    for auth in ({}, headers(accounts[0])):
        response = client.get('/api/feed/today', headers=auth).json()
        assert response['total'] == 3 and {p['id'] for p in response['items']} == set(papers)
        assert {p['id'] for p in response['items'][:2]} == {papers[0], papers[1]}


def standard_body(label,keys,**extra):
    from app.standard_topics import catalog
    entry=next(e for e in catalog().values() if e['system']=='Computer Science' and e['label']==label)
    return {'name_zh':'标准主题','name_en':entry['label'],'standard_key':entry['key'],'category_keys':keys,**extra}


def test_shared_flat_topic_source_membership_survives_rename(client, accounts):
    auth = headers(accounts[0])
    body = standard_body('Explainable AI',['arxiv:cs.LG','venue:ICML'])
    response = client.post('/api/admin/topics', headers=auth, json=body)
    assert response.status_code == 200
    ident = response.json()['id']
    assert client.post('/api/admin/topics', headers=auth, json={**body,'parent_id':ident}).status_code == 400
    assert client.patch(f'/api/admin/topics/{ident}', headers=auth, json={**body,'parent_id':2}).status_code == 400
    assert client.post('/api/admin/topics', headers=auth, json={'name_zh':'无归属','name_en':'No source'}).status_code == 400
    assert client.patch(f'/api/admin/topics/{ident}', headers=auth, json={**body,'name_en':'Renamed shared topic'}).status_code == 200
    body.update(name_zh='神经网络（改名）')
    assert client.patch(f'/api/admin/topics/{ident}', headers=auth, json=body).status_code == 200
    catalog = {c['key']:c for c in client.get('/api/categories').json()}
    for key in body['category_keys']:
        scoped = {t['id']:t for t in catalog[key]['topics']}
        assert scoped[ident]['name_zh'] == body['name_zh']
        assert scoped[ident]['parent_id'] is None
    assert client.patch(f'/api/admin/topics/{ident}', headers=auth, json={**body,'category_keys':['arxiv:cs.DM']}).status_code == 400
    assert client.patch(f'/api/admin/topics/{ident}', headers=auth, json={**body,'category_keys':['arxiv:math.CO']}).status_code == 200
    assert client.patch('/api/admin/topics/2', headers=auth, json={'name_zh':'任意改名','name_en':'Renamed agents'}).status_code == 400


def test_admin_tree_includes_proposals_and_merge_preserves_source_membership(client, accounts, papers):
    auth = headers(accounts[0])
    source = standard_body('Zero-Shot Learning',['arxiv:cs.AI'],status='proposed')
    a = client.post('/api/admin/topics', headers=auth, json=source).json()['id']
    b = client.post('/api/admin/topics', headers=auth, json=standard_body('Active Learning',['venue:ICLR'])).json()['id']
    assert all(t['id']!=a for c in client.get('/api/categories').json() for t in c['topics'])
    assert any(t['id']==a and t['status']=='proposed' for c in client.get('/api/admin/categories',headers=auth).json() for t in c['topics'])
    assert client.get('/api/admin/categories').status_code == 401
    execute('INSERT INTO topic_pending_papers VALUES(?,?,?,?)',(papers[0],a,.9,now()))
    assert client.post(f'/api/admin/topics/{a}/merge', headers=auth, json={'target_id':b}).status_code == 200
    assert set(json.loads(one('SELECT category_keys FROM topics WHERE id=?',(b,))['category_keys'])) == {'arxiv:cs.AI','venue:ICLR'}
    assert [t['topic_id'] for t in rows('SELECT * FROM paper_topics WHERE paper_id=?',(papers[0],))]==[b]
    assert not one('SELECT * FROM topic_pending_papers WHERE paper_id=?',(papers[0],))
    catalog = {c['key']:c for c in client.get('/api/categories').json()}
    for key in ('arxiv:cs.AI','venue:ICLR'):
        assert b in {t['id'] for t in catalog[key]['topics']}
