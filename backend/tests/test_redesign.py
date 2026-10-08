import json
import pytest
from app.config import now, today
from app.db import execute, one, dumps
from app.interest.profile import current, put_profile, active_entries
from app.interest.form import parse_form
from .conftest import headers, finish_interest_updates


def test_source_directory_scope_and_cross_listed_browse(client, accounts, papers):
    auth = headers(accounts[0])
    catalog = client.get('/api/categories', headers=auth).json()
    codes = {c['code'] for c in catalog}
    assert 'math.CO' in codes and 'cs.DM' not in codes
    assert {c['code'] for c in catalog if c['kind'] == 'venue'} == {'ICML', 'AAAI', 'NeurIPS', 'ICLR'}
    execute('UPDATE papers SET primary_category=?,categories=?,venue=? WHERE id=?',
            ('cs.SE', dumps(['cs.SE', 'cs.AI']), 'ICLR.2026', papers[0]))
    execute('UPDATE papers SET primary_category=? WHERE id=?', ('math.CO', papers[1]))
    assert [p['id'] for p in client.get('/api/browse?category=arxiv:math.CO&range=all', headers=auth).json()['items']] == [papers[1]]
    result = client.get('/api/browse?category=arxiv:cs.AI&topic_id=3&range=all', headers=auth).json()
    assert papers[0] not in [p['id'] for p in result['items']]
    assert client.get('/api/browse?category=venue:ICLR&range=all', headers=auth).json()['total'] == 1
    assert client.get('/api/browse?category=arxiv:cs.DM', headers=auth).status_code == 400
    assert client.get('/api/categories').status_code == 200


def test_form_round_trip_history_ttl_and_inferred_entries(client, accounts):
    a, b = accounts
    put_profile(a['user']['id'], '## 核心兴趣\n- [w:0.83] 图论\n## 系统推断\n- [w:0.2] Ramsey theory', {}, 'init')
    form = client.get('/api/profile', headers=headers(a)).json()['current']['form']
    assert form['long_term'][0]['weight'] == .83
    assert form['inferred'] == ['Ramsey theory']
    form.update(description='研究组合数学与 Ramsey 界', temporary=[{'text':'近期证明问题','weight':1,'until':'2099-01-01'}], exclusions=['机器人'])
    form['inferred'] = ['应被忽略的客户端修改']
    response = client.put('/api/profile', headers=headers(a), json={'form':form, 'category_selection':{'categories':['arxiv:math.CO'],'topics':{}}})
    finish_interest_updates(client)
    assert response.status_code == 200 and current(a['user']['id'])['version'] == 2
    content = current(a['user']['id'])['content']
    assert 'Ramsey theory' in content and '应被忽略' not in content
    assert parse_form(content)['long_term'][0]['weight'] == .83
    assert parse_form(content)['description'] == form['description']
    assert any(e['text'] == '近期证明问题' and not e['excluded'] for e in active_entries(content))
    assert client.get('/api/profile', headers=headers(b)).json()['current'] is None
    assert client.post('/api/profile/rollback', headers=headers(a), json={'version':1}).status_code == 200
    finish_interest_updates(client)
    restored = client.get('/api/profile', headers=headers(a)).json()['current']
    assert restored['form']['temporary'] == [] and restored['form']['long_term'][0]['text'] == '图论'
    assert client.put('/api/profile', headers=headers(a), json={'form':{'temporary':[{'text':'方向','until':'bad-date'}]}}).status_code == 422
    finish_interest_updates(client)


def test_whole_category_selection_includes_future_topics_and_partial_is_scoped(client, accounts, papers):
    a = accounts[0]
    auth = headers(a)
    selection = {'categories':['arxiv:cs.AI'], 'topics':{}}
    assert client.post('/api/profile/init', headers=auth, json={'category_selection':selection}).status_code == 200
    finish_interest_updates(client)
    new_id = execute('INSERT INTO topics(name_zh,name_en,category_keys,created_at) VALUES(?,?,?,?)', ('新记忆主题','New memory topic',dumps(['arxiv:cs.AI']),now()))
    catalog = client.get('/api/categories', headers=auth).json()
    assert new_id in {t['id'] for c in catalog if c['key']=='arxiv:cs.AI' for t in c['topics']}
    # A partial interest in cs.AI must not become an interest in the same topic at every venue/category.
    execute('UPDATE papers SET primary_category=?,categories=? WHERE id=?', ('cs.CL', '[]', papers[2]))
    execute('DELETE FROM paper_topics WHERE paper_id=?', (papers[2],))
    execute('INSERT INTO paper_topics VALUES(?,?,.9)', (papers[2],3))
    execute('UPDATE papers SET embedding=NULL,quality_score=40')
    execute('UPDATE papers SET title=?,abstract=?', ('Same title','Same abstract'))
    selection = {'categories':[], 'topics':{'arxiv:cs.AI':[3]}}
    response = client.put('/api/profile', headers=auth, json={'form':{}, 'category_selection':selection})
    finish_interest_updates(client)
    assert response.status_code == 200
    execute('UPDATE interest_profile SET embedding=NULL')
    ranked = {p['id']:p['score'] for p in client.get('/api/browse?range=all', headers=auth).json()['items']}
    assert ranked[papers[0]] > ranked[papers[2]]
    assert client.put('/api/profile', headers=auth, json={'form':{},'category_selection':{'categories':['arxiv:cs.DM']}}).status_code == 400
    finish_interest_updates(client)
    assert client.put('/api/profile', headers=auth, json={'form':{},'category_selection':{'topics':{'arxiv:math.CO':[3]}}}).status_code == 400
    finish_interest_updates(client)


def test_neutral_views_recommendations_and_paging_exclusion(client, accounts, papers):
    a, b = accounts
    auth = headers(a)
    client.post('/api/profile/init', headers=auth, json={'description':'agent memory'})
    finish_interest_updates(client)
    before = current(a['user']['id'])['embedding']
    response = client.post('/api/interactions', headers=auth, json={'paper_id':papers[0],'action':'view','dwell_ms':5000})
    assert response.status_code == 200
    state = one('SELECT * FROM user_paper_state WHERE user_id=? AND paper_id=?', (a['user']['id'],papers[0]))
    assert state['seen'] == 1 and state['liked'] == state['saved'] == 0
    assert current(a['user']['id'])['embedding'] == before
    assert papers[0] not in [p['id'] for p in client.get('/api/recommendations', headers=auth).json()['items']]
    assert papers[0] in [p['id'] for p in client.get('/api/recommendations', headers=headers(b)).json()['items']]
    client.post('/api/interactions', headers=auth, json={'paper_id':papers[0],'action':'like'})
    assert client.get('/api/stats/today', headers=auth).json()['shown'] == 1
    assert client.get(f'/api/feed/today?exclude={papers[1]},{papers[2]}', headers=auth).json()['items'] == []
    assert client.get('/api/feed/today?exclude=bad', headers=auth).status_code == 400


@pytest.mark.asyncio
async def test_three_sentence_generation_is_persisted_and_validated(client, papers, monkeypatch):
    from app.pipeline.tldr import generate_brief
    from app.llm.ollama import ollama
    async def chat(prompt, json_mode=True):
        assert '不要写推荐理由' in prompt
        return {'title_zh':'智能体图记忆','problem':'研究记忆问题。','contribution':'使用图记忆。','result':'摘要报告准确率改善。'}
    monkeypatch.setattr(ollama,'chat',chat)
    brief = await generate_brief(one('SELECT * FROM papers WHERE id=?',(papers[0],)))
    assert json.loads(one('SELECT brief_json FROM papers WHERE id=?',(papers[0],))['brief_json']) == brief
    async def invalid(prompt, json_mode=True): return {'title_zh':'缺失字段'}
    monkeypatch.setattr(ollama,'chat',invalid)
    with pytest.raises(ValueError):
        await generate_brief(one('SELECT * FROM papers WHERE id=?',(papers[1],)))
    assert one('SELECT brief_json FROM papers WHERE id=?',(papers[1],))['brief_json'] is None


@pytest.mark.asyncio
async def test_arxiv_explicit_trial_covers_math_without_advancing_daily_cursor(client, monkeypatch):
    import httpx
    from app.pipeline import fetch
    from app.config import settings
    original_client = httpx.AsyncClient
    requests = []
    def handler(request):
        category = str(request.url.params['search_query']).removeprefix('cat:')
        requests.append((category, int(request.url.params['max_results'])))
        index = len(requests)
        atom = f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:x="http://arxiv.org/schemas/atom"><entry>
          <id>https://arxiv.org/abs/2610.{index:05d}</id><title>Source {category}</title><summary>Abstract</summary>
          <published>2026-10-01T00:00:00Z</published><x:primary_category term="{category}"/>
          <category term="{category}"/><category term="cs.AI"/></entry></feed>'''
        return httpx.Response(200, text=atom)
    monkeypatch.setattr(fetch.httpx,'AsyncClient',lambda **kwargs:original_client(transport=httpx.MockTransport(handler),**kwargs))
    async def no_wait(_): pass
    monkeypatch.setattr(fetch.asyncio,'sleep',no_wait)
    settings().fetch_limit = 50
    assert await fetch.fetch_arxiv(limit=50) == 9
    assert 'math.CO' in {category for category, _ in requests}
    assert sum(budget for _, budget in requests) == 50
    assert json.loads(one("SELECT categories FROM papers WHERE primary_category='math.CO'")['categories']) == ['math.CO', 'cs.AI']
    assert one('SELECT COUNT(*) AS n FROM arxiv_cursors')['n'] == 0


def test_profile_form_preserves_plain_text_brackets_and_paragraphs():
    from app.interest.form import ProfileForm, render_form
    form = ProfileForm(description='长期记忆\n组合数学', long_term=[{'text':'Graph [Memory]','weight':.75}])
    restored = parse_form(render_form(form))
    assert restored['description'] == form.description
    assert restored['long_term'][0]['text'] == 'Graph [Memory]'
