import asyncio
import threading
from datetime import date,timedelta

import httpx
import pytest

from app.config import now,today
from app.db import connect,dumps,execute,one,pack,rows
from app.interest.profile import put_profile
from app.pipeline import alerts
from .conftest import headers


def watch(client,account,kind,value):
    response=client.post('/api/watches',headers=headers(account),json={'type':kind,'value':value})
    assert response.status_code==200
    return response.json()['id']


def notices(uid):
    return rows('SELECT * FROM notifications WHERE user_id=? ORDER BY id',(uid,))


def test_unchanged_run_does_no_paper_work(client,accounts,papers,monkeypatch):
    uid=accounts[0]['user']['id'];watch(client,accounts[0],'keyword','agent memory')
    assert alerts.evaluate_alerts()==3 and len(notices(uid))==1
    def unexpected(*args,**kwargs):raise AssertionError('unchanged papers must not be loaded or reparsed')
    monkeypatch.setattr(alerts,'_materials',unexpected)
    assert alerts.evaluate_alerts()==0 and len(notices(uid))==1
    execute('UPDATE papers SET quality_score=quality_score+1 WHERE id=?',(papers[0],))
    assert alerts.evaluate_alerts()==0


def test_late_reading_and_classification_are_evaluated_even_after_recent_window(client,accounts,papers):
    uid=accounts[0]['user']['id'];watch(client,accounts[0],'benchmark','LoCoBench')
    old=(date.fromisoformat(today())-timedelta(days=10)).isoformat()
    execute('UPDATE papers SET ingested_date=?',(old,));alerts.evaluate_alerts()
    assert not notices(uid)
    claim={'claim':'LoCoBench comparison results','verified':True,'evidence':{'quote':'Original reported comparison results','section':'4'}}
    execute("INSERT INTO reading_cards(paper_id,status,card_json,created_at) VALUES(?,'ready',?,?)",(papers[1],dumps({'key_results':[claim]}),now()))
    assert alerts.evaluate_alerts()==1 and notices(uid)[0]['paper_id']==papers[1]
    assert alerts.evaluate_alerts()==0
    wid=watch(client,accounts[0],'topic','3');alerts.evaluate_alerts()
    before=len(notices(uid))
    execute('INSERT INTO paper_topics VALUES(?,?,1)',(papers[1],3));alerts.evaluate_alerts()
    assert len(notices(uid))==before+1 and notices(uid)[-1]['dedupe_key']==f'watch:{wid}:{papers[1]}'


def test_exact_topic_binding_survives_rename(client,accounts,papers):
    account=accounts[0];uid=account['user']['id']
    topic=one('SELECT name_zh FROM topics WHERE id=3')['name_zh']
    wid=watch(client,account,'topic',topic)
    assert one('SELECT topic_id FROM watches WHERE id=?',(wid,))['topic_id']==3
    alerts.evaluate_alerts();before=len(notices(uid))
    execute("UPDATE topics SET name_zh='新主题名称',name_en='Renamed topic' WHERE id=3")
    execute('INSERT INTO paper_topics VALUES(?,?,1)',(papers[1],3))
    alerts.evaluate_alerts()
    assert len(notices(uid))==before+1
    assert notices(uid)[-1]['paper_id']==papers[1]


def test_keyword_boundaries_and_full_author_names(client,accounts,papers):
    account=accounts[0];uid=account['user']['id']
    wid=watch(client,account,'keyword','agent');watch(client,account,'author','Author')
    extra=execute('INSERT INTO papers(title,abstract,authors,created_at,ingested_date) VALUES(?,?,?,?,?)',('Reagent study','reagents',dumps(['Research Author']),now(),today()))
    alerts.evaluate_alerts()
    assert {n['paper_id'] for n in notices(uid)}=={papers[0],papers[2]}
    assert all(n['dedupe_key'].startswith(f'watch:{wid}:') for n in notices(uid))
    watch(client,account,'author','Research Author');alerts.evaluate_alerts()
    assert any(n['paper_id']==extra for n in notices(uid))
    pattern=alerts.keyword_pattern('agent memory')
    assert pattern.search(alerts.folded('Agent-memory methods'))
    assert not pattern.search('reagent memory')


def test_known_author_ids_disambiguate_same_names(client,accounts,papers):
    execute('INSERT INTO author_metrics VALUES(?,?,?,?,?)',('A100','Research Author',10,'[]',now()))
    execute('INSERT INTO paper_author_links VALUES(?,?,?,?)',(papers[0],'A100','Research Author',1))
    execute('INSERT INTO paper_author_links VALUES(?,?,?,?)',(papers[2],'A200','Research Author',1))
    account=accounts[0];wid=watch(client,account,'author','Research Author')
    # A name linked to two identities cannot be resolved automatically. An explicit ID is reliable.
    assert one('SELECT author_id FROM watches WHERE id=?',(wid,))['author_id'] is None
    client.delete(f'/api/watches/{wid}',headers=headers(account))
    watch(client,account,'author','https://openalex.org/A100');alerts.evaluate_alerts()
    assert {n['paper_id'] for n in notices(account['user']['id'])}=={papers[0]}


def test_known_author_binding_rejects_different_id_and_falls_back_if_unmatched(client,accounts,papers):
    execute('INSERT INTO author_metrics VALUES(?,?,?,?,?)',('A100','Research Author',10,'[]',now()))
    account=accounts[0];wid=watch(client,account,'author','Research Author')
    assert one('SELECT author_id FROM watches WHERE id=?',(wid,))['author_id']=='A100'
    execute('INSERT INTO paper_author_links VALUES(?,?,?,?)',(papers[2],'A200','Research Author',1))
    alerts.evaluate_alerts()
    assert {n['paper_id'] for n in notices(account['user']['id'])}=={papers[0]}


def test_comparison_notice_uses_verified_evidence_and_neutral_title(client,accounts,papers):
    account=accounts[0];uid=account['user']['id']
    client.post('/api/interactions',headers=headers(account),json={'paper_id':papers[0],'action':'save'})
    claims=[{'claim':'Outperforms Graph Memory for Agents','verified':False}]
    execute("INSERT INTO reading_cards(paper_id,status,card_json,created_at) VALUES(?,'ready',?,?)",(papers[2],dumps({'key_results':claims}),now()))
    alerts.evaluate_alerts();assert not notices(uid)
    claims[0]['verified']=True
    execute('UPDATE reading_cards SET card_json=? WHERE paper_id=?',(dumps({'key_results':claims}),papers[2]))
    alerts.evaluate_alerts();notice=notices(uid)[0]
    assert notice['title']=='收藏论文出现相关比较' and notice['type']=='baseline_beaten'


def test_stopped_batches_resume_across_users_and_do_not_duplicate(client,accounts,papers,monkeypatch):
    monkeypatch.setattr(alerts,'USER_BATCH',1)
    for account in accounts:watch(client,account,'keyword','agent memory')
    stop=threading.Event();original=alerts._ack_papers
    def acknowledge(*args,**kwargs):
        original(*args,**kwargs);stop.set()
    monkeypatch.setattr(alerts,'_ack_papers',acknowledge)
    alerts.evaluate_alerts(stop)
    assert one('SELECT COUNT(*) n FROM alert_paper_state WHERE pending=1')['n']==3
    assert one('SELECT MIN(cursor_user) n FROM alert_paper_state WHERE pending=1')['n']==accounts[0]['user']['id']
    monkeypatch.setattr(alerts,'_ack_papers',original)
    alerts.evaluate_alerts()
    assert one('SELECT COUNT(*) n FROM alert_paper_state WHERE pending=1')['n']==0
    assert all(len(notices(a['user']['id']))==1 for a in accounts)
    assert alerts.evaluate_alerts()==0


def test_changed_watch_during_evaluation_never_writes_stale_notice(client,accounts,papers,monkeypatch):
    uid=accounts[0]['user']['id'];wid=watch(client,accounts[0],'keyword','agent memory')
    original=alerts._write_notifications;changed=False
    def write(proposals,stop):
        nonlocal changed
        if not changed:
            execute('UPDATE watches SET active=0 WHERE id=?',(wid,));changed=True
        return original(proposals,stop)
    monkeypatch.setattr(alerts,'_write_notifications',write)
    alerts.evaluate_alerts();assert not notices(uid)
    assert not one('SELECT pending FROM alert_user_state WHERE user_id=?',(uid,))['pending']


def test_changed_paper_during_evaluation_never_writes_stale_notice(client,accounts,papers,monkeypatch):
    uid=accounts[0]['user']['id'];watch(client,accounts[0],'keyword','unique-marker')
    execute("UPDATE papers SET abstract='unique-marker' WHERE id=?",(papers[1],))
    original=alerts._write_notifications;changed=False
    def write(proposals,stop):
        nonlocal changed
        if not changed:
            execute("UPDATE papers SET abstract='Unrelated results' WHERE id=?",(papers[1],));changed=True
        return original(proposals,stop)
    monkeypatch.setattr(alerts,'_write_notifications',write)
    alerts.evaluate_alerts();assert not notices(uid)


def test_notification_preferences_are_respected_and_reenable_catches_up(client,accounts,papers):
    uid=accounts[0]['user']['id'];watch(client,accounts[0],'keyword','agent memory')
    execute('UPDATE users SET notifications_enabled=0 WHERE id=?',(uid,));alerts.evaluate_alerts()
    assert not notices(uid)
    execute('UPDATE users SET notifications_enabled=1 WHERE id=?',(uid,));alerts.evaluate_alerts()
    assert len(notices(uid))==1


def test_paper_update_before_loading_restarts_user_cursor(client,accounts,papers,monkeypatch):
    monkeypatch.setattr(alerts,'USER_BATCH',1)
    watch(client,accounts[0],'keyword','new-marker')
    watch(client,accounts[1],'keyword','old-marker')
    execute("UPDATE papers SET abstract='old-marker' WHERE id=?",(papers[1],))
    original=alerts._contexts;changed=False
    def contexts(db,cache,**kwargs):
        nonlocal changed
        if kwargs.get('after')==accounts[0]['user']['id'] and not changed:
            execute("UPDATE papers SET abstract='new-marker' WHERE id=?",(papers[1],));changed=True
        return original(db,cache,**kwargs)
    monkeypatch.setattr(alerts,'_contexts',contexts)
    alerts.evaluate_alerts()
    assert changed and {n['paper_id'] for n in notices(accounts[0]['user']['id'])}=={papers[1]}


def test_baseline_only_users_do_not_load_papers_without_reading_results(client,accounts,papers,monkeypatch):
    client.post('/api/interactions',headers=headers(accounts[0]),json={'paper_id':papers[0],'action':'save'})
    def unexpected(*args,**kwargs):raise AssertionError('no ready reading results means no baseline evidence to parse')
    monkeypatch.setattr(alerts,'_materials',unexpected)
    assert alerts.evaluate_alerts()==3 and not notices(accounts[0]['user']['id'])


def test_removing_research_direction_during_evaluation_prevents_stale_collision(client,accounts,papers,monkeypatch):
    uid=accounts[0]['user']['id']
    put_profile(uid,'## 在研方向\n- Agent research',{},'test',pack([1,0,0,0]))
    original=alerts._write_notifications;changed=False
    def write(proposals,stop):
        nonlocal changed
        if not changed:
            put_profile(uid,'## 核心兴趣\n- Graph theory',{},'test',pack([1,0,0,0]));changed=True
        return original(proposals,stop)
    monkeypatch.setattr(alerts,'_write_notifications',write)
    alerts.evaluate_alerts()
    assert not notices(uid)
    assert not one('SELECT pending FROM alert_user_state WHERE user_id=?',(uid,))['pending']


@pytest.mark.asyncio
async def test_collision_alert_does_not_start_reading_or_call_models(client,accounts,papers,monkeypatch):
    from app.pipeline import read
    account=accounts[0]
    put_profile(account['user']['id'],'## 在研方向\n- Agent research',{},'test',pack([1,0,0,0]))
    def unexpected(*args,**kwargs):raise AssertionError('alerts must not start expensive reading')
    async def no_push():pass
    monkeypatch.setattr(read,'request_card',unexpected)
    monkeypatch.setattr(alerts,'push_telegram',no_push)
    monkeypatch.setattr(read.models,'complete',unexpected)
    assert await alerts.alert_eval()==3
    assert any(n['type']=='collision' for n in notices(account['user']['id']))


def test_authenticated_explicit_l3_request_and_shared_cache(client,accounts,papers,monkeypatch):
    import app.api.content as content
    calls=[]
    def request(paper_id,level,retry,regenerate=False,allow_regenerate=True):
        assert not regenerate
        assert allow_regenerate==bool(accounts[len(calls)]['user']['is_admin'])
        calls.append((paper_id,level,retry));return {'status':'ready','error':None,'card_json':dumps({'reading_level':'L3'}),'created_at':now()}
    monkeypatch.setattr(content,'request_card',request)
    path=f'/api/papers/{papers[0]}/card?level=L3'
    assert client.get(path).status_code==401 and not calls
    for account in accounts:
        assert client.get(path,headers=headers(account)).json()['card']['reading_level']=='L3'
    assert calls==[(papers[0],'L3',False)]*2
    assert client.get(path.replace('L3','L4'),headers=headers(accounts[0])).status_code==422


@pytest.mark.asyncio
async def test_shared_l3_result_is_not_generated_twice(client,accounts,papers,monkeypatch):
    from app.pipeline import read
    calls=[]
    async def generate(paper_id,level):
        calls.append((paper_id,level))
        execute("UPDATE reading_cards SET status='ready',card_json=? WHERE paper_id=?",(dumps({'reading_level':level}),paper_id))
    monkeypatch.setattr(read,'generate_card',generate)
    assert read.request_card(papers[0],level='L3')['status']=='pending'
    await read._tasks[papers[0]]
    assert read.request_card(papers[0],level='L3')['status']=='ready'
    assert calls==[(papers[0],'L3')]


@pytest.mark.asyncio
async def test_background_evaluation_does_not_block_requests_and_can_stop(client,accounts,papers,monkeypatch):
    from app.main import app
    started=threading.Event();finished=threading.Event()
    def slow(stop):
        started.set()
        assert stop.wait(3)
        finished.set();return 0
    monkeypatch.setattr(alerts,'evaluate_alerts',slow)
    task=asyncio.create_task(alerts.alert_eval())
    assert await asyncio.wait_for(asyncio.to_thread(started.wait,1),1.5)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as web:
        response=await asyncio.wait_for(web.get('/api/feed/today',headers=headers(accounts[0])),1)
        assert response.status_code==200 and response.json()['items']
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert await asyncio.wait_for(asyncio.to_thread(finished.wait,1),1.5)
