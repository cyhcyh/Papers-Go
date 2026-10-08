import json
from datetime import datetime, timezone, timedelta
import pytest
from .conftest import headers, finish_interest_updates
from app.config import settings, now, today
from app.db import one, rows, execute, dumps, unpack
from app.interest.profile import current, put_profile, active_entries
from app.pipeline.read import verify_card, request_card, _tasks
from app.pipeline.fetch import parse_atom, parse_conference_feed, upsert_paper
from app.pipeline.trends import trend_stats
from app.pipeline.alerts import evaluate_alerts
from app.pipeline.metrics import metrics
from app.pipeline.score import quality_score, cosine
from app.scheduler import make_scheduler


def init_profile(client,account,topic):
    response=client.post('/api/profile/init',headers=headers(account),json={'description':'agent memory','topic_ids':[topic],'exclusions':[]})
    finish_interest_updates(client)
    assert response.status_code==200,response.text


def test_auth_invites_refresh_and_disabled(client,accounts):
    a,b=accounts
    assert a['user']['is_admin']==1 and b['user']['is_admin']==0
    assert client.get('/api/profile').status_code==401
    assert client.get('/api/admin/users',headers=headers(b)).status_code==403
    assert client.post('/api/auth/login',json={'username':'alice','password':'wrong123'}).status_code==401
    settings().require_invite_code=True
    assert client.post('/api/auth/register',json={'username':'charlie','password':'secret123'}).status_code==400
    invite=client.post('/api/admin/invites',headers=headers(a)).json()['code']
    c=client.post('/api/auth/register',json={'username':'charlie','password':'secret123','invite_code':invite})
    assert c.status_code==201
    assert client.post('/api/auth/register',json={'username':'david','password':'secret123','invite_code':invite}).status_code==400
    rotated=client.post('/api/auth/refresh',json={'refresh_token':b['refresh_token']})
    assert rotated.status_code==200
    assert client.post('/api/auth/refresh',json={'refresh_token':b['refresh_token']}).status_code==401
    assert client.get('/api/me',headers={'Authorization':'Bearer '+rotated.json()['refresh_token']}).status_code==401
    assert client.patch('/api/admin/users/'+str(b['user']['id']),headers=headers(a),json={'disabled':True}).status_code==200
    assert client.get('/api/me',headers=headers(b)).status_code==401


def test_first_registration_bootstraps_invite_mode(client):
    settings().require_invite_code=True
    first=client.post('/api/auth/register',json={'username':'owner','password':'secret123'})
    assert first.status_code==201 and first.json()['user']['is_admin']==1


def test_profiles_version_rollback_and_ttl(client,accounts):
    a,b=accounts
    init_profile(client,a,3)
    before=current(a['user']['id'])
    assert current(b['user']['id']) is None
    update=client.put('/api/profile',headers=headers(a),json={'content':'## 核心兴趣\n- [w:0.8] Ramsey theory','topic_ids':[15]})
    finish_interest_updates(client)
    assert current(a['user']['id'])['version']==2
    assert current(a['user']['id'])['content']!=before['content']
    assert client.post('/api/profile/rollback',headers=headers(b),json={'version':1}).status_code==404
    finish_interest_updates(client)
    assert client.post('/api/profile/rollback',headers=headers(a),json={'version':1}).status_code==200
    finish_interest_updates(client)
    assert current(a['user']['id'])['version']==3
    assert current(a['user']['id'])['content']==before['content']
    assert one('SELECT content FROM interest_profile WHERE id=?',(before['id'],))['content']==before['content']
    entries=active_entries('## 阶段性关注\n- [w:0.9, until:2020-01-01] expired\n- [w:0.8, until:2099-01-01] active\n## 明确排除\n- robots')
    assert [e['text'] for e in entries]==['active','robots']
    assert entries[-1]['excluded']


def test_feed_scoring_knn_and_user_isolation(client,accounts,papers):
    a,b=accounts
    client.post('/api/profile/init',headers=headers(a),json={'description':'agent memory'})
    finish_interest_updates(client)
    client.post('/api/profile/init',headers=headers(b),json={'description':'Ramsey theory'})
    finish_interest_updates(client)
    client.put('/api/profile',headers=headers(a),json={'form':{'long_term':[{'text':'agent memory','weight':.9}]}})
    finish_interest_updates(client)
    client.put('/api/profile',headers=headers(b),json={'form':{'long_term':[{'text':'Ramsey theory','weight':.9}]}})
    finish_interest_updates(client)
    from app.db import pack
    # Exercise the legacy aggregate-vector path without keeping newer parts
    # that would correctly take precedence over this test vector.
    execute('UPDATE interest_profile SET embedding=?,embedding_parts=NULL WHERE user_id=?',(pack([0.,1.,0.,0.]),b['user']['id']))
    fa=client.get('/api/feed/today',headers=headers(a)).json()
    fb=client.get('/api/feed/today',headers=headers(b)).json()
    sa={p['id']:p['score'] for p in fa['items']}; sb={p['id']:p['score'] for p in fb['items']}
    assert sa[papers[0]]>sb[papers[0]]
    assert fa['total']==3
    assert client.post('/api/interactions',headers=headers(a),json={'paper_id':papers[0],'action':'like','dwell_ms':3000}).status_code==200
    assert client.get('/api/feed/today',headers=headers(a)).json()['total']==2
    assert client.get('/api/feed/today',headers=headers(b)).json()['total']==3
    assert len(client.get('/api/library?type=like',headers=headers(a)).json()['items'])==1
    assert client.get('/api/library?type=like',headers=headers(b)).json()['items']==[]
    assert client.post('/api/interactions',headers=headers(a),json={'paper_id':papers[0],'action':'remove_like','feed_context':'library'}).status_code==200
    assert client.get('/api/library?type=like',headers=headers(a)).json()['items']==[]
    assert client.get('/api/browse?topic_id=3&range=all',headers=headers(a)).json()['total']==1


def test_interaction_undo_atomic_and_deadline(client,accounts,papers):
    a,b=accounts
    init_profile(client,a,3)
    before=current(a['user']['id'])['embedding']
    event=client.post('/api/interactions',headers=headers(a),json={'paper_id':papers[1],'action':'save','dwell_ms':1234}).json()
    assert current(a['user']['id'])['embedding']==before
    assert client.post('/api/interactions',headers=headers(b),json={'action':'undo','target_id':event['id']}).status_code==404
    assert client.post('/api/interactions',headers=headers(a),json={'action':'undo','target_id':event['id']}).status_code==200
    assert current(a['user']['id'])['embedding']==before
    assert client.get('/api/library',headers=headers(a)).json()['items']==[]
    assert len(rows('SELECT * FROM interactions WHERE user_id=?',(a['user']['id'],)))==2
    assert client.post('/api/interactions',headers=headers(a),json={'action':'undo','target_id':event['id']}).status_code==409
    new=client.post('/api/interactions',headers=headers(a),json={'paper_id':papers[1],'action':'skip'}).json()
    execute('UPDATE interactions SET created_at=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(seconds=5)).isoformat(),new['id']))
    assert client.post('/api/interactions',headers=headers(a),json={'action':'undo','target_id':new['id']}).status_code==409


def test_notifications_watches_and_trends_are_per_user(client,accounts,papers):
    a,b=accounts
    init_profile(client,a,3);init_profile(client,b,15)
    watch=client.post('/api/watches',headers=headers(a),json={'type':'keyword','value':'agent memory'}).json()
    evaluate_alerts();evaluate_alerts()
    notices=client.get('/api/notifications',headers=headers(a)).json()
    assert notices['unread']==1
    ident=notices['items'][0]['id']
    assert client.get('/api/notifications',headers=headers(b)).json()['items']==[]
    assert client.post(f'/api/notifications/{ident}/read',headers=headers(b)).status_code==404
    assert client.post(f'/api/notifications/{ident}/read',headers=headers(a)).status_code==200
    assert client.delete('/api/watches/'+str(watch['id']),headers=headers(b)).status_code==404
    trend_stats()
    ta=client.get('/api/trends',headers=headers(a)).json();tb=client.get('/api/trends',headers=headers(b)).json()
    assert {t['id'] for t in ta['topics']}=={3}
    assert {t['id'] for t in tb['topics']}=={15}
    assert len(ta['topics'][0]['series'])==14
    client.post('/api/interactions',headers=headers(a),json={'paper_id':papers[0],'action':'save'})
    assert client.get('/api/trends',headers=headers(a)).json()['movements']
    assert client.get('/api/trends',headers=headers(b)).json()['movements']==[]


def test_chat_tools_confirmation_ownership_and_persistence(client,accounts,papers,monkeypatch):
    from app.llm.provider import cloud
    from types import SimpleNamespace as N
    a,b=accounts;init_profile(client,a,3)
    calls=0
    async def stream(messages,tools):
        nonlocal calls
        calls+=1
        if calls==1:
            yield N(content=None,tool_calls=[N(index=0,id='tool-1',function=N(name='update_interest',arguments=dumps({'patch':'## 核心兴趣\n- [w:0.9] agent evaluation','topic_ids':[4]})))])
        else:
            yield N(content='请确认这项修改。',tool_calls=[])
    monkeypatch.setattr(cloud,'stream',stream)
    session=client.post('/api/chat/sessions',headers=headers(a),json={'title':'新对话'}).json()['id']
    assert client.get(f'/api/chat/sessions/{session}/messages',headers=headers(b)).status_code==404
    before=current(a['user']['id'])['content']
    response=client.post(f'/api/chat/sessions/{session}/messages',headers=headers(a),json={'content':'关注智能体评测','paper_id':papers[0]})
    assert response.status_code==200 and 'event: confirm' in response.text and 'event: delta' in response.text
    assert current(a['user']['id'])['content']==before
    pending=one('SELECT * FROM pending_tools WHERE user_id=?',(a['user']['id'],))
    assert client.post('/api/chat/confirm',headers=headers(b),json={'id':pending['id']}).status_code==404
    assert client.post('/api/chat/confirm',headers=headers(a),json={'id':pending['id']}).status_code==200
    assert current(a['user']['id'])['change_reason']=='chat_confirmed'
    assert client.post('/api/chat/confirm',headers=headers(a),json={'id':pending['id']}).status_code==409
    history=client.get(f'/api/chat/sessions/{session}/messages',headers=headers(a)).json()
    assert any(m.get('proposals') and m['proposals'][0]['status']=='confirmed' for m in history)


@pytest.mark.asyncio
async def test_confirm_watch_rejection_and_stale_profile(client,accounts):
    from app.agent.tools import execute_tool
    a,b=accounts;init_profile(client,a,3)
    session=client.post('/api/chat/sessions',headers=headers(a),json={}).json()['id']
    proposal=await execute_tool('add_watch',{'type':'benchmark','value':'LoCoBench'},a['user']['id'],session,current(a['user']['id']))
    ident=proposal['proposal']['id']
    assert rows('SELECT * FROM watches')==[]
    assert client.post('/api/chat/confirm',headers=headers(a),json={'id':ident,'approve':False}).status_code==200
    assert rows('SELECT * FROM watches')==[]
    second=await execute_tool('add_watch',{'type':'keyword','value':'memory'},a['user']['id'],session,current(a['user']['id']))
    assert client.post('/api/chat/confirm',headers=headers(a),json={'id':second['proposal']['id']}).status_code==200
    assert len(rows('SELECT * FROM watches'))==1
    third=await execute_tool('update_interest',{'patch':'new profile'},a['user']['id'],session,current(a['user']['id']))
    put_profile(a['user']['id'],'manual new',{},'manual')
    assert client.post('/api/chat/confirm',headers=headers(a),json={'id':third['proposal']['id']}).status_code==409


def test_reading_evidence_and_card_shared_without_reason(client,accounts,papers):
    a,b=accounts
    card={'tldr':'一句话','method_summary':'方法','key_results':[
      {'claim':'11 points','evidence':{'section':'fake section','quote':'A verified result improves accuracy by 11 points.'},'verified':True},
      {'claim':'made up','evidence':{'section':'4.2','quote':'A made up quotation from another paper.'},'verified':True}],
      'limitations':['实验范围有限'],'read_priority':'worth_reading'}
    fulltext={'text':'A verified result\n improves accuracy by 11 points.','pages':[{'section':'page 4','text':'A verified result improves accuracy by 11 points.'}],'sections':[]}
    verified=verify_card(card,fulltext)
    assert verified['key_results'][0]['verified'] and verified['key_results'][0]['evidence']['section']=='page 4'
    assert not verified['key_results'][1]['verified']
    execute("INSERT INTO reading_cards(paper_id,status,card_json,created_at) VALUES(?,'ready',?,?)",(papers[0],dumps(verified),now()))
    ca=client.get(f'/api/papers/{papers[0]}/card',headers=headers(a)).json()
    cb=client.get(f'/api/papers/{papers[0]}/card',headers=headers(b)).json()
    assert ca['card']==cb['card'] and 'why_you_care' not in ca and 'why_you_care' not in cb
    assert 'why_you_care' not in one('SELECT card_json FROM reading_cards')['card_json']


def test_ingestion_version_dedup_and_parsers(client,papers):
    first=one('SELECT * FROM papers WHERE id=?',(papers[0],))
    upsert_paper({'arxiv_id':first['arxiv_id'],'arxiv_version':2,'title':'Updated Graph Memory','abstract':'new abstract','authors':['Updated Author'],'published':'2099-01-01'})
    updated=one('SELECT * FROM papers WHERE id=?',(papers[0],))
    assert updated['published']==first['published'] and updated['arxiv_version']==2
    assert updated['embedding'] is None and not rows('SELECT * FROM papers_vec WHERE paper_id=?',(papers[0],))
    atom='''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"><entry><id>http://arxiv.org/abs/2609.12345v3</id><title>A title</title><summary>Abstract</summary><published>2026-09-30T00:00:00Z</published><author><name>Alice Researcher</name></author><arxiv:primary_category term="cs.AI"/><link title="pdf" href="https://arxiv.org/pdf/2609.12345v3"/></entry></feed>'''
    parsed=parse_atom(atom)[0]
    assert parsed['arxiv_id']=='2609.12345' and parsed['arxiv_version']==3
    atom='<feed xmlns="http://www.w3.org/2005/Atom"><title>NeurIPS.2025</title><entry><id>https://papers.cool/venue/test@OpenReview</id><title>A real title</title><summary>Abstract</summary><author><name>Alice</name></author></entry></feed>'
    year,parsed=parse_conference_feed(atom,'NeurIPS')
    assert year==2025 and parsed[0]['venue']=='NeurIPS' and parsed[0]['primary_category'] is None



def test_metrics_and_scheduler(client,accounts,papers):
    a,b=accounts
    client.post('/api/interactions',headers=headers(a),json={'paper_id':papers[0],'action':'like','dwell_ms':2000})
    client.post('/api/interactions',headers=headers(a),json={'paper_id':papers[1],'action':'skip','dwell_ms':100})
    for paper in papers[:2]:
        client.post('/api/interactions',headers=headers(a),json={'paper_id':paper,'action':'view','dwell_ms':5000})
    metrics()
    data=client.get('/api/admin/metrics/daily',headers=headers(a)).json()
    assert data[0]['shown']==2 and data[0]['like_rate']==.5 and data[0]['quick_skip_rate']==.5
    assert client.get('/api/admin/metrics/daily?user_id='+str(b['user']['id']),headers=headers(a)).json()==[]
    scheduler=make_scheduler()
    assert len([job for job in scheduler.get_jobs() if not job.id.startswith('_')])==6
    assert scheduler.get_job('pipeline') is not None
    assert str(scheduler.timezone)=='Asia/Shanghai'
    assert quality_score({'venue_rank':'oral','hf_upvotes':10,'github_stars':100},{'novelty':80,'rigor':90})>70


def test_flat_topic_management_merge_and_user_access(client,accounts,papers):
    a,b=accounts
    init_profile(client,a,3)
    assert client.post('/api/admin/topics',headers=headers(b),json={'name_zh':'主题','name_en':'Topic'}).status_code==403
    assert client.patch('/api/admin/topics/1',headers=headers(a),json={'name_zh':'AI','name_en':'AI','parent_id':3}).status_code==400
    assert client.patch('/api/admin/topics/1',headers=headers(a),json={'name_zh':'AI','name_en':'AI','parent_id':9}).status_code==400
    assert client.post('/api/admin/topics/2/merge',headers=headers(a),json={'target_id':2}).status_code==400
    assert client.post('/api/admin/topics/1/merge',headers=headers(a),json={'target_id':15}).status_code==400
    assert client.delete('/api/admin/topics/3',headers=headers(a)).status_code==200
    assert one('SELECT status FROM topics WHERE id=3')['status']=='disabled'
    assert json.loads(current(a['user']['id'])['structured'])['topic_ids']==[]
    assert not one('SELECT * FROM paper_topics WHERE paper_id=?',(papers[0],))
    assert one('SELECT id FROM papers WHERE id=?',(papers[0],))
    assert one('SELECT classified FROM papers WHERE id=?',(papers[0],))['classified']==0



def test_preferences_and_password(client,accounts):
    a,b=accounts
    assert client.put('/api/me/preferences',headers=headers(a),json={'notifications_enabled':False,'telegram_enabled':False,'telegram_chat_id':'12345'}).status_code==200
    assert client.get('/api/me',headers=headers(b)).json()['telegram_chat_id'] is None
    assert client.put('/api/me/password',headers=headers(a),json={'old_password':'wrong','new_password':'newpass123','confirm_password':'newpass123'}).status_code==400
    assert client.put('/api/me/password',headers=headers(a),json={'old_password':'secret123','new_password':'newpass123','confirm_password':'newpass123'}).status_code==200
    assert client.post('/api/auth/refresh',json={'refresh_token':a['refresh_token']}).status_code==401
    assert client.post('/api/auth/login',json={'username':'alice','password':'newpass123'}).status_code==200
