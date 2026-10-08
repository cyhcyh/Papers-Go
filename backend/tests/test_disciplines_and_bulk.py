import json
from unittest.mock import Mock
import pytest
from app.config import now,today,settings
from app.db import connect,execute,one,rows,dumps,pack
from app.disciplines import DISCIPLINES,ID_PREFIXES
from app.source_catalog import official_categories,initialize,invalidate
from app.standard_topics import catalog,queue_new_topic
from app.interest.profile import put_profile,current
from app.catalog import CategorySelection,validate_selection
from app.pipeline.topic_decision import predict_topic,NewTopic
from app.llm import runtime
from .conftest import headers

def spelling(i):
    return 'entry'+''.join(chr(97+(i//power)%26) for power in (676,26,1))

def add_topics(count,status='proposed',discipline='Physics'):
    with connect() as db:
        return [db.execute('INSERT INTO topics(name_zh,name_en,status,created_at,category_keys,discipline,description) VALUES(?,?,?,?,?,?,?)',
                ('新方向'+str(i),spelling(i),status,now(),dumps(['arxiv:cs.AI']),discipline,
                 'An independent scientific direction studying well defined research questions through mathematical analysis and systematic experiments.')).lastrowid for i in range(count)]

@pytest.mark.parametrize('discipline',DISCIPLINES)
def test_admin_can_create_each_discipline_and_edit_without_changing_id(client,accounts,discipline):
    auth=headers(accounts[0]);body={'name_zh':'新研究方向','name_en':'Independent Research Direction','discipline':discipline,
                                  'description':'Studies a mature community of related scientific problems.','category_keys':['arxiv:cs.AI']}
    response=client.post('/api/admin/topics',headers=auth,json=body)
    assert response.status_code==200,response.text
    ident=response.json()['id'];original=one('SELECT * FROM topics WHERE id=?',(ident,))
    assert original['standard_key'].startswith('RA-LOCAL-')
    assert client.patch(f'/api/admin/topics/{ident}',headers=auth,json={**body,'discipline':'Physics','name_zh':'更新方向'}).status_code==200
    assert one('SELECT standard_key FROM topics WHERE id=?',(ident,))['standard_key']==original['standard_key']
    assert catalog()[original['standard_key']]['discipline']=='Physics'
    assert client.post('/api/admin/topics',headers=auth,json={**body,'discipline':'Invented Discipline'}).status_code==422
    assert all(set(area)=={'id','discipline','name','description'} for area in rows('SELECT * FROM research_areas'))

def test_official_catalog_grouping_search_names_and_source_discipline(client,accounts):
    auth=headers(accounts[0]);base='/api/admin/source-categories'
    official=client.get(base+'/catalog',headers=auth).json()
    assert len(official)==155 and {s['group'] for s in official}==set(DISCIPLINES)
    assert all(s['label'] and s['label_zh'] for s in official)
    assert next(s for s in official if s['code']=='quant-ph')['label_zh']=='量子物理'
    initial=one('SELECT COUNT(*) n FROM source_categories')['n']
    added=client.post(base,headers=auth,json={'kind':'arxiv','code':'quant-ph','label':'量子物理','discipline':'Computer Science','fetch_enabled':False,'guest_default':False})
    assert added.status_code==200 and added.json()['discipline']=='Physics'
    assert one('SELECT COUNT(*) n FROM source_categories')['n']==initial+1
    assert next(s for s in client.get('/api/categories').json() if s['code']=='quant-ph')['discipline']=='Physics'
    execute("DELETE FROM source_categories WHERE key='venue:COLT'")
    venue={'kind':'venue','code':'COLT','label':'Learning Theory','discipline':'Economics','fetch_enabled':False}
    assert client.post(base,headers=auth,json=venue).status_code==200
    omitted={k:v for k,v in venue.items() if k!='discipline'}
    assert client.patch(base+'/venue:COLT',headers=auth,json=omitted).json()['discipline']=='Economics'
    assert client.patch(base+'/venue:COLT',headers=auth,json={**venue,'discipline':'Statistics'}).json()['discipline']=='Statistics'

def test_existing_sources_backfill_once_and_preserve_venue_edits(client):
    with connect() as db:
        db.execute('ALTER TABLE source_categories DROP COLUMN discipline')
        initialize(db)
        assert db.execute("SELECT discipline FROM source_categories WHERE code='stat.ML'").fetchone()[0]=='Statistics'
        db.execute("UPDATE source_categories SET discipline='Economics' WHERE code='ICML'")
        initialize(db)
        assert db.execute("SELECT discipline FROM source_categories WHERE code='ICML'").fetchone()[0]=='Economics'
    invalidate()
    assert next(c for c in client.get('/api/categories').json() if c['code']=='ICML')['discipline']=='Economics'

@pytest.mark.asyncio
async def test_agent_can_propose_other_disciplines_with_source_evidence_without_extra_calls(client,accounts,monkeypatch):
    from app.source_catalog import SourceInput,save_source
    save_source(SourceInput(kind='arxiv',code='quant-ph',label='量子物理',fetch_enabled=False))
    ident=execute('INSERT INTO papers(title,abstract,primary_category,categories,created_at,ingested_date) VALUES(?,?,?,?,?,?)',
                  ('Quantum entanglement','We study quantum entanglement in many body systems.','quant-ph',dumps(['quant-ph','math.MP']),now(),today()))
    paper=one('SELECT * FROM papers WHERE id=?',(ident,));calls=[]
    draft={'discipline':'Physics','name':'Quantum Entanglement','name_zh':'量子纠缠',
           'description':'Quantum entanglement studies correlations between quantum systems and their mathematical structure, physical generation, detection, and uses in information processing.',
           'novelty_reason':'研究量子系统之间的关联与结构，现有目录缺少这一独立方向。'}
    async def complete(feature,messages,**options):
        calls.append((messages,options))
        answer={'standard_key':None,'confidence':.1,'name_zh':'','reason':'现有方向无法表达贡献','no_suitable_topic':True}
        return {**answer,'new_topic':draft} if len(calls)==2 else answer
    monkeypatch.setattr(runtime,'complete',complete)
    result=await predict_topic(paper,list(catalog().values())[:4])
    assert len(calls)==2 and result['new_topic']['discipline']=='Physics'
    content=calls[1][0][0]['content'];assert 'quant-ph' in content and 'Physics' in content and 'math.MP' in content and '主要科学贡献' in content
    assert set(NewTopic.model_json_schema()['properties']['discipline']['enum'])==set(DISCIPLINES)
    with connect() as db:tid,state=queue_new_topic(db,paper,result['new_topic'],.1)
    assert state=='awaiting_approval' and not one("SELECT id FROM research_areas WHERE name='Quantum Entanglement'")
    assert client.post('/api/admin/topics/batch',headers=headers(accounts[0]),json={'ids':[tid],'action':'approve'}).json()['completed']==[tid]
    assert one('SELECT discipline FROM topics WHERE id=?',(tid,))['discipline']=='Physics'
    assert one("SELECT id FROM research_areas WHERE name='Quantum Entanglement'")['id']=='RA-LOCAL-000001'

def test_interest_can_select_all_official_categories(client):
    categories=[{'key':'arxiv:'+e['code'],'topics':[]} for e in official_categories()]
    keys=[c['key'] for c in categories]
    selection=CategorySelection(categories=keys,weights={key:.7 for key in keys})
    assert len(validate_selection(selection,categories)['categories'])==155

def test_batch_reject_cleans_profiles_and_labels_once_with_partial_errors(client,accounts,papers,monkeypatch):
    from app.api import admin
    tids=add_topics(120);uid=accounts[0]['user']['id']
    with connect() as db:
        for i,tid in enumerate(tids):
            pid=db.execute('INSERT INTO papers(title,created_at,ingested_date,classified,classification_state) VALUES(?,?,?,1,?)',
                           (spelling(i),now(),today(),'awaiting_approval')).lastrowid
            db.execute('INSERT INTO topic_pending_papers VALUES(?,?,.9,?)',(pid,tid,now()))
        db.execute("INSERT INTO direction_trends(audience,profile_version,summary,attempted_at) VALUES('guest',1,'保留',?),('user:1',1,'清除',?)",(now(),now()))
    structured={'topic_ids':tids,'category_selection':{'categories':[],'topics':{'arxiv:cs.AI':tids},'weights':{'arxiv:cs.AI':.7}}}
    put_profile(uid,'长期兴趣保留',structured,'manual',pack([1,0,0,0]));before=current(uid)
    cleaner=Mock(wraps=admin.clean_profile_references);monkeypatch.setattr(admin,'clean_profile_references',cleaner)
    existing=one("SELECT id FROM topics WHERE status='active'")['id']
    response=client.post('/api/admin/topics/batch',headers=headers(accounts[0]),json={'ids':[*tids,tids[0],existing,999999],'action':'reject'})
    assert response.status_code==200,response.text
    result=response.json();assert result['completed']==tids and {r['id'] for r in result['failed']}=={existing,999999}
    assert cleaner.call_count==1 and cleaner.call_args.args[1]==set(tids)
    profile=current(uid);assert profile['version']==before['version']+1 and profile['embedding']==before['embedding'] and profile['content']==before['content']
    assert json.loads(profile['structured'])=={'topic_ids':[],'category_selection':{'categories':[],'topics':{},'weights':{}}}
    assert not rows('SELECT * FROM topic_pending_papers')
    assert one("SELECT summary FROM direction_trends WHERE audience='guest'")['summary']=='保留'
    assert one("SELECT summary FROM direction_trends WHERE audience='user:1'")['summary']==''
    assert len(rows("SELECT id FROM papers WHERE classification_state='pending'"))>=120
    assert one('SELECT COUNT(*) n FROM papers')['n']==123

def test_batch_approval_persists_locally_and_rolls_back_invalid_item(client,accounts,monkeypatch):
    from app.api import admin
    tids=add_topics(3)
    execute("UPDATE topics SET description='' WHERE id=?",(tids[1],))
    result=client.post('/api/admin/topics/batch',headers=headers(accounts[0]),json={'ids':tids,'action':'approve'}).json()
    assert result['completed']==[tids[0],tids[2]] and result['failed'][0]['id']==tids[1]
    assert not (settings().data_dir/'research_areas.json').exists()
    assert one('SELECT status,standard_key FROM topics WHERE id=?',(tids[1],))=={'status':'proposed','standard_key':None}
    assert one("SELECT COUNT(*) n FROM research_areas WHERE discipline='Physics'")['n']==104
    assert sorted(r['id'] for r in rows("SELECT id FROM research_areas WHERE discipline='Physics' AND id LIKE 'RA-LOCAL-%'"))[-2:]==['RA-LOCAL-000001','RA-LOCAL-000002']

def test_all_topic_selection_over_500_and_permanent_deletion_releases_ban(client,accounts):
    from app.standard_topics import queue_or_assign
    tids=add_topics(601,'disabled');auth=headers(accounts[0])
    assert client.post('/api/admin/topics/batch',headers=headers(accounts[1]),json={'ids':tids,'action':'delete'}).status_code==403
    assert client.post('/api/admin/topics/batch',headers=auth,json={'ids':tids,'action':'delete'}).json()['completed']==tids
    assert not rows("SELECT id FROM topics WHERE name_en LIKE 'entry%'")
    entry=next(e for e in catalog().values() if e['label']=='Ramsey Theory')
    ident=execute('INSERT INTO topics(name_zh,name_en,status,created_at,category_keys,standard_key,discipline,description) VALUES(?,?,?,?,?,?,?,?)',
                  ('Ramsey理论',entry['label'],'disabled',now(),dumps(['arxiv:math.CO']),entry['key'],entry['discipline'],entry['description']))
    assert client.post('/api/admin/topics/batch',headers=auth,json={'ids':[ident],'action':'delete'}).json()['completed']==[ident]
    pid=execute('INSERT INTO papers(title,primary_category,created_at,ingested_date) VALUES(?,?,?,?)',('Ramsey Graphs','math.CO',now(),today()))
    with connect() as db:
        _,state=queue_or_assign(db,one('SELECT * FROM papers WHERE id=?',(pid,)),entry,.9)
    assert state=='awaiting_approval'

def test_all_users_sources_and_sessions_are_not_truncated_at_500(client,accounts,monkeypatch):
    from app import user_management
    stopped=[]
    async def stop(uid):stopped.append(uid)
    monkeypatch.setattr(user_management,'stop_personal_tasks',stop)
    uid=accounts[1]['user']['id'];auth=headers(accounts[0]);ids=[];keys=[];sessions=[]
    with connect() as db:
        for i in range(601):
            ids.append(db.execute('INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)',(spelling(i),'unused hash',now())).lastrowid)
            key='venue:CONF'+str(i);keys.append(key)
            db.execute('INSERT INTO source_categories(key,kind,code,label,created_at,discipline) VALUES(?,?,?,?,?,?)',(key,'venue','CONF'+str(i),spelling(i),now(),'Computer Science'))
            sessions.append(db.execute('INSERT INTO chat_sessions(user_id,title,created_at) VALUES(?,?,?)',(uid,spelling(i),now())).lastrowid)
        foreign=db.execute('INSERT INTO chat_sessions(user_id,title,created_at) VALUES(?,?,?)',(accounts[0]['user']['id'],'Other user',now())).lastrowid
    assert client.post('/api/admin/users/batch/disable',headers=auth,json={'ids':[*ids,accounts[0]['user']['id']]}).status_code==400
    assert one('SELECT SUM(disabled) n FROM users')['n']==0
    response=client.post('/api/admin/users/batch/disable',headers=auth,json={'ids':ids})
    assert response.status_code==200 and len(response.json()['updated'])==601 and len(stopped)==601
    response=client.patch('/api/admin/source-categories/batch',headers=auth,json={'keys':keys,'fetch_enabled':False})
    assert response.status_code==200 and len(response.json()['updated'])==601
    assert one("SELECT COUNT(*) n FROM source_categories WHERE key LIKE 'venue:CONF%' AND fetch_enabled=0")['n']==601
    assert client.post('/api/chat/sessions/batch-delete',headers=headers(accounts[1]),json={'ids':[*sessions,foreign]}).status_code==404
    assert one('SELECT COUNT(*) n FROM chat_sessions')['n']==602
    response=client.post('/api/chat/sessions/batch-delete',headers=headers(accounts[1]),json={'ids':sessions})
    assert response.status_code==200 and len(response.json()['deleted'])==601
    assert one('SELECT id FROM chat_sessions')=={'id':foreign}
