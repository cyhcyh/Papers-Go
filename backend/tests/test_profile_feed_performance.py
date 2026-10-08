from app.config import now, today
from app.db import execute, one, pack, rows
from app.interest.profile import put_profile, current
from .conftest import headers, finish_interest_updates


def test_category_only_save_reuses_learned_vector_and_reads_topics_once(client, accounts, monkeypatch):
    import app.api.profile as api
    import app.catalog as catalog
    uid=accounts[0]['user']['id']
    text='## 核心兴趣\n- [w:0.7] Graph theory\n'
    vector=pack([0,1,0,0])
    put_profile(uid,text,{},'test',vector)
    async def unexpected(content):
        raise AssertionError('category changes must not regenerate embeddings')
    monkeypatch.setattr(api.profile_updates,'profile_embedding',unexpected)
    queries=[]; original=catalog.rows
    def traced(sql,args=()):
        queries.append(sql);return original(sql,args)
    monkeypatch.setattr(catalog,'rows',traced)
    response=client.put('/api/profile',headers=headers(accounts[0]),json={'content':text,'category_selection':{'categories':['arxiv:math.CO'],'topics':{},'weights':{'arxiv:math.CO':1}}})
    assert response.status_code==200
    assert current(uid)['embedding']==vector
    assert len([q for q in queries if 'FROM topics' in q])==1
    assert not any('paper_categories' in q or 'paper_topics' in q for q in queries)
    assert client.get('/api/feed/today',headers=headers(accounts[0])).status_code==200


def test_changed_text_or_entry_weight_regenerates_vector(client, accounts, monkeypatch):
    import app.api.profile as api
    uid=accounts[0]['user']['id']
    put_profile(uid,'## 核心兴趣\n- [w:0.7] Graph theory',{},'test',pack([1,0,0,0]))
    called=[]
    async def embed(content,**kwargs):called.append(content);return pack([0,1,0,0])
    monkeypatch.setattr(api.profile_updates,'profile_embedding',embed)
    for content in ['## 核心兴趣\n- [w:0.9] Graph theory','## 核心兴趣\n- [w:0.9] Ramsey theory']:
        assert client.put('/api/profile',headers=headers(accounts[0]),json={'content':content}).status_code==200
        finish_interest_updates(client)
    assert len(called)==2


def test_expired_entry_clears_old_vector_without_model_work(client, accounts, monkeypatch):
    import app.api.profile as api
    uid=accounts[0]['user']['id'];text='## 阶段性关注\n- [w:0.7, until:2020-01-02] Graph theory'
    p=put_profile(uid,text,{},'test',pack([1,0,0,0]))
    execute('UPDATE interest_profile SET created_at=? WHERE id=?',('2020-01-01T00:00:00+00:00',p['id']))
    called=[]
    async def embed(content):called.append(content);return None
    monkeypatch.setattr(api.profile_updates,'profile_embedding',embed)
    assert client.put('/api/profile',headers=headers(accounts[0]),json={'content':text}).status_code==200
    assert called==[] and current(uid)['embedding'] is None


def test_views_filter_cached_rankings_without_rebuilding(client,accounts,monkeypatch):
    import app.pipeline.score as score
    auth=headers(accounts[0]); uid=accounts[0]['user']['id']
    for i in range(35):
        execute('INSERT INTO papers(title,abstract,primary_category,published,ingested_date,created_at) VALUES(?,?,?,?,?,?)',(f'paper {i}','research','cs.AI',today(),today(),now()))
    put_profile(uid,'',{'category_selection':{'categories':['arxiv:cs.AI'],'topics':{}}},'test')
    count=[];original=score.candidate_pool
    def candidates(*args,**kwargs):count.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(score,'candidate_pool',candidates)
    first=client.get('/api/feed/today',headers=auth).json()['items'];ident=first[0]['id'];key=score.ranking_key(uid,'today')
    assert client.post('/api/interactions',headers=auth,json={'paper_id':ident,'action':'view','dwell_ms':4999}).json()['recorded'] is False
    client.post('/api/interactions',headers=auth,json={'paper_id':ident,'action':'view','dwell_ms':5000})
    second=client.get('/api/feed/today',headers=auth).json()['items']
    assert score.ranking_key(uid,'today')==key
    assert count==[1] and len(second)==20 and ident not in {p['id'] for p in second}
    client.post('/api/interactions',headers=auth,json={'paper_id':second[0]['id'],'action':'like'})
    client.get('/api/feed/today',headers=auth)
    assert len(count)==2 and score.ranking_key(uid,'today')!=key


def test_browse_author_search_is_available_for_both_sorts(client,papers):
    for sort in ('score','date'):
        response=client.get('/api/browse',params={'query':'Other Author','range':'all','sort':sort})
        assert response.status_code==200
        assert [p['id'] for p in response.json()['items']]==[papers[1]]
