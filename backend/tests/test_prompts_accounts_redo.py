from .vector_helpers import vector_rows,embedding
import asyncio
import json
import pytest
from app import prompts
from app.config import now
from app.db import execute,one,rows,dumps,unpack
from app.llm import runtime as models
from app.pipeline import redo
from .conftest import headers


def test_prompts_admin_only_persistent_override_and_task_snapshot(client,accounts):
    admin,user=accounts
    assert client.get('/api/admin/prompts').status_code==401
    assert client.get('/api/admin/prompts',headers=headers(user)).status_code==403
    listed=client.get('/api/admin/prompts',headers=headers(admin)).json()
    assert all(p['text'] for p in listed)
    assert not any(p['id']=='quality' for p in listed)
    assert [p['id'] for p in client.get('/api/admin/skills',headers=headers(admin)).json()]==['quality']
    default=prompts.get('chat')
    with models.model_snapshot():
        assert client.put('/api/admin/prompts/chat',headers=headers(admin),json={'text':'新的研究对话指令'}).status_code==200
        assert prompts.get('chat')==default
    assert prompts.get('chat')=='新的研究对话指令'
    assert json.loads(one("SELECT value FROM app_settings WHERE name='prompts'")['value'])['chat']=='新的研究对话指令'
    assert client.put('/api/admin/prompts/chat',headers=headers(admin),json={'text':'  '}).status_code==400
    assert client.put('/api/admin/prompts/missing',headers=headers(admin),json={'text':'x'}).status_code==404
    restored=client.put('/api/admin/prompts/chat',headers=headers(admin),json={'text':default}).json()
    assert not restored['customized'] and prompts.get('chat')==default


def test_role_changes_revoke_old_tokens_and_do_not_change_entry(client,accounts):
    admin,user=accounts;auth=headers(admin);uid=user['user']['id']
    entry=client.get('/api/admin/site',headers=auth).json()['admin_path']
    assert client.patch(f'/api/admin/users/{uid}',headers=headers(user),json={'is_admin':True}).status_code==403
    assert client.patch(f'/api/admin/users/{uid}',headers=auth,json={'is_admin':True}).json()['user']['is_admin']
    assert client.get('/api/me',headers=headers(user)).status_code==401
    promoted=client.post('/api/auth/login',json={'username':'bob','password':'secret123'}).json()
    assert client.get('/api/admin/prompts',headers=headers(promoted)).status_code==200
    assert client.get('/api/admin/site',headers=auth).json()['admin_path']==entry
    assert client.patch(f'/api/admin/users/{admin["user"]["id"]}',headers=auth,json={'is_admin':False}).status_code==400
    assert client.patch(f'/api/admin/users/{uid}',headers=auth,json={'is_admin':False}).status_code==200
    assert client.get('/api/admin/prompts',headers=headers(promoted)).status_code==401
    downgraded=client.post('/api/auth/login',json={'username':'bob','password':'secret123'}).json()
    assert client.get('/api/admin/prompts',headers=headers(downgraded)).status_code==403


def test_batch_users_validate_entire_selection_and_clean_personal_data(client,accounts,papers):
    admin,user=accounts;uid=user['user']['id'];auth=headers(admin)
    other=client.post('/api/auth/register',json={'username':'charlie','password':'secret123'}).json();oid=other['user']['id']
    execute('INSERT INTO user_paper_state(user_id,paper_id,saved) VALUES(?,?,1)',(uid,papers[0]))
    execute("INSERT INTO watches(user_id,type,value,created_at) VALUES(?,'keyword','hello',?)",(oid,now()))
    ids=[uid,oid]
    assert client.post('/api/admin/users/batch/disable',headers=headers(user),json={'ids':ids}).status_code==403
    assert client.post('/api/admin/users/batch/disable',headers=auth,json={'ids':[uid,admin['user']['id']]}).status_code==400
    assert not one('SELECT disabled FROM users WHERE id=?',(uid,))['disabled']
    assert client.post('/api/admin/users/batch/disable',headers=auth,json={'ids':[uid,99999]}).status_code==409
    assert client.post('/api/admin/users/batch/disable',headers=auth,json={'ids':ids}).status_code==200
    assert client.get('/api/me',headers=headers(user)).status_code==401
    impact=client.post('/api/admin/users/batch/impact',headers=auth,json={'ids':ids}).json()
    assert impact['counts']['paper_states']==1 and impact['counts']['watches']==1
    assert client.request('DELETE','/api/admin/users/batch',headers=auth,json={'ids':ids,'confirm_text':'wrong'}).status_code==400
    assert client.request('DELETE','/api/admin/users/batch',headers=auth,json={'ids':ids,'confirm_text':'删除 2 个用户'}).status_code==200
    assert not rows('SELECT id FROM users WHERE id IN (?,?)',ids)
    assert not rows('SELECT id FROM watches') and not rows('SELECT * FROM user_paper_state')
    assert len(rows('SELECT id FROM papers'))==3


def test_password_requires_matching_confirmation_and_old_password(client,accounts):
    user=accounts[1];auth=headers(user)
    body={'old_password':'secret123','new_password':'newsecret456','confirm_password':'different123'}
    assert client.put('/api/me/password',headers=auth,json=body).status_code==400
    body['confirm_password']=body['new_password'];body['old_password']='badpassword'
    assert client.put('/api/me/password',headers=auth,json=body).status_code==400
    body['old_password']='secret123'
    assert client.put('/api/me/password',headers=auth,json=body).status_code==200
    assert client.get('/api/me',headers=auth).status_code==401
    assert client.post('/api/auth/refresh',json={'refresh_token':user['refresh_token']}).status_code==401
    assert client.post('/api/auth/login',json={'username':'bob','password':'newsecret456'}).status_code==200


def test_redo_preview_scopes_completed_papers_and_guards(client,accounts,papers):
    admin,user=accounts;auth=headers(admin)
    execute('UPDATE papers SET classified=1,scored=1,brief_json=?',(dumps({'title_zh':'旧标题'}),))
    endpoint='/api/admin/jobs/tldr_gen/redo/preview'
    assert client.post(endpoint,headers=headers(user),json={}).status_code==403
    assert client.post(endpoint,headers=auth,json={}).json()['papers']==3
    assert client.post(endpoint,headers=auth,json={'category_key':'arxiv:cs.AI'}).json()['papers']==3
    assert client.post(endpoint,headers=auth,json={'from_date':'2099-01-01'}).json()['papers']==0
    assert client.post(endpoint,headers=auth,json={'from_date':'2026-10-02','to_date':'2026-10-01'}).status_code==400
    assert client.post(endpoint,headers=auth,json={'components':['embedding']}).status_code==400
    assert client.post('/api/admin/jobs/fetch_arxiv/redo/preview',headers=auth,json={}).status_code==404


@pytest.mark.asyncio
async def test_brief_redo_retains_old_result_on_invalid_model_output_and_resumes(client,papers,monkeypatch):
    from app.pipeline.tldr import PaperBrief
    for ident in papers:execute('UPDATE papers SET brief_json=? WHERE id=?',(dumps({'title_zh':'旧解读'}),ident))
    bad=True;calls=[]
    async def complete(feature,messages,**kwargs):
        calls.append(messages[-1]['content'])
        if bad and messages[-1]['content'].startswith('Ramsey'):return PaperBrief.model_validate({'title_zh':''})
        return PaperBrief(title_zh='新标题',problem='问题',contribution='贡献',result='结果')
    monkeypatch.setattr(models,'complete',complete)
    ident=redo.create_run('tldr_gen',redo.RedoOptions())
    with pytest.raises(RuntimeError):await redo.run(ident,'tldr_gen')
    assert redo.state(ident)['completed']==2 and redo.state(ident)['failed']==1
    assert json.loads(one('SELECT brief_json FROM papers WHERE id=?',(papers[1],))['brief_json'])['title_zh']=='旧解读'
    bad=False;before=len(calls);redo.validate_resume(ident,'tldr_gen');await redo.run(ident,'tldr_gen')
    assert len(calls)==before+1 and redo.state(ident)['status']=='completed'
    assert json.loads(one('SELECT brief_json FROM papers WHERE id=?',(papers[1],))['brief_json'])['title_zh']=='新标题'


@pytest.mark.asyncio
async def test_redo_stop_keeps_successes_and_pending_work(client,papers,monkeypatch):
    done=asyncio.Event();blocked=asyncio.Event();calls=[]
    async def process(item):
        calls.append(item['paper_id'])
        if len(calls)==1:done.set();return
        blocked.set();await asyncio.Event().wait()
    monkeypatch.setattr(redo,'process',process)
    ident=redo.create_run('classify',redo.RedoOptions())
    task=asyncio.create_task(redo.run(ident,'classify'));await done.wait();await blocked.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert redo.state(ident)['status']=='stopped' and redo.state(ident)['completed']==1
    assert not rows("SELECT id FROM pipeline_redo_items WHERE run_id=? AND status='running'",(ident,))
    async def resumed(item):calls.append(item['paper_id'])
    monkeypatch.setattr(redo,'process',resumed);redo.validate_resume(ident,'classify');await redo.run(ident,'classify')
    assert calls.count(papers[0])==1 and redo.state(ident)['completed']==3


@pytest.mark.asyncio
async def test_embedding_redo_includes_profiles_and_rejects_wrong_dimensions(client,accounts,papers,monkeypatch):
    from app.interest.profile import put_profile
    profile=put_profile(accounts[1]['user']['id'],'## 核心兴趣（长期）\n- [w:0.7] 图论',{},'manual')
    old=embedding(papers[0])
    async def embed(texts):return [[0.,1.] for _ in texts]
    monkeypatch.setattr(models,'embed',embed)
    options=redo.RedoOptions(components=['embedding','profile_embedding'])
    assert redo.preview('build_vectors',options)['profiles']==1
    ident=redo.create_run('build_vectors',options)
    with pytest.raises(RuntimeError):await redo.run(ident,'build_vectors')
    assert embedding(papers[0])==old
    assert one('SELECT embedding FROM interest_profile WHERE id=?',(profile['id'],))['embedding'] is None
    async def good(texts):return [[0.,1.,0.,0.] for _ in texts]
    monkeypatch.setattr(models,'embed',good);await redo.run(ident,'build_vectors')
    assert unpack(embedding(papers[0]))==[0.,1.,0.,0.]
    assert unpack(one('SELECT embedding FROM interest_profile WHERE id=?',(profile['id'],))['embedding'])==[0.,1.,0.,0.]
    execute("UPDATE pipeline_redo_runs SET status='stopped' WHERE id=?",(ident,))
    monkeypatch.setattr(models,'embedding_identity',lambda config:('cloud','new','model',4))
    from fastapi import HTTPException
    with pytest.raises(HTTPException):redo.validate_resume(ident,'build_vectors')


@pytest.mark.asyncio
async def test_quality_only_redo_does_not_touch_vectors_and_retains_score_on_failure(client,papers,monkeypatch):
    from app.pipeline import embed
    original=one('SELECT embedding,quality_score FROM papers WHERE id=?',(papers[0],))
    async def no_pdf(*args,**kwargs):return None
    async def invalid(*args,**kwargs):return {'novelty':999,'rigor':99,'summary':'bad'}
    monkeypatch.setattr(embed.fulltext_cache,'get',lambda ident:None);monkeypatch.setattr(models,'complete',invalid)
    ident=redo.create_run('assess_quality',redo.RedoOptions())
    with pytest.raises(RuntimeError):await redo.run(ident,'assess_quality')
    assert one('SELECT embedding,quality_score FROM papers WHERE id=?',(papers[0],))==original
    assert redo.state(ident)['total']==3


@pytest.mark.asyncio
async def test_stopping_queued_redo_allows_resume_without_modifying_old_results(client,papers,monkeypatch):
    from app import scheduler
    monkeypatch.setattr(scheduler,'_pipeline_lock',asyncio.Lock())
    ident=redo.create_run('tldr_gen',redo.RedoOptions())
    async with scheduler._pipeline_lock:
        task=scheduler.start_manual('tldr_gen',redo_id=ident)
        await asyncio.sleep(0)
        assert 'tldr_gen' in scheduler.job_state()['queued']
        await scheduler.stop_jobs('tldr_gen')
        await asyncio.gather(task,return_exceptions=True)
    assert redo.state(ident)['status']=='stopped'
    assert redo.state(ident)['completed']==0
    redo.validate_resume(ident,'tldr_gen')


def test_worker_command_keeps_redo_identity_and_marks_queue_busy(client,monkeypatch):
    from fastapi import HTTPException
    from app.config import settings
    from app.pipeline_control import queue_command,publish_state,public_state
    monkeypatch.setattr(settings(),'pipeline_mode','external')
    publish_state({'pid':123,'busy':False,'active':[],'queued':[],'pipeline':False,'stopping':False})
    ident=queue_command('redo','classify',42)
    assert one('SELECT redo_id FROM pipeline_commands WHERE id=?',(ident,))['redo_id']==42
    assert public_state()['busy'] and 'classify' in public_state()['queued']
    with pytest.raises(HTTPException):queue_command('start','tldr_gen')
