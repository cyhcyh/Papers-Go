import json
import pytest
from app.config import now,today,settings
from app.db import execute,one,rows,connect
from app.standard_topics import catalog,queue_or_assign,migrate
from app.pipeline.topic_decision import predict_topic,ClassificationError
from app.pipeline.classify import classify_paper
from app.llm import runtime
from .conftest import headers

def area(name):return next(e for e in catalog().values() if e['label']==name)
def paper(title='Autonomous AI Agents',abstract='We study autonomous agents with persistent goals and tool use.'):
    ident=execute('INSERT INTO papers(title,abstract,primary_category,created_at,ingested_date) VALUES(?,?,?,?,?)',(title,abstract,'cs.AI',now(),today()))
    return one('SELECT * FROM papers WHERE id=?',(ident,))
def decision(key,**extra):
    return {'standard_key':key,'confidence':.9,'name_zh':'强化学习','reason':'研究主要贡献','evidence':'摘要简述','no_suitable_topic':False,**extra}
def draft():
    return {'discipline':'Computer Science','name':'Autonomous AI Agents','name_zh':'自主智能体',
            'description':'Autonomous AI agents study persistent goal directed systems that plan actions, use tools, maintain memory, and adapt their behavior through interaction with environments.',
            'novelty_reason':'围绕自主行动、工具使用与长期目标形成独立研究问题，现有多智能体系统主要研究多个智能体交互。'}
def body(topic,**extra):
    return {'name_zh':topic['name_zh'],'name_en':topic['name_en'],'discipline':topic['discipline'],
            'description':topic['description'],'category_keys':json.loads(topic['category_keys']),**extra}

def test_seed_is_flat_independent_catalog(client):
    values=list(catalog().values());assert len(values)==388
    from app.disciplines import DISCIPLINES
    from app.standard_topics import validate_area,FIELDS
    assert {e['discipline'] for e in values}==set(DISCIPLINES)
    assert any(e['id'].startswith('RA-QB-') for e in values)
    assert any(e['id'].startswith('RA-QF-') for e in values)
    assert all(e['parent'] is None and e['key'].startswith('RA-') for e in values)
    saved=rows('SELECT * FROM research_areas ORDER BY id')
    assert all(set(e)=={'id','discipline','name','description'} for e in saved)
    assert all(validate_area(e)==e for e in saved)
    for prefix,discipline in [('QBIO','Quantitative Biology'),('QFIN','Quantitative Finance')]:
        assert validate_area(dict(zip(FIELDS,[f'RA-{prefix}-001',discipline,'Legacy direction','Existing administrator direction.'])))['id']==f'RA-{prefix}-001'


def test_seed_changes_do_not_overwrite_an_installed_catalog(client,monkeypatch):
    from app.db import init_db
    from app.standard_topics import revision
    from pathlib import Path
    execute("UPDATE research_areas SET description='Administrator description' WHERE id='RA-CS-001'")
    execute("DELETE FROM research_areas WHERE id LIKE 'RA-QB-%'")
    before=rows('SELECT * FROM research_areas ORDER BY id')
    with connect() as db:before_revision=revision(db)
    original=Path.read_text
    def refuse_seed_read(path,*args,**kwargs):
        if path.name=='research_areas.json' and path.parent.name=='resources':
            raise AssertionError('An installed site must not reload the bundled seed')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',refuse_seed_read)
    init_db()
    assert rows('SELECT * FROM research_areas ORDER BY id')==before
    with connect() as db:assert revision(db)==before_revision

@pytest.mark.asyncio
async def test_one_call_no_quote_or_vector_disagreement_review(client,monkeypatch):
    p=paper();e=area('Reinforcement Learning');calls=[]
    async def complete(*args,**kwargs):calls.append(args);return decision(e['key'],confidence=.6)
    monkeypatch.setattr(runtime,'complete',complete)
    from app.pipeline import classify
    async def shortlist(*args,**kwargs):return [e,area('Computer Vision')]
    monkeypatch.setattr(classify,'semantic_candidates',shortlist)
    value=await predict_topic(p,[{**e,'semantic_score':.1},{**area('Computer Vision'),'semantic_score':.99}])
    assert len(calls)==1 and 'needs_review' not in value and value['standard_key']==e['key']
    await classify_paper(p)
    assert one('SELECT classification_state FROM papers WHERE id=?',(p['id'],))['classification_state']=='awaiting_approval'

@pytest.mark.asyncio
async def test_ambiguous_existing_labels_still_classify_once(client,monkeypatch):
    p=paper();e=area('Reinforcement Learning');calls=[]
    async def complete(*args,**kwargs):calls.append(1);return decision(e['key'],confidence=.3)
    monkeypatch.setattr(runtime,'complete',complete)
    assert (await predict_topic(p,[e]))['standard_key']==e['key'];assert len(calls)==1

@pytest.mark.asyncio
async def test_uncertainty_can_recover_outside_initial_candidates(client,monkeypatch):
    p=paper();chosen=area('Bayesian Statistics');calls=[]
    async def complete(feature,messages,**kwargs):
        calls.append(messages[0]['content'])
        if len(calls)==1:return decision(None,confidence=.2,no_suitable_topic=True)
        return {**decision(chosen['key']),'new_topic':None}
    monkeypatch.setattr(runtime,'complete',complete)
    result=await predict_topic(p,[area('Computer Vision')])
    assert result['standard_key']==chosen['key'] and len(calls)==2
    assert chosen['description'] in calls[1] and '完整方向目录' in calls[1]

@pytest.mark.asyncio
async def test_novel_proposal_waits_for_approval_and_appends_schema_once(client,accounts,monkeypatch):
    async def complete(feature,messages,**kwargs):
        if 'new_topic' not in kwargs['schema']['properties']:return decision(None,confidence=.1,no_suitable_topic=True)
        return {**decision(None,confidence=.1,no_suitable_topic=True),'new_topic':draft()}
    monkeypatch.setattr(runtime,'complete',complete)
    p1,p2=paper(),paper()
    await classify_paper(p1);await classify_paper(p2)
    t=one("SELECT * FROM topics WHERE name_en='Autonomous AI Agents'")
    assert t['status']=='proposed' and t['standard_key'] is None
    assert one('SELECT COUNT(*) n FROM topic_pending_papers WHERE topic_id=?',(t['id'],))['n']==2
    assert len(catalog())==388 and not rows('SELECT * FROM paper_topics WHERE paper_id IN (?,?)',(p1['id'],p2['id']))
    url='/api/admin/topics/'+str(t['id'])
    assert client.patch(url,json=body(t,status='active'),headers=headers(accounts[1])).status_code==403
    assert client.patch(url,json=body(t,status='active'),headers=headers(accounts[0])).status_code==200
    approved=one('SELECT * FROM topics WHERE id=?',(t['id'],));key=approved['standard_key'];assert key=='RA-LOCAL-000001'
    assert len(catalog())==389
    assert rows('SELECT paper_id FROM paper_topics WHERE topic_id=? ORDER BY paper_id',(t['id'],))==[{'paper_id':p1['id']},{'paper_id':p2['id']}]
    assert client.patch(url,json=body(approved,status='active'),headers=headers(accounts[0])).status_code==200
    saved=rows('SELECT * FROM research_areas ORDER BY id')
    assert len(saved)==389 and len([a for a in saved if a['id']==key])==1
    assert all(set(a)=={'id','discipline','name','description'} for a in saved)
    assert not (settings().data_dir/'research_areas.json').exists()

def test_admin_free_edit_preserves_identity_and_updates_paper_label(client,accounts):
    auth=headers(accounts[0]);payload={'name_zh':'自定义方向','name_en':'Custom Research Direction','discipline':'Mathematics',
                                    'description':'A stable independent research community studying well defined scientific problems.','category_keys':['arxiv:math.CO']}
    assert client.post('/api/admin/topics',json=payload,headers=headers(accounts[1])).status_code==403
    response=client.post('/api/admin/topics',json=payload,headers=auth);assert response.status_code==200,response.text
    tid=response.json()['id'];before=one('SELECT * FROM topics WHERE id=?',(tid,));key=before['standard_key']
    p=paper();execute('INSERT INTO paper_topics VALUES(?,?,.9)',(p['id'],tid))
    changed={**payload,'name_zh':'修改后的方向','name_en':'Updated Research Direction','discipline':'Computer Science'}
    assert client.patch('/api/admin/topics/'+str(tid),json=changed,headers=auth).status_code==200
    t=one('SELECT * FROM topics WHERE id=?',(tid,));assert t['standard_key']==key
    assert catalog()[key]['label']=='Updated Research Direction'
    assert one('SELECT t.name_zh FROM paper_topics l JOIN topics t ON t.id=l.topic_id WHERE l.paper_id=?',(p['id'],))['name_zh']=='修改后的方向'
    assert client.post('/api/admin/topics',json=changed,headers=auth).status_code==409

def test_disabled_topic_blocks_agent_and_permanent_delete_releases(client,accounts):
    e=area('Ramsey Theory');p=paper()
    with connect() as db:tid,_=queue_or_assign(db,p,e,.9)
    t=one('SELECT * FROM topics WHERE id=?',(tid,));auth=headers(accounts[0]);url='/api/admin/topics/'+str(tid)
    assert client.patch(url,json=body(t,status='active'),headers=auth).status_code==200
    assert client.delete(url,headers=auth).status_code==200
    with pytest.raises(ValueError,match='停用'):
        with connect() as db:queue_or_assign(db,p,e,.9)
    assert client.patch(url,json={**body(t,status='disabled'),'name_zh':'停用后修改'},headers=auth).status_code==200
    assert client.delete(url+'?permanent=true',headers=auth).status_code==200
    with connect() as db:tid2,state=queue_or_assign(db,p,e,.9)
    assert state=='awaiting_approval' and one('SELECT status FROM topics WHERE id=?',(tid2,))['status']=='proposed'

@pytest.mark.asyncio
async def test_proposal_cannot_rename_disabled_old_topic(client,monkeypatch):
    execute("INSERT INTO topics(name_zh,name_en,status,created_at) VALUES('自主智能体','Autonomous AI Agents','disabled',?)",(now(),))
    calls=[]
    async def complete(*args,**kwargs):
        calls.append(args)
        if len(calls)==1:return decision(None,confidence=.1,no_suitable_topic=True)
        return {**decision(None,confidence=.1,no_suitable_topic=True),'new_topic':draft()}
    monkeypatch.setattr(runtime,'complete',complete)
    with pytest.raises(ClassificationError,match='停用'):await classify_paper(paper())
    assert len(calls)==3 and len(rows("SELECT * FROM topics WHERE name_en='Autonomous AI Agents'"))==1

def test_review_endpoints_are_removed(client,accounts):
    assert client.get('/api/admin/classification-reviews',headers=headers(accounts[0])).status_code==404
