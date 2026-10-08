import asyncio
import json
from collections import Counter
from datetime import date, timedelta

import pytest

from app.config import now, settings, today
from app.db import connect, execute, one, pack, rows, set_paper_vector
from app.interest.profile import current, put_profile
from app.interest.vectors import active_vectors, make_embedding
from app.pipeline import score
from .conftest import headers


def profile(uid, interests=None, structured=None):
    interests=interests or [('Graph theory',1,[1.,0,0,0]),('Language models',1,[0.,1,0,0])]
    content='## 核心兴趣\n'+'\n'.join(f'- [w:{weight}] {text}' for text,weight,_ in interests)
    vector=make_embedding([{'text':text,'weight':weight} for text,weight,_ in interests],
                          [v for _,_,v in interests])
    return put_profile(uid,content,structured or {},'manual',vector)


def paper(title, vector, *, code='cs.AI', age=0, quality=50):
    published=(date.fromisoformat(today())-timedelta(days=age)).isoformat()
    ident=execute('''INSERT INTO papers(title,abstract,primary_category,published,ingested_date,
        created_at,quality_score,scored) VALUES(?,?,?,?,?,?,?,1)''',
        (title,'Research results',code,published,today(),now(),quality))
    set_paper_vector(ident,vector)
    return ident


def part_map(p):
    return {v['key']:v['blob'] for v in active_vectors(p)}


def test_best_interest_scores_do_not_dilute_different_directions(client,accounts):
    uid=accounts[0]['user']['id']
    math_paper=paper('Graph theorem',[1.,0,0,0])
    ai_paper=paper('Language method',[0.,1,0,0])
    other=paper('Different topic',[0.,0,1,0])
    profile(uid,[('Graph theory',1,[1.,0,0,0])])
    before=score.scored_papers(uid,rows('SELECT * FROM papers'))
    original=next(p['score'] for p in before if p['id']==math_paper)
    profile(uid)
    scores={p['id']:p['score'] for p in score.scored_papers(uid,rows('SELECT * FROM papers'))}
    assert scores[math_paper]==scores[ai_paper]==original
    assert scores[math_paper]>scores[other]
    response=client.get('/api/profile',headers=headers(accounts[0])).json()['current']
    assert response['embedding_ready'] and 'embedding_parts' not in response and 'embedding' not in response
    assert len(active_vectors(current(uid)))==2


def test_publication_freshness_adds_small_bonus_without_old_ingestion_bonus(client,accounts):
    uid=accounts[0]['user']['id'];profile(uid,[('Graph theory',1,[1.,0,0,0])])
    newest=paper('New theorem',[1.,0,0,0],age=0)
    older=paper('Old newly imported theorem',[1.,0,0,0],age=300)
    unknown=paper('Unknown publication',[1.,0,0,0])
    execute('UPDATE papers SET published=NULL,paper_date=NULL WHERE id=?',(unknown,))
    for endpoint in ['/api/feed/today','/api/browse?range=all&sort=score','/api/recommendations']:
        items=client.get(endpoint,headers=headers(accounts[0])).json()['items']
        assert [p['id'] for p in items]==[newest,older,unknown]
        assert items[0]['score']>items[1]['score']>=items[2]['score']


def test_old_relevant_paper_can_beat_new_lower_score_in_personal_feed(client,accounts):
    uid=accounts[0]['user']['id'];profile(uid,[('Graph theory',1,[1.,0,0,0])])
    older=paper('Relevant theorem',[1.,0,0,0],age=300)
    execute('UPDATE papers SET ingested_date=published WHERE id=?',(older,))
    newest=paper('Less relevant theorem',[.2,0,.98,0],age=0)
    items=client.get('/api/feed/today',headers=headers(accounts[0])).json()['items']
    assert [p['id'] for p in items]==[older,newest]


@pytest.mark.parametrize('directions',[2,12])
def test_retrieval_uses_independent_queries_with_one_candidate_budget(client,accounts,monkeypatch,directions):
    uid=accounts[0]['user']['id']
    profile(uid,[(f'Interest {i}',1,[1.,0,0,0] if i%2 else [0.,1,0,0]) for i in range(directions)])
    with connect() as db:
        db.executemany('''INSERT INTO papers(title,abstract,primary_category,published,ingested_date,created_at)
            VALUES(?,?,?,?,?,?)''',[(f'Test {i}','Research','cs.AI',today(),today(),now()) for i in range(250)])
    all_ids=[p['id'] for p in rows('SELECT id FROM papers')]
    calls=[]
    def nearest(blob,limit,revision,space=None):
        start=sum(n for _,n in calls);calls.append((blob,limit))
        return all_ids[start:start+limit]
    monkeypatch.setattr(score,'nearest_ids',nearest)
    monkeypatch.setattr(settings(),'recommendation_candidates',100)
    found,total=score.candidate_pool(uid,'recommendations')
    assert len(calls)==directions and sum(n for _,n in calls)==60
    assert all(blob in part_map(current(uid)).values() for blob,_ in calls)
    assert len(found)<=100 and total==250


def test_score_order_continues_across_pages_without_forced_direction_shares(client,accounts):
    uid=accounts[0]['user']['id']
    profile(uid,[('Graph theory',1,[1.,0,0,0]),('Language models',.5,[0.,1,0,0])])
    groups={}
    for key,vector in [('math',[1.,0,0,0]),('ai',[0.,1,0,0])]:
            for i in range(40):groups[paper(f'{key} result {i}',vector,quality=70 if key=='math' else 50)]=key
    auth=headers(accounts[0]);collected=[]
    for _ in range(3):
        items=client.get('/api/feed/today',headers=auth,params={'exclude':','.join(map(str,collected))}).json()['items']
        ids=[p['id'] for p in items]
        assert len(ids)==20 and not set(ids)&set(collected)
        assert all('_direction' not in p for p in items)
        collected.extend(ids)
        counts=Counter(groups[i] for i in collected)
        assert counts['math']==min(40,len(collected))


def test_explicit_source_proportions_take_priority_over_direction_mix(client,accounts):
    uid=accounts[0]['user']['id']
    selection={'categories':['arxiv:math.CO','arxiv:cs.AI'],'topics':{},
               'weights':{'arxiv:math.CO':.4,'arxiv:cs.AI':1}}
    profile(uid,[('Graph theory',1,[1.,0,0,0]),('Language models',.1,[0.,1,0,0])],
            {'category_selection':selection})
    for code in ['math.CO','cs.AI']:
        for i in range(20):paper(f'{code} {i}',[1.,0,0,0] if i%2 else [0.,1,0,0],code=code)
    items=client.get('/api/feed/today',headers=headers(accounts[0])).json()['items']
    assert Counter(p['primary_category'] for p in items)=={'math.CO':6,'cs.AI':14}


def test_exhausted_direction_fills_from_other_interests(client,accounts):
    uid=accounts[0]['user']['id'];profile(uid)
    expected={paper('One graph result',[1.,0,0,0])}
    expected.update(paper(f'AI result {i}',[0.,1,0,0]) for i in range(25))
    first=client.get('/api/feed/today',headers=headers(accounts[0])).json()['items']
    second=client.get('/api/feed/today',headers=headers(accounts[0]),params={
        'exclude':','.join(str(p['id']) for p in first)}).json()['items']
    assert len(first)==20 and len(second)==6
    assert {p['id'] for p in first+second}==expected


@pytest.mark.parametrize('action',['like','skip'])
def test_feedback_only_updates_matching_interest_and_undo_restores_it(client,accounts,action):
    uid=accounts[0]['user']['id'];profile(uid)
    ident=paper('Related graph result',[.9,0,.1,0])
    before=current(uid);auth=headers(accounts[0])
    event=client.post('/api/interactions',headers=auth,json={'paper_id':ident,'action':action}).json()
    changed=part_map(current(uid));original=part_map(before)
    assert changed['Graph theory']!=original['Graph theory']
    assert changed['Language models']==original['Language models']
    response=client.post('/api/interactions',headers=auth,json={'action':'undo','target_id':event['id']})
    assert response.status_code==200
    assert current(uid)['embedding_parts']==before['embedding_parts']
    assert current(uid)['embedding']==before['embedding']


def test_save_does_not_learn_and_undo_after_regeneration_keeps_new_vectors(client,accounts):
    from app.interest.profile import save_current_embedding
    uid=accounts[0]['user']['id'];profile(uid)
    ident=paper('Related graph result',[.9,0,.1,0]);auth=headers(accounts[0])
    before=current(uid)
    client.post('/api/interactions',headers=auth,json={'paper_id':ident,'action':'save'})
    assert part_map(current(uid))==part_map(before)
    event=client.post('/api/interactions',headers=auth,json={'paper_id':ident,'action':'like'}).json()
    rebuilt=make_embedding([{'text':'Graph theory','weight':1},{'text':'Language models','weight':1}],
                           [[0.,0,1,0],[0.,1,0,0]])
    assert save_current_embedding(current(uid),rebuilt)
    after=current(uid)
    response=client.post('/api/interactions',headers=auth,json={'action':'undo','target_id':event['id']})
    assert response.status_code==200
    assert part_map(current(uid))==part_map(after) and current(uid)['embedding']==after['embedding']


def test_background_category_edit_and_rollback_preserve_direction_vectors(client,accounts,monkeypatch):
    import app.api.profile as api
    uid=accounts[0]['user']['id'];first=profile(uid);before=current(uid)
    async def forbidden(*args):raise AssertionError('Background and category changes must reuse vectors')
    monkeypatch.setattr(api.profile_updates,'profile_embedding',forbidden)
    auth=headers(accounts[0])
    response=client.put('/api/profile',headers=auth,json={
        'content':'## 研究方向描述\nBackground only\n'+before['content'],
        'category_selection':{'categories':['arxiv:math.CO'],'topics':{}}})
    assert response.status_code==200 and part_map(current(uid))==part_map(before)
    assert client.post('/api/profile/rollback',headers=auth,json={'version':first['version']}).status_code==200
    assert part_map(current(uid))==part_map(before)


def test_source_and_topic_maintenance_preserves_learned_direction_vectors(client,accounts):
    uid=accounts[0]['user']['id'];profile(uid)
    before=current(uid)
    for reason in ('topic_removed','topic_merge','source_removed'):
        put_profile(uid,before['content'],{'category_selection':{'categories':['arxiv:math.CO'],'topics':{}}},
                    reason,before['embedding'])
        assert part_map(current(uid))==part_map(before)


def test_expired_recent_interest_stops_scoring_retrieval_and_feedback(client,accounts,monkeypatch):
    from app.interest import profile as profiles
    from app.interest.online import update_profile
    uid=accounts[0]['user']['id']
    content='## 核心兴趣\n- [w:1] Graph theory\n## 近期关注\n- [w:1, until:2090-01-01] Language models'
    bundle=make_embedding([{'text':'Graph theory','weight':1},{'text':'Language models','weight':1}],
                           [[1.,0,0,0],[0.,1,0,0]])
    put_profile(uid,content,{},'manual',bundle)
    monkeypatch.setattr(profiles,'today',lambda:'2090-01-02')
    p=current(uid)
    assert list(part_map(p))==['Graph theory']
    _,parts,_=update_profile(p,pack([.1,.9,0,0]),'like')
    stored=json.loads(parts)
    assert stored[1]==json.loads(p['embedding_parts'])[1]
    ident=paper('Expired direction result',[0.,1,0,0])
    item=score.scored_papers(uid,rows('SELECT * FROM papers'))[0]
    assert item['id']==ident and item['score']==10.2  # zero match; quality/freshness renormalized while author data is unavailable
    calls=[]
    monkeypatch.setattr(score,'nearest_ids',lambda blob,limit,revision,space=None:calls.append(blob) or [])
    score.candidate_pool(uid,'recommendations')
    assert calls==[pack([1.,0,0,0])]


def test_incremental_build_upgrades_current_only_and_reuses_paper_vectors(client,accounts,papers,monkeypatch):
    from app.pipeline.embed import build_vectors
    from app.interest import profile as profiles
    uid=accounts[0]['user']['id']
    content='## 核心兴趣\n- [w:1] Graph theory\n- [w:1] Language models'
    old=put_profile(uid,content,{},'manual',pack([1.,0,0,0]))['id']
    new=put_profile(uid,content,{},'manual',pack([1.,0,0,0]))['id']
    vectors_before=rows('SELECT id,embedding FROM papers')
    calls=[]
    async def embed(texts):calls.extend(texts);return [[1.,0,0,0] if t=='Graph theory' else [0.,1,0,0] for t in texts]
    monkeypatch.setattr(profiles.models,'embed',embed)
    asyncio.run(build_vectors())
    assert [t.split(' — ')[0] for t in calls]==['Graph theory','Language models']
    assert len(active_vectors(current(uid)))==2 and current(uid)['id']==new
    assert one('SELECT embedding_parts FROM interest_profile WHERE id=?',(old,))['embedding_parts'] is None
    assert rows('SELECT id,embedding FROM papers')==vectors_before
    calls.clear();asyncio.run(build_vectors());assert calls==[]


def test_regeneration_invalidates_warm_rank_cache_for_same_profile(client,accounts):
    from app.interest.profile import save_current_embedding
    uid=accounts[0]['user']['id'];profile(uid,[('Graph theory',1,[1.,0,0,0])])
    first=paper('First direction',[1.,0,0,0]);second=paper('Second direction',[0.,1,0,0])
    before=score.ranking_key(uid,'recommendations')
    assert score.ranked_page(uid,'recommendations',0,5)['items'][0]['id']==first
    new=make_embedding([{'text':'Graph theory','weight':1}],[[0.,1,0,0]])
    assert save_current_embedding(current(uid),new)
    assert score.ranking_key(uid,'recommendations')!=before
    assert score.ranked_page(uid,'recommendations',0,5)['items'][0]['id']==second
