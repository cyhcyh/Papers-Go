from datetime import date, timedelta

from app.config import now, settings, today
from app.db import connect, dumps, execute, pack, set_paper_vector
from app.interest.profile import put_profile
from .conftest import headers


def add_paper(code='cs.AI', age=0, quality=50, ingested_age=None):
    published=(date.fromisoformat(today())-timedelta(days=age)).isoformat()
    ingested=(date.fromisoformat(today())-timedelta(days=age if ingested_age is None else ingested_age)).isoformat()
    return execute('''INSERT INTO papers(title,abstract,primary_category,published,
                      ingested_date,created_at,quality_score,scored)
                      VALUES(?,?,?,?,?,?,?,1)''',
                   (f'{code} paper {age}', 'Research', code,published,ingested,now(),quality))


def select(account, categories=('arxiv:cs.AI',), weights=None):
    put_profile(account['user']['id'],'',{'category_selection':{
        'categories':list(categories),'topics':{},'weights':weights or {key:1 for key in categories}}},'test')


def test_recent_first_then_history_fills_the_same_page(client,accounts):
    recent={add_paper(quality=1) for _ in range(3)}
    # Recently ingested older papers belong to the recent stage too.
    recent.add(add_paper(age=365,ingested_age=0,quality=1))
    older={add_paper(age=200,quality=100) for _ in range(30)}
    outside=add_paper(code='math.CO',age=200,quality=100)
    select(accounts[0]); auth=headers(accounts[0])
    first=client.get('/api/feed/today',headers=auth).json()
    ids=[p['id'] for p in first['items']]
    assert len(ids)==20 and set(ids[:4])==recent
    assert set(ids[4:])<=older and outside not in ids
    second=client.get('/api/feed/today',headers=auth,params={'exclude':','.join(map(str,ids))}).json()
    rest=[p['id'] for p in second['items']]
    assert len(rest)==14 and not set(rest)&set(ids)
    assert set(ids+rest)==recent|older and second['total']==14


def test_first_page_does_not_query_history_when_recent_is_sufficient(client,accounts,monkeypatch):
    import app.pipeline.score as score
    for _ in range(30):add_paper()
    add_paper(age=100)
    select(accounts[0]); calls=[]; original=score.candidate_pool
    def traced(user_id,context,excluded=(),scoring=None,*,budget=None):
        calls.append(context);return original(user_id,context,excluded,scoring,budget=budget)
    monkeypatch.setattr(score,'candidate_pool',traced)
    assert len(client.get('/api/feed/today',headers=headers(accounts[0])).json()['items'])==20
    assert calls==['today']


def test_seen_cache_shortage_refills_before_cache_is_empty(client,accounts,monkeypatch):
    import app.pipeline.score as score
    monkeypatch.setattr(settings(),'recommendation_candidates',100)
    with connect() as db:
        db.executemany('INSERT INTO papers(title,abstract,primary_category,published,ingested_date,created_at) VALUES(?,?,?,?,?,?)',
                       [(f'paper {i}','Research','cs.AI',today(),today(),now()) for i in range(650)])
    select(accounts[0]);auth=headers(accounts[0]);uid=accounts[0]['user']['id']
    client.get('/api/feed/today',headers=auth)
    key=score.ranking_key(uid,'today');saved=score._ranking_cache[key]
    keep=set(saved['ids'][-5:]); seen=set(saved['ids'])-keep
    with connect() as db:
        db.executemany('INSERT INTO user_paper_state(user_id,paper_id,seen,updated_at) VALUES(?,?,1,?)',
                       [(uid,ident,now()) for ident in seen])
    response=client.get('/api/feed/today',headers=auth).json()
    ids={p['id'] for p in response['items']}
    assert len(ids)==20 and keep<=ids and not ids&seen
    assert score._ranking_cache[key] is saved  # Appended, not discarded/rebuilt.


def test_weighted_history_paging_is_complete_and_non_repeating(client,accounts,monkeypatch):
    monkeypatch.setattr(settings(),'recommendation_candidates',100)
    expected={add_paper('cs.AI',age=200) for _ in range(330)}
    expected.update(add_paper('math.CO',age=200) for _ in range(110))
    select(accounts[0],('arxiv:cs.AI','arxiv:math.CO'),{'arxiv:cs.AI':.7,'arxiv:math.CO':1})
    auth=headers(accounts[0]);seen=[]
    for _ in range(30):
        response=client.get('/api/feed/today',headers=auth,params={'exclude':','.join(map(str,seen))}).json()
        ids=[p['id'] for p in response['items']]
        assert not set(ids)&set(seen)
        seen.extend(ids)
        if response['total']<=len(ids):break
        assert len(ids)==20
    assert len(seen)==len(expected) and set(seen)==expected


def test_vector_reader_reuses_identical_search_and_observes_worker_updates(client,accounts,monkeypatch):
    import app.vector_search as vectors
    from app.pipeline.score import candidate_pool
    from app.db import one
    vectors.close()
    first=add_paper();second=add_paper()
    set_paper_vector(first,[1,0,0,0]);set_paper_vector(second,[0,1,0,0])
    put_profile(accounts[0]['user']['id'],'',{'category_selection':{'categories':['arxiv:cs.AI'],'topics':{}}},'test',pack([1,0,0,0]))
    revision=one("SELECT value FROM app_settings WHERE name='paper_revision'")['value']
    query=pack([1,0,0,0]);assert vectors.nearest_ids(query,2,revision)[0]==first
    from app import vector_store
    with vector_store.reader(vector_store.active()['name']) as db:
        traced=[];db.set_trace_callback(traced.append)
    assert vectors.nearest_ids(query,2,revision)[0]==first
    assert not any(' MATCH ' in statement for statement in traced)
    set_paper_vector(first,[0,1,0,0]);set_paper_vector(second,[1,0,0,0])
    revision=one("SELECT value FROM app_settings WHERE name='paper_revision'")['value']
    assert vectors.nearest_ids(query,2,revision)[0]==second
    with vector_store.reader(vector_store.active()['name']) as current_reader:
        assert current_reader is db and not db.in_transaction
    # Candidate scope is applied after shared global KNN results.
    outside=add_paper('math.CO');set_paper_vector(outside,[1,0,0,0])
    papers,total=candidate_pool(accounts[0]['user']['id'],'recommendations')
    assert {p['id'] for p in papers}=={first,second} and total==2
    # Replacing the index for an embedding model must not retain old IDs.
    with connect() as writer:
        writer.execute('UPDATE papers SET embedding=NULL')
    with vector_store.writer(vector_store.active()['name']) as writer:
        vector_store.index_create(writer,4)
        writer.execute("UPDATE metadata SET value=value+1 WHERE name='revision'")
    revision=one("SELECT value FROM app_settings WHERE name='paper_revision'")['value']
    assert vectors.nearest_ids(query,2,revision)==[]
    set_paper_vector(second,[1,0,0,0])
    revision=one("SELECT value FROM app_settings WHERE name='paper_revision'")['value']
    assert vectors.nearest_ids(query,2,revision)==[second]
    vectors.close()
