import pytest

from app.config import now, today
from app.db import execute, one
from app.pipeline.metrics import metrics
from .conftest import headers


def test_five_second_threshold_daily_dedup_and_separate_feedback(client, accounts, papers):
    admin,_=accounts
    auth=headers(admin)
    for duration in (0,1200,4999):
        response=client.post('/api/interactions',headers=auth,json={'paper_id':papers[0],'action':'view','dwell_ms':duration})
        assert response.json()=={'id':None,'recorded':False}
    assert not one('SELECT paper_id FROM user_paper_state')
    for action,ident in [('like',papers[0]),('save',papers[0]),('skip',papers[1])]:
        assert client.post('/api/interactions',headers=auth,json={'paper_id':ident,'action':action}).status_code==200
    assert client.get('/api/stats/today',headers=auth).json()['shown']==0
    metrics()
    metric=one('SELECT * FROM daily_metrics')
    assert metric['shown']==0 and metric['considered']==2 and metric['like_rate']==.5
    first=client.post('/api/interactions',headers=auth,json={'paper_id':papers[0],'action':'view','dwell_ms':5000}).json()
    repeated=client.post('/api/interactions',headers=auth,json={'paper_id':papers[0],'action':'view','dwell_ms':9000}).json()
    assert repeated=={'id':first['id'],'recorded':False}
    assert client.get('/api/stats/today',headers=auth).json()['shown']==1
    execute('UPDATE interactions SET created_at=? WHERE id=?',('2026-01-01T00:00:00+00:00',first['id']))
    assert client.post('/api/interactions',headers=auth,json={'paper_id':papers[0],'action':'view','dwell_ms':5000}).json()['id']!=first['id']


def test_undo_feedback_retains_subsequent_qualified_view(client, accounts, papers):
    admin,_=accounts
    auth=headers(admin)
    like=client.post('/api/interactions',headers=auth,json={'paper_id':papers[0],'action':'like'}).json()
    client.post('/api/interactions',headers=auth,json={'paper_id':papers[0],'action':'view','dwell_ms':5000})
    assert client.post('/api/interactions',headers=auth,json={'action':'undo','target_id':like['id']}).status_code==200
    assert one('SELECT liked,seen FROM user_paper_state')=={'liked':0,'seen':1}
    assert client.get('/api/stats/today',headers=auth).json()['shown']==1


def test_legacy_counts_remain_after_migration(client, accounts, papers):
    admin,_=accounts
    execute('INSERT INTO interactions(user_id,paper_id,action,created_at) VALUES(?,?,?,?)',(admin['user']['id'],papers[0],'save',now()))
    from app.db import init_db
    init_db()
    assert client.get('/api/stats/today',headers=headers(admin)).json()['shown']==1
    metrics()
    assert one('SELECT shown,considered FROM daily_metrics')=={'shown':1,'considered':1}
