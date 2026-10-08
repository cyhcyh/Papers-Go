import json
from collections import Counter

import pytest

from app.config import now, settings, today
from app.db import connect, dumps, execute
from app.interest.profile import put_profile
from .conftest import headers, finish_interest_updates


AI = 'arxiv:cs.AI'
MATH = 'arxiv:math.CO'


def seed(count_ai=80, count_math=80):
    with connect() as db:
        for code, count, quality in [('cs.AI', count_ai, 90), ('math.CO', count_math, 20)]:
            db.executemany('''INSERT INTO papers(title,abstract,authors,primary_category,
                published,ingested_date,created_at,quality_score) VALUES(?,?,?,?,?,?,?,?)''',
                [(f'{code} paper {i}', 'Research results', '[]', code, today(), today(), now(), quality)
                 for i in range(count)])


def profile(account, weights=None):
    selection = {'categories': [AI, MATH], 'topics': {}}
    if weights is not None:
        selection['weights'] = weights
    put_profile(account['user']['id'], '', {'category_selection': selection}, 'test')


def labels(items):
    return Counter(p['source_label'].split(' · ')[-1] for p in items)


def test_default_equal_mix_survives_large_global_candidate_cutoff(client, accounts, monkeypatch):
    monkeypatch.setattr(settings(), 'recommendation_candidates', 100)
    seed(600, 40)
    profile(accounts[0])  # Existing profiles have no weights field.
    items = client.get('/api/feed/today', headers=headers(accounts[0])).json()['items']
    assert labels(items) == {'cs.AI': 10, 'math.CO': 10}
    assert all(items[i]['primary_category'] != items[i+1]['primary_category'] for i in range(19))
    for code in ('cs.AI', 'math.CO'):
        scores = [p['score'] for p in items if p['primary_category'] == code]
        assert scores == sorted(scores, reverse=True)
    recommendations = client.get('/api/recommendations', headers=headers(accounts[0])).json()['items']
    assert set(labels(recommendations)) == {'cs.AI', 'math.CO'}


def test_weights_carry_across_loading_seen_feedback_and_cache_rebuild(client, accounts):
    import app.pipeline.score as score
    seed()
    profile(accounts[0], {AI: .7, MATH: 1})
    auth = headers(accounts[0])
    collected = []
    for batch in range(3):
        exclude = ','.join(str(p['id']) for p in collected)
        page = client.get('/api/feed/today?exclude='+exclude, headers=auth).json()['items']
        assert len(page) == 20
        assert not {p['id'] for p in collected} & {p['id'] for p in page}
        collected.extend(page)
        assert abs(labels(collected)['math.CO'] - len(collected)/1.7) < 1
        # A five-second view filters cached results; a deliberately discarded
        # cache still carries the display counts supplied by the client.
        assert client.post('/api/interactions', headers=auth,
                           json={'paper_id': page[0]['id'], 'action': 'view', 'dwell_ms': 5000}).status_code == 200
        if batch == 1:
            score._ranking_cache.clear()
    assert labels(collected[:20]) == {'cs.AI': 8, 'math.CO': 12}


def test_shortage_backfills_and_cross_listed_papers_are_not_duplicated(client, accounts):
    seed(30, 2)
    execute('UPDATE papers SET categories=? WHERE id=(SELECT MIN(id) FROM papers)', (dumps(['cs.AI', 'math.CO']),))
    profile(accounts[0])
    auth = headers(accounts[0])
    first = client.get('/api/feed/today', headers=auth).json()['items']
    assert len(first) == 20 and labels(first) == {'cs.AI': 18, 'math.CO': 2}
    second = client.get('/api/feed/today?exclude='+','.join(str(p['id']) for p in first), headers=auth).json()['items']
    all_ids = [p['id'] for p in first+second]
    assert len(all_ids) == len(set(all_ids)) == 32


def test_weighted_partial_topics_and_empty_category(client, accounts):
    seed(24, 24)
    topic = execute('INSERT INTO topics(name_zh,name_en,category_keys,created_at) VALUES(?,?,?,?)',
                    ('所选主题', 'Selected topic', dumps([AI]), now()))
    with connect() as db:
        db.execute('INSERT INTO paper_topics SELECT id,?,.9 FROM papers WHERE primary_category=? AND id<=8', (topic, 'cs.AI'))
    selection = {'categories': [MATH, 'venue:ICML'], 'topics': {AI: [topic]},
                 'weights': {AI: .7, MATH: .7, 'venue:ICML': 1}}
    put_profile(accounts[0]['user']['id'], '', {'category_selection': selection}, 'test')
    items = client.get('/api/feed/today', headers=headers(accounts[0])).json()['items']
    assert labels(items) == {'cs.AI': 8, 'math.CO': 12}
    assert all(any(t['id'] == topic for t in p['topics']) for p in items if p['primary_category'] == 'cs.AI')


def test_weight_save_reload_rollback_and_changed_ratio(client, accounts):
    seed()
    auth = headers(accounts[0])
    selection = {'categories': [AI, MATH], 'topics': {}, 'weights': {AI: 1, MATH: .4}}
    assert client.post('/api/profile/init', headers=auth, json={'category_selection': selection}).status_code == 200
    finish_interest_updates(client)
    loaded = client.get('/api/profile', headers=auth).json()['current']
    assert loaded['structured']['category_selection']['weights'] == selection['weights']
    before = client.get('/api/feed/today', headers=auth).json()['items']
    assert labels(before) == {'cs.AI': 14, 'math.CO': 6}
    selection['weights'] = {AI: .4, MATH: 1}
    assert client.put('/api/profile', headers=auth, json={'form': {}, 'category_selection': selection}).status_code == 200
    finish_interest_updates(client)
    assert labels(client.get('/api/feed/today', headers=auth).json()['items']) == {'cs.AI': 6, 'math.CO': 14}
    assert client.post('/api/profile/rollback', headers=auth, json={'version': 1}).status_code == 200
    finish_interest_updates(client)
    assert client.get('/api/profile', headers=auth).json()['current']['structured']['category_selection']['weights'] == {AI: 1, MATH: .4}
    assert client.get('/api/profile', headers=headers(accounts[1])).json()['current'] is None


@pytest.mark.parametrize('weights,status', [({AI: 0}, 422), ({AI: -1}, 422), ({AI: 1.1}, 422), ({MATH: 1}, 400), ({'arxiv:cs.DM': 1}, 400)])
def test_reject_invalid_or_unselected_category_weight(client, accounts, weights, status):
    assert client.post('/api/profile/init', headers=headers(accounts[0]),
                       json={'category_selection': {'categories': [AI], 'weights': weights}}).status_code == status
    finish_interest_updates(client)


def test_keyword_scoring_uses_interest_weight_without_vectors(client, accounts):
    from app.pipeline.score import scored_papers
    from app.db import rows
    seed(1, 0)
    uid = accounts[0]['user']['id']
    put_profile(uid, '## 核心兴趣\n- [w:0.4] Research', {}, 'test')
    low = scored_papers(uid, rows('SELECT * FROM papers'))[0]['score']
    put_profile(uid, '## 核心兴趣\n- [w:1] Research', {}, 'test')
    high = scored_papers(uid, rows('SELECT * FROM papers'))[0]['score']
    assert high > low
    put_profile(uid, '## 核心兴趣\n- [w:1] Research\n- [w:0.4] Research', {}, 'test')
    assert scored_papers(uid, rows('SELECT * FROM papers'))[0]['score'] == high


def test_offset_matches_exclusion_paging_for_unchanged_feed(client, accounts):
    seed()
    profile(accounts[0], {AI: .7, MATH: 1})
    auth = headers(accounts[0])
    first = client.get('/api/feed/today?limit=20', headers=auth).json()['items']
    offset = client.get('/api/feed/today?offset=20&limit=20', headers=auth).json()['items']
    excluded = client.get('/api/feed/today?exclude='+','.join(str(p['id']) for p in first), headers=auth).json()['items']
    assert [p['id'] for p in offset] == [p['id'] for p in excluded]
