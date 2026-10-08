from datetime import datetime, timedelta, timezone
import json
from app.db import connect, execute, one, rows
from app.logs import event, cleanup
from app.site_settings import configuration
from .conftest import headers


def add_logs(count, *, age_days=0):
    timestamp=(datetime.now(timezone.utc)-timedelta(days=age_days)).isoformat()
    with connect() as db:
        db.executemany("INSERT INTO app_logs(kind,level,message,created_at) VALUES('test','info',?,?)",
                       [(f'record-{index}',timestamp) for index in range(count)])


def policy(**changes):
    value={**configuration(),**changes}
    execute("UPDATE app_settings SET value=? WHERE name='site'",(json.dumps(value),))


def test_log_settings_are_private_admin_only_validated_and_preserved(client,accounts):
    auth=headers(accounts[0]);initial=configuration()
    assert initial['log_retention_days']==30 and initial['log_max_entries']==500_000
    assert not {'log_retention_days','log_max_entries'} & client.get('/api/site').json().keys()
    assert client.put('/api/admin/site',json=initial,headers=headers(accounts[1])).status_code==403
    for key,value in [('log_retention_days',0),('log_retention_days',3651),('log_max_entries',0),('log_max_entries',10_000_001),('log_max_entries',1.5)]:
        assert client.put('/api/admin/site',json={**initial,key:value},headers=auth).status_code==422
    saved=client.put('/api/admin/site',json={**initial,'log_retention_days':45,'log_max_entries':123456},headers=auth)
    assert saved.status_code==200
    legacy={key:value for key,value in initial.items() if not key.startswith('log_')}
    assert client.put('/api/admin/site',json=legacy,headers=auth).status_code==200
    assert configuration()['log_retention_days']==45 and configuration()['log_max_entries']==123456


def test_cleanup_combines_age_and_count_keeps_newest_and_preserves_other_data(client):
    policy(log_retention_days=7,log_max_entries=4)
    add_logs(3,age_days=8);add_logs(9,age_days=1)
    newest=[row['id'] for row in rows('SELECT id FROM app_logs ORDER BY created_at DESC,id DESC LIMIT 4')]
    execute("INSERT INTO llm_usage(model,input_tokens,output_tokens,created_at) VALUES('unchanged',10,20,?)",(datetime.now(timezone.utc).isoformat(),))
    result=cleanup()
    assert result['deleted']==8 and not result['more']
    assert [row['id'] for row in rows('SELECT id FROM app_logs ORDER BY created_at DESC,id DESC')]==newest
    assert one("SELECT input_tokens FROM llm_usage WHERE model='unchanged'")['input_tokens']==10


def test_log_browsing_uses_configured_retention_and_keeps_filters(client,accounts):
    policy(log_retention_days=45,log_max_entries=123456)
    add_logs(2,age_days=35);add_logs(1,age_days=46)
    auth=headers(accounts[0])
    result=client.get('/api/admin/logs?kind=test',headers=auth)
    assert result.status_code==200
    body=result.json()
    assert body['total']==2 and len(body['items'])==2
    assert body['policy']=={'retention_days':45,'max_entries':123456}
    assert client.get('/api/admin/logs?kind=test&query=record-1',headers=auth).json()['total']==1
    assert len(client.get('/api/admin/logs?kind=test&offset=1&limit=1',headers=auth).json()['items'])==1
    policy(log_retention_days=7)
    assert client.get('/api/admin/logs?kind=test',headers=auth).json()['total']==0


def test_cleanup_is_bounded_and_continues_without_losing_recent_logs(client):
    policy(log_retention_days=7,log_max_entries=5)
    add_logs(12,age_days=9);add_logs(8,age_days=1)
    result=cleanup(batch_size=3,max_batches=1)
    assert result=={'deleted':3,'more':True}
    assert one('SELECT COUNT(*) n FROM app_logs')['n']==17
    for _ in range(10):
        result=cleanup(batch_size=3,max_batches=1)
        if not result['more']:break
    assert not result['more'] and one('SELECT COUNT(*) n FROM app_logs')['n']==5
    assert [row['message'] for row in rows('SELECT message FROM app_logs ORDER BY id')]==[f'record-{index}' for index in range(3,8)]


def test_inserting_logs_does_not_run_retention_queries(client):
    add_logs(2,age_days=31)
    policy(log_max_entries=1)
    event('system','new row',api_key='never-save-this',error='Bearer never-save-token')
    assert one('SELECT COUNT(*) n FROM app_logs')['n']==3
    encoded=json.dumps(rows('SELECT * FROM app_logs'))
    assert 'never-save-this' not in encoded and 'never-save-token' not in encoded
    cleanup()
    assert [row['message'] for row in rows('SELECT message FROM app_logs')]==['new row']


def test_saving_log_limits_starts_cleanup_in_background(client,accounts):
    add_logs(5,age_days=1)
    body={**configuration(),'log_max_entries':2}
    response=client.put('/api/admin/site',headers=headers(accounts[0]),json=body)
    assert response.status_code==200
    assert one('SELECT COUNT(*) n FROM app_logs')['n']==2
    assert any(row['message']=='网站基本设置已更新' for row in rows('SELECT message FROM app_logs'))
