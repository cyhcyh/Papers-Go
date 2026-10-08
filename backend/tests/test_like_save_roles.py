import json

import pytest

from app.db import dumps, execute, one, pack, rows
from app.interest.online import update_vector
from app.interest.profile import current, put_profile
from app.interest import reflect as reflection
from .conftest import headers


def feedback(client, account, paper, action):
    response = client.post('/api/interactions', headers=headers(account),
                           json={'paper_id': paper, 'action': action})
    assert response.status_code == 200, response.text
    return response.json()


def scores(client, account):
    papers = client.get('/api/browse?range=all&sort=score', headers=headers(account)).json()['items']
    return {p['id']: p['score'] for p in papers}


def test_only_directional_feedback_changes_interest_vectors(client):
    profile, paper = pack([1., 0., 0., 0.]), pack([0., 1., 0., 0.])
    assert update_vector(profile, paper, 'save') == profile
    assert update_vector(None, paper, 'save') is None
    assert update_vector(profile, paper, 'like') != profile
    assert update_vector(profile, paper, 'skip') != profile


@pytest.mark.parametrize('save_first', [False, True])
def test_like_and_save_are_independent_and_save_does_not_double_feedback(client, accounts, papers, save_first):
    account, other = accounts
    uid = account['user']['id']
    put_profile(uid, '## 核心兴趣\n- [w:0.8] graph research', {}, 'manual', pack([1., 0., 0., 0.]))
    before = current(uid)['embedding']
    before_scores = scores(client, account)
    if save_first:
        feedback(client, account, papers[1], 'save')
        assert current(uid)['embedding'] == before
        assert scores(client, account) == before_scores
    feedback(client, account, papers[1], 'like')
    liked_vector, liked_scores = current(uid)['embedding'], scores(client, account)
    assert liked_vector != before
    if not save_first:
        feedback(client, account, papers[1], 'save')
    assert current(uid)['embedding'] == liked_vector
    assert scores(client, account) == liked_scores
    assert one('SELECT liked,saved,seen FROM user_paper_state WHERE user_id=?', (uid,)) == {'liked': 1, 'saved': 1, 'seen': 1}
    for kind in ('like', 'save'):
        assert [p['id'] for p in client.get('/api/library?type='+kind, headers=headers(account)).json()['items']] == [papers[1]]
        assert client.get('/api/library?type='+kind, headers=headers(other)).json()['items'] == []
    feedback(client, account, papers[1], 'remove_like')
    assert one('SELECT liked,saved FROM user_paper_state WHERE user_id=?', (uid,)) == {'liked': 0, 'saved': 1}
    feedback(client, account, papers[1], 'like')
    feedback(client, account, papers[1], 'remove_save')
    assert one('SELECT liked,saved FROM user_paper_state WHERE user_id=?', (uid,)) == {'liked': 1, 'saved': 0}


@pytest.mark.parametrize('action', ['like', 'save'])
def test_duplicate_add_does_not_record_or_apply_feedback_twice(client, accounts, papers, action):
    account = accounts[0]
    uid = account['user']['id']
    put_profile(uid, 'Graph research', {}, 'manual', pack([1., 0., 0., 0.]))
    feedback(client, account, papers[1], action)
    vector = current(uid)['embedding']
    response = feedback(client, account, papers[1], action)
    assert response['id'] is None and response['recorded'] is False
    assert current(uid)['embedding'] == vector
    assert len(rows('SELECT id FROM interactions WHERE user_id=? AND action=?', (uid, action))) == 1


def test_undo_save_does_not_restore_an_interest_vector_it_never_changed(client, accounts, papers):
    account = accounts[0]
    uid = account['user']['id']
    put_profile(uid, 'Graph research', {}, 'manual', pack([1., 0., 0., 0.]))
    feedback(client, account, papers[1], 'like')
    event = feedback(client, account, papers[1], 'save')
    newer = pack([0., 0., 1., 0.])
    execute('UPDATE interest_profile SET embedding=? WHERE id=?', (newer, current(uid)['id']))
    response = client.post('/api/interactions', headers=headers(account), json={'action': 'undo', 'target_id': event['id']})
    assert response.status_code == 200, response.text
    assert current(uid)['embedding'] == newer
    assert one('SELECT liked,saved FROM user_paper_state WHERE user_id=?', (uid,)) == {'liked': 1, 'saved': 0}


@pytest.mark.asyncio
async def test_saves_alone_do_not_trigger_weekly_interest_inference(client, accounts, papers, monkeypatch):
    account = accounts[0]
    uid = account['user']['id']
    put_profile(uid, 'Graph research', {}, 'manual', pack([1., 0., 0., 0.]))
    before = current(uid)
    feedback(client, account, papers[0], 'save')
    feedback(client, account, papers[0], 'remove_save')
    feedback(client, account, papers[0], 'save')
    async def unexpected(*args, **kwargs):
        raise AssertionError('Saving alone must not call the interest inference model')
    monkeypatch.setattr(reflection.models, 'complete', unexpected)
    await reflection.reflect()
    assert current(uid) == before


@pytest.mark.asyncio
async def test_weekly_reflection_omits_saves_and_cancelled_likes_even_with_custom_prompt(client, accounts, papers, monkeypatch):
    account = accounts[0]
    uid = account['user']['id']
    put_profile(uid, 'Graph research', {}, 'manual', pack([1., 0., 0., 0.]))
    # Overrides cannot accidentally reintroduce saving as a preference signal.
    execute("INSERT INTO app_settings(name,value,updated_at) VALUES('prompts',?,datetime('now'))",
            (dumps({'reflect': 'A custom administrator prompt'}),))
    feedback(client, account, papers[0], 'like')
    feedback(client, account, papers[0], 'save')
    feedback(client, account, papers[1], 'like')
    feedback(client, account, papers[1], 'remove_like')
    feedback(client, account, papers[2], 'save')
    feedback(client, account, papers[2], 'remove_save')
    feedback(client, account, papers[2], 'skip')
    captured = []
    async def complete(feature, messages, **kwargs):
        assert feature == 'reflect'
        captured.extend(json.loads(messages[-1]['content'].split('\n行为：')[1]))
        return {'content': '## 核心兴趣\n- [w:0.8] Graph research'}
    monkeypatch.setattr(reflection.models, 'complete', complete)
    await reflection.reflect()
    assert {(e['action'], e['title']) for e in captured} == {
        ('like', 'Graph Memory for Agents'), ('skip', 'Agent Evaluation Protocol')}
    assert current(uid)['change_reason'] == 'weekly_reflect'
