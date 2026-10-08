import json
import pytest
from app.config import now,today,settings
from app.db import execute,one,rows,dumps,init_db
from app.standard_topics import catalog,candidates
from app.source_catalog import invalidate
from .conftest import headers

def source(kind,code,**kwargs):
    return {'kind':kind,'code':code,'label':code,'standard_system':'MSC2020' if code.startswith('math.') else 'CCS2012',**kwargs}

def test_admin_rename_and_password_reset_revoke_credentials(client,accounts,papers):
    admin,user=accounts;uid=user['user']['id'];auth=headers(admin)
    execute('INSERT INTO user_paper_state(user_id,paper_id,saved) VALUES(?,?,1)',(uid,papers[0]))
    assert client.patch(f'/api/admin/users/{uid}',headers=headers(user),json={'username':'newname'}).status_code==403
    assert client.patch(f'/api/admin/users/{uid}',headers=auth,json={'username':'alice'}).status_code==409
    changed=client.patch(f'/api/admin/users/{uid}',headers=auth,json={'username':'newname'}).json()
    assert changed['user']['id']==uid and 'password_hash' not in changed['user']
    assert client.get('/api/me',headers=headers(user)).json()['username']=='newname'
    assert client.patch(f'/api/admin/users/{uid}',headers=auth,json={'password':'newsecret99'}).status_code==200
    assert client.get('/api/me',headers=headers(user)).status_code==401
    assert client.post('/api/auth/refresh',json={'refresh_token':user['refresh_token']}).status_code==401
    assert client.post('/api/auth/login',json={'username':'newname','password':'secret123'}).status_code==401
    assert client.post('/api/auth/login',json={'username':'newname','password':'newsecret99'}).status_code==200
    assert one('SELECT saved FROM user_paper_state WHERE user_id=?',(uid,))['saved']==1
    assert 'newsecret99' not in dumps(rows('SELECT detail FROM app_logs'))

def test_delete_cleans_personal_data_but_preserves_public_papers_and_other_users(client,accounts,papers):
    from app.interest.profile import put_profile
    admin,user=accounts;uid=user['user']['id'];auth=headers(admin)
    put_profile(uid,'private',{ 'topic_ids':[]},'manual')
    session=client.post('/api/chat/sessions',headers=headers(user),json={'title':'Private'}).json()['id']
    execute("INSERT INTO chat_messages(session_id,role,content,created_at) VALUES(?,'user','Private text',?)",(session,now()))
    execute('INSERT INTO user_paper_state(user_id,paper_id,saved) VALUES(?,?,1)',(uid,papers[0]))
    execute('INSERT INTO user_paper_state(user_id,paper_id,saved) VALUES(?,?,1)',(admin['user']['id'],papers[0]))
    execute("INSERT INTO interactions(user_id,paper_id,action,created_at) VALUES(?,?,'save',?)",(uid,papers[0],now()))
    execute("INSERT INTO watches(user_id,type,value,created_at) VALUES(?,'keyword','private',?)",(uid,now()))
    execute("INSERT INTO notifications(user_id,type,title,created_at) VALUES(?,'test','private',?)",(uid,now()))
    execute("INSERT INTO direction_trends(audience,profile_version,summary) VALUES(?,1,'private')",(f'user:{uid}:month',))
    execute("INSERT INTO invite_codes VALUES('consumed',?,?)",(uid,now()))
    assert client.delete(f'/api/admin/users/{admin["user"]["id"]}',headers=auth).status_code==400
    assert client.delete(f'/api/admin/users/{uid}',headers=headers(user)).status_code==403
    assert client.get(f'/api/admin/users/{uid}/impact',headers=auth).json()['counts']['sessions']==1
    assert client.delete(f'/api/admin/users/{uid}',headers=auth).status_code==200
    for table in ('users','chat_sessions','interest_profile','watches','notifications','interactions','user_paper_state','refresh_tokens'):
        assert not rows('SELECT * FROM '+table+' WHERE '+('id' if table=='users' else 'user_id')+'=?',(uid,))
    assert not one('SELECT * FROM chat_messages WHERE session_id=?',(session,))
    assert not one('SELECT * FROM invite_codes') and not one('SELECT * FROM direction_trends')
    assert len(rows('SELECT id FROM papers'))==3 and one('SELECT saved FROM user_paper_state')['saved']==1
    replacement=client.post('/api/auth/register',json={'username':'replacement','password':'secret123'}).json()
    assert replacement['user']['id']>uid and client.get('/api/me',headers=headers(user)).status_code==401

def test_source_addition_scope_guest_defaults_disable_and_immutable_code(client,accounts,papers):
    auth=headers(accounts[0]);base='/api/admin/source-categories'
    assert client.get(base,headers=headers(accounts[1])).status_code==403
    assert client.post(base,headers=auth,json=source('arxiv','cs.fake')).status_code==400
    assert client.post(base,headers=auth,json=source('arxiv','math.PR',guest_default=False)).status_code==200
    created=next(s for s in client.get(base,headers=auth).json() if s['code']=='math.PR')
    assert created['standard_system'] is None
    assert created['fetch_enabled'] and not created['guest_default']
    edited=client.patch(base+'/arxiv:math.PR',headers=auth,json=source('arxiv','math.PR',label='概率论')).json()
    assert edited['fetch_enabled'] and not edited['guest_default']
    execute("UPDATE papers SET primary_category='math.PR',categories='[]' WHERE id=?",(papers[0],))
    assert 'arxiv:math.PR' in {s['key'] for s in client.get('/api/categories').json()}
    assert papers[0] not in {p['id'] for p in client.get('/api/recommendations').json()['items']}
    assert client.get('/api/browse?category=arxiv:math.PR&range=all').json()['total']==1
    assert client.patch(base+'/arxiv:math.PR',headers=auth,json={**created,'code':'math.NT'}).status_code==400
    assert client.patch(base+'/arxiv:math.PR',headers=auth,json={**created,'guest_default':True}).status_code==200
    assert client.get('/api/browse?category=arxiv:math.PR&range=all').json()['total']==1
    assert one('SELECT title FROM papers WHERE id=?',(papers[0],))
    assert client.post(base,headers=auth,json=source('venue','COLT',feed_url='http://127.0.0.1/feed')).status_code==400
    assert client.post(base,headers=auth,json=source('venue','TESTCONF')).status_code==400
    execute("DELETE FROM source_categories WHERE key='venue:COLT'")
    assert client.post(base,headers=auth,json=source('venue','COLT')).status_code==200
    added=next(s for s in client.get(base,headers=auth).json() if s['code']=='COLT')
    assert added['feed_url']=='https://papers.cool/venue/COLT/feed'
    assert added['fetch_enabled'] and added['guest_default']


def test_conference_choices_and_legacy_edits(client,accounts):
    from app.source_catalog import DEFAULT_VENUES,ADDITIONAL_AI_VENUES
    auth=headers(accounts[0]);base='/api/admin/source-categories'
    assert client.get(base+'/venues').status_code==401
    assert client.get(base+'/venues',headers=headers(accounts[1])).status_code==403
    choices=client.get(base+'/venues',headers=auth).json()
    assert [choice['code'] for choice in choices]==DEFAULT_VENUES+ADDITIONAL_AI_VENUES
    assert len(choices)==19 and all(choice['label'] for choice in choices)
    assert client.post(base+'/test',headers=auth,json=source('venue','TESTCONF')).status_code==400
    execute("INSERT INTO source_categories(key,kind,code,label,discipline,created_at) VALUES('venue:OLD','venue','OLD','旧会议','Economics',?)",(now(),))
    changed=client.patch(base+'/venue:OLD',headers=auth,json=source('venue','OLD',label='保留旧会议',guest_default=True))
    assert changed.status_code==200 and changed.json()['discipline']=='Economics'
    assert 'OLD' not in {choice['code'] for choice in choices}

def test_user_standard_proposal_admin_approval_and_deleted_topic_reproposal(client,accounts):
    admin,user=accounts;body={'standard_key':'RA-MATH-060','category_keys':['arxiv:math.CO'],'note':'I study Ramsey bounds'}
    assert client.post('/api/topic-proposals',json=body).status_code==401
    assert client.get('/api/standards?query=Ramsey',headers=headers(user)).status_code==200
    assert client.post('/api/topic-proposals',headers=headers(user),json={**body,'category_keys':['arxiv:cs.fake']}).status_code==400
    r=client.post('/api/topic-proposals',headers=headers(user),json=body);assert r.status_code==200;tid=r.json()['topic_id']
    assert one('SELECT status FROM topics WHERE id=?',(tid,))['status']=='proposed'
    assert client.post('/api/topic-proposals',headers=headers(user),json=body).json()['topic_id']==tid
    assert len(client.get('/api/topic-proposals',headers=headers(user)).json())==1
    assert not client.get('/api/topic-proposals',headers=headers(admin)).json()
    assert client.get(f'/api/admin/topics/{tid}/proposals',headers=headers(admin)).json()[0]['username']=='bob'
    assert tid not in {t['id'] for c in client.get('/api/categories').json() for t in c['topics']}
    entry=catalog()[body['standard_key']]
    active=client.patch(f'/api/admin/topics/{tid}',headers=headers(admin),json={'name_zh':'Ramsey 理论','name_en':entry['label'],'standard_key':entry['key'],'category_keys':body['category_keys'],'status':'active'})
    assert active.status_code==200
    assert client.get('/api/topic-proposals',headers=headers(user)).json()[0]['status']=='active'
    assert client.post('/api/topic-proposals',headers=headers(user),json=body).status_code==409
    client.delete(f'/api/admin/topics/{tid}',headers=headers(admin));client.delete(f'/api/admin/topics/{tid}?permanent=true',headers=headers(admin))
    assert client.post('/api/topic-proposals',headers=headers(user),json=body).status_code==200

def test_fresh_database_has_no_sources_or_preset_topics_even_after_restart(tmp_path,monkeypatch):
    monkeypatch.setenv('DATA_DIR',str(tmp_path));monkeypatch.setenv('MODEL_KEY_FILE',str(tmp_path/'keys'));monkeypatch.setenv('MODEL_ENCRYPTION_KEY','');monkeypatch.setenv('EMBEDDING_DIM','4');settings.cache_clear()
    try:
        init_db();assert not rows('SELECT * FROM topics') and not rows('SELECT * FROM source_categories')
        from app.source_catalog import sources_to_fetch,registry,save_source,SourceInput
        assert not sources_to_fetch('arxiv') and not sources_to_fetch('venue') and not registry(guest=True)
        init_db();assert not rows('SELECT * FROM topics') and not rows('SELECT * FROM source_categories')
        save_source(SourceInput(kind='venue',code='ACL',label='ACL'))
        save_source(SourceInput(kind='arxiv',code='math.CO',label='组合数学'))
        init_db()
        sources=rows('SELECT * FROM source_categories')
        assert {s['key'] for s in sources}=={'venue:ACL','arxiv:math.CO'}
        assert all(s['fetch_enabled'] and s['guest_default'] for s in sources)
    finally:settings.cache_clear();invalidate()


def test_ai_venue_upgrade_preserves_current_settings_and_deletions(client):
    # Simulate the old catalog, including an administrator-added lowercase code.
    from app.source_catalog import ADDITIONAL_AI_VENUES
    execute("DELETE FROM app_migrations WHERE name='ai_venue_catalog_v1'")
    for code in ADDITIONAL_AI_VENUES:execute('DELETE FROM source_categories WHERE key=?',('venue:'+code,))
    execute("UPDATE source_categories SET fetch_enabled=1,guest_default=1,label='我的会议' WHERE key='venue:ICML'")
    execute("INSERT INTO source_categories(key,kind,code,label,fetch_enabled,guest_default,sort_order,discipline,created_at) VALUES('venue:acl','venue','acl','自定义 ACL',1,1,99,'Computer Science',?)",(now(),))
    before=rows('SELECT * FROM source_categories ORDER BY key')
    init_db()
    for original in before:assert one('SELECT * FROM source_categories WHERE key=?',(original['key'],))==original
    assert not one("SELECT key FROM source_categories WHERE key='venue:ACL'")
    assert len(rows("SELECT key FROM source_categories WHERE kind='venue' AND code=? COLLATE NOCASE",('acl',)))==1
    new=[s for s in rows('SELECT * FROM source_categories') if s['key'] not in {r['key'] for r in before}]
    assert len(new)==14 and all(not s['fetch_enabled'] and not s['guest_default'] for s in new)
    execute("DELETE FROM source_categories WHERE key='venue:COLT'")
    init_db()
    assert not one("SELECT key FROM source_categories WHERE key='venue:COLT'")
