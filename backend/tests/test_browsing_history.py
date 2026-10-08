from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from app import browsing
from app.config import now, today, settings
from app.db import connect, dumps, execute, one, rows
from app.interest.profile import put_profile
from app.pipeline.expire import purge_batch
from .conftest import headers


def state(uid, paper, stamp, **flags):
    execute('''INSERT INTO user_paper_state(user_id,paper_id,seen,liked,saved,dismissed,last_browsed_at,updated_at)
        VALUES(?,?,1,?,?,?,?,?)''',
        (uid,paper,flags.get('liked',0),flags.get('saved',0),flags.get('dismissed',0),stamp,stamp))


def seed(count, category='cs.AI', quality=20):
    ids=[]
    with connect() as db:
        for index in range(count):
            ids.append(db.execute('''INSERT INTO papers(title,abstract,authors,primary_category,
                published,ingested_date,created_at,quality_score,scored) VALUES(?,?,?,?,?,?,?,?,1)''',
                (f'{category} research {index}','Research results','[]',category,today(),today(),now(),quality)).lastrowid)
    return ids


def visit(client, account, paper, action='view', **extra):
    response=client.post('/api/interactions',headers=headers(account),
        json={'paper_id':paper,'action':action,'dwell_ms':5000,**extra})
    assert response.status_code==200,response.text
    return response.json()


def test_repeat_visit_updates_history_without_daily_double_count_or_lifetime_extension(client,accounts,papers,monkeypatch):
    from app.api import content
    account=accounts[0];uid=account['user']['id'];ident=papers[0]
    expires=one('SELECT expires_at FROM papers WHERE id=?',(ident,))['expires_at']
    stamp=datetime.now(timezone.utc)
    monkeypatch.setattr(content,'now',lambda:stamp.isoformat())
    first=visit(client,account,ident)
    stamp+=timedelta(seconds=60)
    repeated=visit(client,account,ident)
    assert repeated=={'id':first['id'],'recorded':False}
    assert one('SELECT last_browsed_at FROM user_paper_state WHERE user_id=? AND paper_id=?',(uid,ident))['last_browsed_at']==stamp.isoformat()
    assert one("SELECT COUNT(*) n FROM interactions WHERE user_id=? AND action='view'",(uid,))['n']==1
    assert client.get('/api/stats/today',headers=headers(account)).json()['shown']==1
    assert one('SELECT expires_at FROM papers WHERE id=?',(ident,))['expires_at']==expires
    assert len(client.get('/api/library?type=history',headers=headers(account)).json()['items'])==1


def test_history_keyset_order_privacy_and_repeat_deduplication(client,accounts):
    ids=seed(45);uid=accounts[0]['user']['id'];base=datetime.now(timezone.utc)-timedelta(days=2)
    for index,ident in enumerate(ids):
        state(uid,ident,(base+timedelta(seconds=index//2)).isoformat())
    state(accounts[1]['user']['id'],ids[0],now())
    auth=headers(accounts[0]);collected=[];cursor=''
    while True:
        data=client.get('/api/library',headers=auth,params={'type':'history','limit':20,'cursor':cursor}).json()
        assert len(data['items'])<=20
        collected.extend(p['id'] for p in data['items'])
        cursor=data['next_cursor']
        if not cursor:break
    assert collected==list(reversed(ids))
    assert len(collected)==len(set(collected))==45
    assert [p['id'] for p in client.get('/api/library?type=history',headers=headers(accounts[1])).json()['items']]==ids[:1]
    assert client.get('/api/library?type=history').status_code==401
    # A row revisited while loading moves above the cursor; it must not recur below it.
    first=client.get('/api/library?type=history',headers=auth).json()
    visit(client,accounts[0],ids[-1])
    rest=client.get('/api/library',headers=auth,params={'type':'history','cursor':first['next_cursor']}).json()
    assert ids[-1] not in [p['id'] for p in rest['items']]
    latest=client.get('/api/library?type=history',headers=auth).json()['items'][0]
    assert latest['id']==ids[-1] and latest['last_browsed_at']


@pytest.mark.parametrize('cursor',['bad','[]','{}','null','["bad",1]','["2026-01-01",true]','["2026-01-01",0]','["2026-01-01",999999999999999999999999]'])
def test_history_rejects_invalid_cursor(client,accounts,cursor):
    assert client.get('/api/library',headers=headers(accounts[0]),params={'type':'history','cursor':cursor}).status_code==400


def test_social_history_removal_and_expand_semantics(client,accounts,papers):
    account=accounts[0];auth=headers(account)
    visit(client,account,papers[0],'expand')
    assert client.get('/api/library?type=history',headers=auth).json()['items']==[]
    for action in ('like','save','skip'):
        ident=papers[('like','save','skip').index(action)]
        visit(client,account,ident,action)
    before={p['id']:p['last_browsed_at'] for p in client.get('/api/library?type=history',headers=auth).json()['items']}
    visit(client,account,papers[0],'remove_like');visit(client,account,papers[1],'remove_save')
    after=client.get('/api/library?type=history',headers=auth).json()['items']
    assert {p['id']:p['last_browsed_at'] for p in after}==before
    assert client.get('/api/library?type=like',headers=auth).json()['items']==[]
    assert client.get('/api/library?type=save',headers=auth).json()['items']==[]
    assert one('SELECT dismissed FROM user_paper_state WHERE user_id=? AND paper_id=?',(account['user']['id'],papers[2]))['dismissed']==1


def test_history_keeps_saved_and_liked_flags_even_for_excluded_interests(client,accounts,papers):
    account=accounts[0];visit(client,account,papers[0],'like');visit(client,account,papers[0],'save')
    put_profile(account['user']['id'],'## 排除主题\n- Graph Memory',{},'test')
    paper=client.get('/api/library?type=history',headers=headers(account)).json()['items'][0]
    assert paper['id']==papers[0] and paper['liked'] is True and paper['saved'] is True


def test_undo_restores_cooldown_and_keeps_a_later_deduplicated_view(client,accounts,papers,monkeypatch):
    from app.api import content
    account=accounts[0];uid=account['user']['id'];auth=headers(account);ident=papers[0]
    first=visit(client,account,ident)
    old=(datetime.now(timezone.utc)-timedelta(days=8)).isoformat()
    execute('UPDATE user_paper_state SET last_browsed_at=? WHERE user_id=? AND paper_id=?',(old,uid,ident))
    skip=visit(client,account,ident,'skip')
    assert client.post('/api/interactions',headers=auth,json={'action':'undo','target_id':skip['id']}).status_code==200
    assert one('SELECT dismissed,last_browsed_at FROM user_paper_state WHERE user_id=? AND paper_id=?',(uid,ident))=={'dismissed':0,'last_browsed_at':old}
    skip=visit(client,account,ident,'skip')
    later=(datetime.now(timezone.utc)+timedelta(milliseconds=100)).isoformat()
    monkeypatch.setattr(content,'now',lambda:later)
    assert visit(client,account,ident)=={'id':first['id'],'recorded':False}
    assert client.post('/api/interactions',headers=auth,json={'action':'undo','target_id':skip['id']}).status_code==200
    assert one('SELECT dismissed,seen,last_browsed_at FROM user_paper_state WHERE user_id=? AND paper_id=?',(uid,ident))=={'dismissed':0,'seen':1,'last_browsed_at':later}


def test_migration_backfills_once_including_legacy_favorites_and_ignores_undone_skip(client,accounts,papers):
    uid=accounts[0]['user']['id'];old='2026-01-01T00:00:00+00:00'
    for ident in papers:
        state(uid,ident,old)
    execute('UPDATE user_paper_state SET seen=0,liked=1,last_browsed_at=NULL WHERE paper_id=?',(papers[2],))
    first=execute('INSERT INTO interactions(user_id,paper_id,action,created_at) VALUES(?,?,?,?)',(uid,papers[0],'skip',old))
    execute('INSERT INTO interactions(user_id,paper_id,action,target_id,created_at) VALUES(?,?,?,?,?)',(uid,papers[0],'undo',first,now()))
    execute('INSERT INTO interactions(user_id,paper_id,action,created_at) VALUES(?,?,?,?)',(uid,papers[1],'skip',old))
    execute("DELETE FROM app_migrations WHERE name='browsing-cooldown-v1'")
    with connect() as db:browsing.initialize(db)
    records={r['paper_id']:r for r in rows('SELECT paper_id,seen,dismissed,last_browsed_at FROM user_paper_state')}
    assert records[papers[0]]['dismissed']==0
    assert records[papers[1]]['dismissed']==1
    assert records[papers[2]]['seen']==1 and records[papers[2]]['last_browsed_at']==old
    execute('UPDATE user_paper_state SET last_browsed_at=? WHERE paper_id=?',(now(),papers[0]))
    before=rows('SELECT * FROM user_paper_state')
    with connect() as db:browsing.initialize(db)
    assert rows('SELECT * FROM user_paper_state')==before


def test_cooldown_boundary_exclusions_cache_and_fixed_candidate_budget(client,accounts,monkeypatch):
    import app.pipeline.score as score
    monkeypatch.setattr(settings(),'recommendation_candidates',100)
    account=accounts[0];uid=account['user']['id'];unread=seed(40);old=seed(7,quality=100)
    put_profile(uid,'',{'category_selection':{'categories':['arxiv:cs.AI'],'topics':{}}},'test')
    clock=datetime.now(timezone.utc);monkeypatch.setattr(browsing,'now',lambda:clock.isoformat())
    stamps=[clock-timedelta(days=8),clock-timedelta(days=7),clock-timedelta(days=7)+timedelta(seconds=1)]
    for ident,stamp in zip(old[:3],stamps):state(uid,ident,stamp.isoformat())
    for ident,flag in zip(old[3:6],['liked','saved','dismissed']):state(uid,ident,(clock-timedelta(days=8)).isoformat(),**{flag:1})
    state(uid,old[6],clock.isoformat())
    calls=[];original=score.candidate_pool
    def counted(*args,**kwargs):
        calls.append(kwargs['budget']);return original(*args,**kwargs)
    monkeypatch.setattr(score,'candidate_pool',counted)
    auth=headers(account);first=client.get('/api/feed/today',headers=auth).json()['items']
    assert len(first)==20 and {p['id'] for p in first}&set(old)==set(old[:2])
    assert all(p['id'] in unread for p in first[:9])
    assert calls==[90]
    assert all(p['last_browsed_at'] for p in first if p['id'] in old)
    visit(client,account,old[0])
    second=client.get('/api/feed/today',headers=auth).json()['items']
    assert old[0] not in {p['id'] for p in second}
    assert calls==[90]  # Views filter cached IDs without rebuilding or model calls.
    assert old[3] not in {p['id'] for p in client.get('/api/recommendations',headers=auth).json()['items']}


def test_repeat_limit_relevance_and_source_proportions():
    unread=list(range(1,101));old=list(range(101,151));scores={i:20 for i in unread}|{i:90 for i in old}
    mixed=browsing.mix(unread,old,scores)
    assert mixed[:9]==unread[:9]
    for start in range(len(mixed)):
        assert len(set(mixed[start:start+20])&set(old))<=2
    assert browsing.mix(unread,old,{i:100 for i in unread}|{i:10 for i in old})[:100]==unread
    assert len(browsing.mix([],old,scores))==2
    assert browsing.mix([],old,scores,previous_returns=[-2,-1],presented=2)==[]
    categories={i:'AI' if i%2 else 'MATH' for i in unread+old};weights={'AI':.7,'MATH':1}
    mixed=browsing.mix(unread,old,scores,categories,weights)
    for end in (20,40,60,80):
        counts=Counter(categories[i] for i in mixed[:end])
        assert abs(counts['MATH']-end/1.7)<1
        assert len(set(mixed[end-20:end])&set(old))<=2


def test_live_weighted_feed_preserves_category_share_across_pages_with_returns(client,accounts):
    uid=accounts[0]['user']['id'];ai=seed(40);math=seed(40,'math.CO');old=seed(10,quality=100)+seed(10,'math.CO',quality=100)
    stamp=(datetime.now(timezone.utc)-timedelta(days=8)).isoformat()
    for ident in old:state(uid,ident,stamp)
    put_profile(uid,'',{'category_selection':{'categories':['arxiv:cs.AI','arxiv:math.CO'],'topics':{},'weights':{'arxiv:cs.AI':.7,'arxiv:math.CO':1}}},'test')
    collected=[]
    for index in range(3):
        page=client.get('/api/feed/today',headers=headers(accounts[0]),params={'exclude':','.join(map(str,collected))}).json()['items']
        assert len(page)==20
        ids=[p['id'] for p in page]
        assert not set(ids)&set(collected)
        assert len(set(ids)&set(old))<=2
        collected.extend(ids)
        counts=Counter('AI' if ident in ai or ident in old[:10] else 'MATH' for ident in collected)
        assert abs(counts['MATH']-len(collected)/1.7)<1
    for start in range(len(collected)):
        assert len(set(collected[start:start+20])&set(old))<=2


def test_no_repeat_only_tail_when_paging_an_exhausted_unread_queue(client,accounts):
    uid=accounts[0]['user']['id'];old=seed(10,quality=100)
    for ident in old:state(uid,ident,(datetime.now(timezone.utc)-timedelta(days=8)).isoformat())
    first=client.get('/api/feed/today',headers=headers(accounts[0])).json()['items']
    assert len(first)==2
    second=client.get('/api/feed/today',headers=headers(accounts[0]),params={'exclude':','.join(str(p['id']) for p in first)}).json()
    assert second['items']==[] and second['total']==0


def test_returns_preserve_recent_unread_priority_when_weighting_sources(client,accounts):
    uid=accounts[0]['user']['id'];recent=seed(4);older=seed(30)+seed(30,'math.CO');old=seed(8,'math.CO',quality=100)
    for ident in older+old:
        execute('UPDATE papers SET published=?,ingested_date=? WHERE id=?',('2026-01-01','2026-01-01',ident))
    for ident in old:state(uid,ident,(datetime.now(timezone.utc)-timedelta(days=8)).isoformat())
    put_profile(uid,'',{'category_selection':{'categories':['arxiv:cs.AI','arxiv:math.CO'],'topics':{}}},'test')
    items=client.get('/api/feed/today',headers=headers(accounts[0])).json()['items']
    assert {p['id'] for p in items[:4]}==set(recent)
    assert Counter(p['primary_category'] for p in items)=={'cs.AI':10,'math.CO':10}
    assert len({p['id'] for p in items}&set(old))<=2


def test_expiry_removes_history_and_uses_indexed_keyset_query(client,accounts,papers):
    account=accounts[0];uid=account['user']['id'];ident=papers[0]
    visit(client,account,ident)
    with connect() as db:
        plan=list(db.execute('''EXPLAIN QUERY PLAN SELECT p.id FROM user_paper_state s INDEXED BY idx_state_browsed
            JOIN papers p ON p.id=s.paper_id WHERE s.user_id=? AND s.last_browsed_at IS NOT NULL
            AND (s.last_browsed_at,s.paper_id)<(?,?) ORDER BY s.last_browsed_at DESC,s.paper_id DESC LIMIT 21''',(uid,now(),ident)))
    assert any('idx_state_browsed' in r['detail'] for r in plan)
    assert not any('TEMP B-TREE' in r['detail'] for r in plan)
    execute('UPDATE papers SET expires_at=? WHERE id=?',('2020-01-01T00:00:00+00:00',ident))
    assert purge_batch(now())==1
    assert client.get('/api/library?type=history',headers=headers(account)).json()['items']==[]
    assert not one('SELECT paper_id FROM user_paper_state WHERE user_id=? AND paper_id=?',(uid,ident))
