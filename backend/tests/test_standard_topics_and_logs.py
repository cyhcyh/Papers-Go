import json
from datetime import datetime,timedelta,timezone
import pytest
from app.config import now
from app.db import connect,execute,one,rows,dumps
from app.standard_topics import catalog,candidates,queue_or_assign,migrate
from app.logs import event
from app.interest.profile import current,put_profile
from .conftest import headers


def body(topic):
    return {**topic,'category_keys':json.loads(topic['category_keys']),'status':'active'}


@pytest.mark.asyncio
async def test_standard_proposal_is_deduplicated_and_approved_once(client,accounts,papers,monkeypatch):
    from app.pipeline.classify import classify_paper
    from app.llm import runtime
    for ident in papers[:2]:
        execute("UPDATE papers SET primary_category='math.CO',title='Ramsey theory for hypergraphs',abstract='We prove new Ramsey bounds',categories='[]' WHERE id=?",(ident,))
    async def complete(*args,**kwargs):
        return {'standard_key':'RA-MATH-060','confidence':.95,'name_zh':'Ramsey 理论','reason':'研究 Ramsey 界','evidence':'We prove new Ramsey bounds','no_suitable_topic':False}
    monkeypatch.setattr(runtime,'complete',complete)
    for ident in papers[:2]:await classify_paper(one('SELECT * FROM papers WHERE id=?',(ident,)))
    topic=one("SELECT * FROM topics WHERE standard_key='RA-MATH-060'")
    assert topic['status']=='proposed' and topic['name_en']==catalog()['RA-MATH-060']['label']
    assert one('SELECT COUNT(*) n FROM topic_pending_papers WHERE topic_id=?',(topic['id'],))['n']==2
    assert not rows('SELECT * FROM paper_topics WHERE paper_id IN (?,?)',papers[:2])
    assert client.patch('/api/admin/topics/'+str(topic['id']),json=body(topic),headers=headers(accounts[1])).status_code==403
    assert client.patch('/api/admin/topics/'+str(topic['id']),json=body(topic),headers=headers(accounts[0])).status_code==200
    for ident in papers[:2]:
        assert rows('SELECT topic_id FROM paper_topics WHERE paper_id=?',(ident,))==[{'topic_id':topic['id']}]
        assert one('SELECT classification_state FROM papers WHERE id=?',(ident,))['classification_state']=='ready'
    assert not rows('SELECT * FROM topic_pending_papers WHERE topic_id=?',(topic['id'],))


def test_delete_removes_label_preserves_paper_and_feedback_and_blocks_new_proposal(client,accounts,papers):
    paper=one('SELECT * FROM papers WHERE id=?',(papers[1],))
    execute("UPDATE papers SET primary_category='math.CO',categories='[]' WHERE id=?",(paper['id'],));paper=one('SELECT * FROM papers WHERE id=?',(paper['id'],))
    with connect() as db:ident,_=queue_or_assign(db,paper,catalog()['RA-MATH-060'],.9)
    topic=one('SELECT * FROM topics WHERE id=?',(ident,))
    client.patch('/api/admin/topics/'+str(ident),json=body(topic),headers=headers(accounts[0]))
    uid=accounts[0]['user']['id'];put_profile(uid,'保留科研描述',{'topic_ids':[ident],'category_selection':{'categories':[],'topics':{'arxiv:math.CO':[ident]}}},'test')
    execute('INSERT INTO user_paper_state(user_id,paper_id,liked,saved,seen,updated_at) VALUES(?,?,1,1,0,?)',(uid,paper['id'],now()))
    result=client.delete('/api/admin/topics/'+str(ident),headers=headers(accounts[0]))
    assert result.status_code==200 and result.json()['affected_papers']==1
    assert one('SELECT title FROM papers WHERE id=?',(paper['id'],))['title']==paper['title']
    assert not rows('SELECT * FROM paper_topics WHERE topic_id=?',(ident,))
    assert one('SELECT liked,saved FROM user_paper_state WHERE user_id=? AND paper_id=?',(uid,paper['id']))=={'liked':1,'saved':1}
    assert one('SELECT classified FROM papers WHERE id=?',(paper['id'],))['classified']==0
    assert ident not in json.loads(current(uid)['structured'])['topic_ids']
    assert current(uid)['content']=='保留科研描述'
    assert 'RA-MATH-060' not in {e['key'] for e in candidates(paper,{'RA-MATH-060'})}


def test_migration_starts_with_catalog_without_enabling_all_topics(client):
    assert len(catalog())==388
    assert not rows('SELECT * FROM topics WHERE standard_key IS NOT NULL')
    assert one("SELECT * FROM app_migrations WHERE name='medium_research_areas_v1'")


def test_permanent_delete_releases_standard_key_and_allows_agent_to_propose_again(client,accounts,papers):
    execute("UPDATE papers SET primary_category='math.CO',categories='[]' WHERE id=?",(papers[0],))
    paper=one('SELECT * FROM papers WHERE id=?',(papers[0],))
    entry=catalog()['RA-MATH-060']
    with connect() as db:ident,_=queue_or_assign(db,paper,entry,.9)
    admin=headers(accounts[0]);url='/api/admin/topics/'+str(ident)
    assert client.delete(url+'?permanent=true',headers=admin).status_code==400
    assert client.delete(url,headers=admin).status_code==200
    with pytest.raises(ValueError,match='停用'):
        with connect() as db:queue_or_assign(db,paper,entry,.9)
    assert client.delete(url+'?permanent=true',headers=headers(accounts[1])).status_code==403
    result=client.delete(url+'?permanent=true',headers=admin)
    assert result.status_code==200 and result.json()['deleted'] is True
    assert not one('SELECT id FROM topics WHERE standard_key=?',(entry['key'],))
    with connect() as db:new_id,state=queue_or_assign(db,paper,entry,.9)
    assert state=='awaiting_approval'
    assert one('SELECT status FROM topics WHERE id=?',(new_id,))['status']=='proposed'
    assert one('SELECT topic_id FROM topic_pending_papers WHERE paper_id=?',(paper['id'],))['topic_id']==new_id
    assert not rows('SELECT * FROM paper_topics WHERE paper_id=?',(paper['id'],))


def test_permanent_delete_of_archived_legacy_removes_references_but_keeps_papers(client,accounts,papers):
    execute("UPDATE topics SET status='legacy' WHERE id=3")
    execute('UPDATE topics SET parent_id=3 WHERE id=4')
    uid=accounts[0]['user']['id']
    put_profile(uid,'保留研究描述',{'topic_ids':[3],'category_selection':{'categories':[],'topics':{'arxiv:cs.AI':[3]}}},'test')
    before=rows('SELECT id,title FROM papers ORDER BY id')
    result=client.delete('/api/admin/topics/3?permanent=true',headers=headers(accounts[0]))
    assert result.status_code==200
    assert not one('SELECT id FROM topics WHERE id=3')
    assert one('SELECT parent_id FROM topics WHERE id=4')['parent_id'] is None
    assert not rows('SELECT * FROM paper_topics WHERE topic_id=3')
    assert not rows('SELECT * FROM topic_pending_papers WHERE topic_id=3')
    assert rows('SELECT id,title FROM papers ORDER BY id')==before
    assert current(uid)['content']=='保留研究描述'
    assert 3 not in json.loads(current(uid)['structured'])['topic_ids']


def test_admin_can_write_names_and_cross_discipline_sources(client,accounts):
    auth=headers(accounts[0]);entry=catalog()['RA-MATH-060']
    body={'name_zh':'管理员名称','name_en':'Edited Ramsey Direction','discipline':entry['discipline'],
          'description':entry['description'],'category_keys':['arxiv:cs.AI'],'standard_key':entry['key']}
    assert client.post('/api/admin/topics',json=body,headers=auth).status_code==200
    assert catalog()[entry['key']]['label']=='Edited Ramsey Direction'


def test_logs_exclude_credentials_and_expire_without_expiring_model_configuration(client,accounts):
    execute("INSERT INTO app_logs(kind,level,message,created_at) VALUES('system','info','old',?)",((datetime.now(timezone.utc)-timedelta(days=31)).isoformat(),))
    event('model','调用完成',model='test',api_key='private-secret',Authorization='Bearer private-token',request_headers={'Authorization':'private-token'},input_tokens=10,error='Bearer private-token')
    from app.logs import cleanup
    cleanup()
    saved=rows('SELECT * FROM app_logs')
    encoded=dumps(saved)
    assert 'private-secret' not in encoded and 'private-token' not in encoded and '"old"' not in encoded
    assert client.get('/api/admin/logs',headers=headers(accounts[1])).status_code==403
    response=client.get('/api/admin/logs',headers=headers(accounts[0]))
    assert response.status_code==200 and response.json()['total']>=1


def test_guest_trend_does_not_schedule_generation(client,monkeypatch):
    import app.api.content as content
    def generate(*args,**kwargs):raise AssertionError('游客不应生成趋势')
    monkeypatch.setattr(content,'request_update',generate)
    assert client.get('/api/trends/summary').json()['status']=='login_required'


@pytest.mark.asyncio
async def test_crosslisted_math_paper_uses_msc_and_out_of_scope_has_no_proposal(client,papers,monkeypatch):
    from app.pipeline.classify import classify_paper
    from app.llm import runtime
    execute("UPDATE papers SET primary_category='quant-ph',categories='[\"math.CO\"]',title='Ramsey theory for graphs',abstract='Ramsey bounds for graphs' WHERE id=?",(papers[0],))
    paper=one('SELECT * FROM papers WHERE id=?',(papers[0],))
    assert any(e['key']=='RA-MATH-060' for e in candidates(paper))
    async def complete(*args,**kwargs):
        return {'standard_key':'RA-MATH-060','confidence':.9,'name_zh':'Ramsey 理论','reason':'研究 Ramsey 界','evidence':'Ramsey bounds for graphs','no_suitable_topic':False}
    monkeypatch.setattr(runtime,'complete',complete)
    await classify_paper(paper)
    assert one('SELECT * FROM topic_pending_papers WHERE paper_id=?',(papers[0],))
    execute("UPDATE papers SET primary_category='quant-ph',categories='[]' WHERE id=?",(papers[1],))
    await classify_paper(one('SELECT * FROM papers WHERE id=?',(papers[1],)))
    assert one('SELECT classification_state FROM papers WHERE id=?',(papers[1],))['classification_state']=='unmatched'
    assert not one('SELECT * FROM topic_pending_papers WHERE paper_id=?',(papers[1],))
    assert not one('SELECT * FROM paper_topics WHERE paper_id=?',(papers[1],))
