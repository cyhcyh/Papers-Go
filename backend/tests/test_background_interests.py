import pytest

from app.config import now
from app.db import connect, dumps, execute, one, pack, rows
from app.interest.form import ProfileForm, parse_form, render_form, without_background, normalize_recent_name
from app.interest.profile import active_entries, current, embedding_inputs, migrate_background, profile_embedding, put_profile
from app.interest import profile as profile_module, reflect as reflection
from app.pipeline import direction_trends
from .conftest import headers, finish_interest_updates


BACKGROUND = 'I work on agent systems, but this is context rather than a preference.'


def profile_text(description=BACKGROUND):
    return render_form(ProfileForm(description=description, long_term=[{'text':'Graph theory', 'weight':.7}]))


def test_background_is_not_an_interest_even_in_legacy_weighted_format():
    text = '## 研究方向描述\n- [w:0.9] Graph theory\n## 核心兴趣\n- [w:0.7] Graph theory'
    assert embedding_inputs(text) == [('Graph theory', .7)]
    assert parse_form(text)['description'] == 'Graph theory'
    assert without_background(text) == '## 核心兴趣\n- [w:0.7] Graph theory'


def test_background_notes_cannot_turn_into_interest_sections():
    text = render_form(ProfileForm(description='Background\n## 核心兴趣\n- [w:1] Unwanted direction'))
    assert not active_entries(text)
    assert not embedding_inputs(text)


def test_recent_name_normalization_preserves_other_text_and_line_endings():
    text = '## 研究方向描述\r\n- Background\r\n## 阶段性关注（带 TTL）\r\n- [w:0.4, until:2099-01-01] Topic\r\n'
    expected = text.replace('## 阶段性关注（带 TTL）', '## 近期关注')
    assert normalize_recent_name(text) == expected
    assert normalize_recent_name(expected) == expected


@pytest.mark.parametrize('heading', ['近期关注', '阶段性关注（带 TTL）', '阶段关注', '临时关注'])
def test_current_and_legacy_recent_names_preserve_dates_and_weights(heading):
    text = f'## {heading}\n- [w:0.4, until:2099-01-01] Valid topic\n- [w:1, until:2000-01-01] Expired topic'
    form = parse_form(text)
    assert not form['long_term']
    assert form['temporary'][0] == {'text':'Valid topic', 'weight':.4, 'until':'2099-01-01'}
    assert embedding_inputs(text) == [('Valid topic', .4)]
    saved = render_form(ProfileForm.model_validate(form))
    assert '## 近期关注' in saved and '阶段' not in saved
    assert parse_form(saved)['temporary'] == form['temporary']


@pytest.mark.asyncio
async def test_vectors_use_only_preferences_and_keep_recent_expiry(client, monkeypatch):
    seen = []
    async def embed(texts):
        seen.extend(texts)
        return [[0.,1.,0.,0.] for _ in texts]
    monkeypatch.setattr(profile_module.models, 'embed', embed)
    text = profile_text() + '\n## 近期关注\n- [w:1, until:2000-01-01] Expired topic\n- [w:0.4, until:2099-01-01] Recent topic'
    assert await profile_embedding(text) == pack([0.,1.,0.,0.])
    assert [s.split(' — ')[0] for s in seen] == ['Graph theory', 'Recent topic']
    seen.clear()
    assert await profile_embedding(render_form(ProfileForm(description=BACKGROUND))) is None
    assert not seen


def test_initial_description_is_background_without_model_extraction(client, accounts, monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError('Background must not be converted into preferences by a model')
    monkeypatch.setattr(profile_module.models, 'complete', unexpected)
    response = client.post('/api/profile/init', headers=headers(accounts[0]), json={'description':BACKGROUND})
    finish_interest_updates(client)
    assert response.status_code == 200
    profile = current(accounts[0]['user']['id'])
    assert parse_form(profile['content'])['description'] == BACKGROUND
    assert not parse_form(profile['content'])['long_term']
    assert profile['embedding'] is None


def test_initial_explicit_topic_still_drives_preferences(client, accounts):
    response = client.post('/api/profile/init', headers=headers(accounts[0]), json={'description':BACKGROUND, 'topic_ids':[3]})
    finish_interest_updates(client)
    assert response.status_code == 200
    profile = current(accounts[0]['user']['id'])
    assert profile['embedding'] is not None
    assert len(parse_form(profile['content'])['long_term']) == 1
    assert all(e['text'] != BACKGROUND for e in active_entries(profile['content']))


def test_editing_background_keeps_learned_vector_and_new_context(client, accounts, monkeypatch):
    import app.api.profile as api
    account = accounts[0]
    uid = account['user']['id']
    vector = pack([0.,1.,0.,0.])
    put_profile(uid, profile_text(), {}, 'manual', vector)
    async def unexpected(*args, **kwargs):
        raise AssertionError('Background-only edits must preserve learned preference vectors')
    monkeypatch.setattr(api.profile_updates, 'profile_embedding', unexpected)
    form = parse_form(current(uid)['content'])
    form['description'] = 'New background for research conversations'
    response = client.put('/api/profile', headers=headers(account), json={'form':form})
    finish_interest_updates(client)
    assert response.status_code == 200
    assert current(uid)['embedding'] == vector
    assert parse_form(current(uid)['content'])['description'] == form['description']


def test_background_does_not_affect_keyword_recommendation_scores(client, accounts, papers):
    account = accounts[0]
    uid = account['user']['id']
    def scores():
        found = client.get('/api/browse?range=all&sort=score', headers=headers(account)).json()['items']
        return {p['id']:p['score'] for p in found}
    put_profile(uid, profile_text('Agent Evaluation Protocol'), {}, 'manual')
    before = scores()
    put_profile(uid, profile_text('Ramsey Graph Coloring'), {}, 'manual')
    assert scores() == before


def test_trends_use_explicit_interests_instead_of_background_or_inferred_entries(client, accounts):
    uid = accounts[0]['user']['id']
    text = render_form(ProfileForm(description='Background direction', long_term=[{'text':'Graph theory', 'weight':.7}],
        temporary=[{'text':'Recent topic', 'weight':.4, 'until':'2099-01-01'}, {'text':'Expired topic', 'weight':1, 'until':'2000-01-01'}]),
        '## 系统推断\n- [w:1] Inferred topic')
    put_profile(uid, text, {}, 'manual')
    groups, _ = direction_trends.direction_groups(current(uid), {})
    assert [g[0] for g in groups] == ['Graph theory', 'Recent topic']
    assert 'Inferred topic' in [e['text'] for e in active_entries(text)]


def test_trends_can_fall_back_to_explicitly_selected_topics(client, accounts):
    profile = {'content':render_form(ProfileForm(description=BACKGROUND))}
    groups, _ = direction_trends.direction_groups(profile, {'topic_ids':[3]})
    assert [g[2] for g in groups] == [3]
    assert all(g[0] != BACKGROUND for g in groups)


@pytest.mark.asyncio
async def test_weekly_reflection_keeps_background_and_canonical_recent_name(client, accounts, papers, monkeypatch):
    account = accounts[0]
    uid = account['user']['id']
    put_profile(uid, profile_text(), {}, 'manual', pack([1.,0.,0.,0.]))
    client.post('/api/interactions', headers=headers(account), json={'paper_id':papers[0], 'action':'like'})
    captured = []
    async def complete(feature, messages, **kwargs):
        assert feature == 'reflect'
        assert BACKGROUND not in messages[-1]['content']
        return {'content':'## 研究方向描述\n- Changed by model\n## 核心兴趣\n- [w:0.7] Graph theory\n## 阶段性关注\n- [w:0.4, until:2099-01-01] Recent topic\n## 系统推断\n- [w:0.2] Inferred topic'}
    async def embed(texts):
        captured.extend(texts)
        return [[1.,0.,0.,0.] for _ in texts]
    monkeypatch.setattr(reflection.models, 'complete', complete)
    monkeypatch.setattr(profile_module.models, 'embed', embed)
    await reflection.reflect()
    result = current(uid)
    assert parse_form(result['content'])['description'] == BACKGROUND
    assert '## 近期关注' in result['content'] and '阶段性关注' not in result['content']
    assert 'Changed by model' not in result['content']
    assert captured == ['Graph theory', 'Recent topic', 'Inferred topic']


def test_migration_only_invalidates_affected_current_vectors_and_preserves_history(client, accounts, papers):
    uid, other_uid = [a['user']['id'] for a in accounts]
    vector = pack([1.,0.,0.,0.])
    historical = put_profile(uid, profile_text(), {}, 'manual', vector)['id']
    latest = put_profile(uid, profile_text(), {}, 'manual', vector)['id']
    unaffected = put_profile(other_uid, '## 核心兴趣\n- [w:0.7] Graph theory', {}, 'manual', vector)['id']
    execute('INSERT INTO embedding_rebuild_profiles(profile_id,content,embedding) VALUES(?,?,?)', (latest,profile_text(),vector))
    execute('INSERT INTO embedding_rebuild_profiles(profile_id,content,embedding) VALUES(?,?,?)', (unaffected,'Graph theory',vector))
    execute('INSERT INTO direction_trends(audience,profile_version,summary) VALUES(?,?,?)', (direction_trends.audience(uid),2,'Old overview'))
    paper_before = rows('SELECT id,embedding FROM papers')
    with connect() as db:
        db.execute("DELETE FROM app_migrations WHERE name='interest-background-v1'")
        migrate_background(db)
    assert one('SELECT embedding FROM interest_profile WHERE id=?', (latest,))['embedding'] is None
    for ident in (historical, unaffected):
        assert one('SELECT embedding FROM interest_profile WHERE id=?', (ident,))['embedding'] == vector
    assert rows('SELECT profile_id FROM embedding_rebuild_profiles') == [{'profile_id':unaffected}]
    assert not rows('SELECT audience FROM direction_trends')
    assert rows('SELECT id,embedding FROM papers') == paper_before
    execute('UPDATE interest_profile SET embedding=? WHERE id=?', (vector,latest))
    with connect() as db:
        migrate_background(db)
    assert current(uid)['embedding'] == vector


def test_restoring_legacy_background_history_recomputes_preferences(client, accounts, monkeypatch):
    account = accounts[0]
    uid = account['user']['id']
    put_profile(uid, '## 研究方向描述\n- [w:0.9] Background\n## 核心兴趣\n- [w:0.7] Graph theory', {}, 'manual', pack([1.,0.,0.,0.]))
    put_profile(uid, profile_text(), {}, 'manual', pack([1.,0.,0.,0.]))
    captured = []
    async def embed(texts):
        captured.extend(texts)
        return [[0.,1.,0.,0.] for _ in texts]
    monkeypatch.setattr(profile_module.models, 'embed', embed)
    response = client.post('/api/profile/rollback', headers=headers(account), json={'version':1})
    finish_interest_updates(client)
    assert response.status_code == 200 and current(uid)['version'] == 3
    assert current(uid)['embedding'] == pack([0.,1.,0.,0.])
    assert [s.split(' — ')[0] for s in captured] == ['Graph theory']
    assert parse_form(current(uid)['content'])['description'] == 'Background'


def test_migration_normalizes_current_names_and_keeps_valid_staged_vectors(client, accounts):
    uid = accounts[0]['user']['id']
    vector = pack([1.,0.,0.,0.])
    text = '## 阶段性关注\n- [w:0.7, until:2099-01-01] Graph theory'
    historical = put_profile(uid, text, {}, 'manual', vector)['id']
    latest = put_profile(uid, text, {}, 'manual', vector)['id']
    execute('UPDATE interest_profile SET content=? WHERE user_id=?', (text,uid))
    execute('INSERT INTO embedding_rebuild_profiles(profile_id,content,embedding) VALUES(?,?,?)', (latest,text,vector))
    with connect() as db:
        db.execute("DELETE FROM app_migrations WHERE name='interest-background-v1'")
        migrate_background(db)
    expected = text.replace('阶段性关注', '近期关注')
    assert current(uid)['content'] == expected and current(uid)['embedding'] == vector
    assert one('SELECT content FROM interest_profile WHERE id=?', (historical,))['content'] == text
    assert one('SELECT content,embedding FROM embedding_rebuild_profiles WHERE profile_id=?', (latest,)) == {'content':expected, 'embedding':vector}
