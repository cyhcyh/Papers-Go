import json
from pathlib import Path
from types import SimpleNamespace as N
import pytest
from fastapi import HTTPException
from app import agent_skills as skills, prompts
from app.db import connect, one, execute, dumps
from app.config import settings
from app.agent.tools import execute_tool
from .conftest import headers


def payload(name='compare-methods',**extra):
    return {'name':name,'title':'方法对比','description':'比较论文中的方法与实验时使用',
            'text':'先搜索论文，再说明方法差异。\n保持结果简洁。\n',
            'allowed_tools':['search_papers','read_now'],
            'resources':{'references/checklist.md':'问题、方法、实验和限制。'},**extra}


def create(client,account,**extra):
    response=client.post('/api/skills',headers=headers(account),json=payload(**extra))
    assert response.status_code==200,response.text
    return response.json()


def test_standard_package_default_and_legacy_migration_are_exact(client):
    import tomllib
    legacy=tomllib.loads((Path(prompts.__file__).with_name('skills.toml')).read_text(encoding='utf-8'))['quality']['text']
    assert skills.builtin()['text']==legacy==prompts.get('quality')
    text='\n自定义评分内容\r\n$\\alpha$\n'
    with connect() as db:
        db.execute("DELETE FROM agent_skills WHERE id='quality'")
        db.execute("INSERT INTO app_settings VALUES('prompts',?,'old')",(dumps({'quality':text}),))
        skills.initialize(db)
    item=skills.detail('quality',admin=True)
    assert item['text']==text==prompts.get('quality')
    assert skills.parse_document(item['document'])[1]==text
    assert item['default']==legacy
    before=one("SELECT version_id FROM agent_skills WHERE id='quality'")
    with connect() as db:skills.initialize(db)
    assert one("SELECT version_id FROM agent_skills WHERE id='quality'")==before


def test_personal_isolation_and_admin_agent_cannot_edit_system(client,accounts):
    admin,user=accounts;item=create(client,user);ident=item['id']
    assert client.get('/api/skills',headers=headers(admin)).json()['total']==0
    assert client.get('/api/skills/'+ident,headers=headers(admin)).status_code==404
    assert client.get('/api/admin/skills/detail/'+ident,headers=headers(admin)).status_code==404
    assert client.put('/api/skills/'+ident,headers=headers(admin),json=payload()).status_code==404
    assert client.get('/api/admin/skills/catalog',headers=headers(user)).status_code==403
    assert client.get('/api/skills').status_code==401
    assert client.delete('/api/skills/'+ident,headers=headers(admin)).status_code==404
    assert client.delete('/api/admin/skills/detail/quality',headers=headers(admin)).status_code==400
    assert not skills.available(admin['user']['id'])


@pytest.mark.parametrize('extra',[{'name':'../escape'},{'name':'UPPER'},{'allowed_tools':['admin_delete_user']},
    {'resources':{'references/../../escape.md':'bad'}},{'resources':{'scripts/run.py':'print(1)'}},
    {'resources':{'references/a.exe':'x'}},{'resources':{'references/a.md':'x'*24001}},
    {'owner_id':999},{'scope':'shared'},{'document':'not a standard skill'}])
def test_invalid_packages_never_create_records_or_files(client,accounts,extra):
    before=set(skills.root().iterdir())
    response=client.post('/api/skills',headers=headers(accounts[1]),json=payload(**extra))
    assert response.status_code in (400,422)
    assert client.get('/api/skills',headers=headers(accounts[1])).json()['total']==0
    assert set(skills.root().iterdir())==before


def test_versions_source_edit_conflict_restore_and_resources(client,accounts):
    item=create(client,accounts[1]);ident=item['id'];auth=headers(accounts[1]);url='/api/skills/'+ident
    source=skills.document_for('compare-methods','新标题','新的使用条件','新的步骤\n',['search_papers'])
    edited=client.put(url,headers=auth,json=payload(document=source,expected_revision=1)).json()
    assert edited['title']=='新标题' and edited['text']=='新的步骤\n' and edited['revision']==2
    assert client.put(url,headers=auth,json=payload(expected_revision=1)).status_code==409
    restored=client.post(url+'/restore',headers=auth,json={'revision':1}).json()
    assert restored['revision']==3 and restored['document']==item['document']
    assert restored['resources']==item['resources']
    assert [v['revision'] for v in client.get(url+'/versions',headers=auth).json()]==[3,2,1]
    before=list(skills.root().iterdir())
    assert client.post('/api/skills',headers=auth,json=payload()).status_code==409
    assert list(skills.root().iterdir())==before


def test_shared_proposal_snapshot_review_and_ownership(client,accounts):
    admin,user=accounts;item=create(client,user);ident=item['id'];auth=headers(user)
    proposal=client.post('/api/skills/'+ident+'/propose',headers=auth).json();pid=proposal['id']
    assert client.post('/api/skills/'+ident+'/propose',headers=auth).json()['id']==pid
    assert not skills.available(admin['user']['id'])
    assert client.get('/api/skills/'+pid,headers=auth).status_code==404
    assert client.post('/api/admin/skills/detail/'+pid+'/review',headers=auth,json={'approve':True}).status_code==403
    client.put('/api/skills/'+ident,headers=auth,json=payload(text='修改后的个人内容',expected_revision=1))
    pending=client.get('/api/admin/skills/detail/'+pid,headers=headers(admin)).json()
    assert pending['text']==item['text'] and pending['status']=='pending'
    approved=client.post('/api/admin/skills/detail/'+pid+'/review',headers=headers(admin),json={'approve':True}).json()
    assert approved['enabled'] and approved['scope']=='shared' and approved['owner_id'] is None
    assert skills.available(admin['user']['id'])[0]['id']==pid
    assert client.get('/api/skills/'+pid,headers=auth).json()['editable'] is False
    assert client.put('/api/skills/'+pid,headers=auth,json=payload()).status_code==404
    assert client.post('/api/admin/skills/detail/'+pid+'/review',headers=headers(admin),json={'approve':True}).status_code==409


@pytest.mark.asyncio
async def test_agent_creates_activates_and_preserves_confirmation_boundaries(client,accounts):
    user=accounts[1];uid=user['user']['id'];auth=headers(user)
    sid=client.post('/api/chat/sessions',headers=auth,json={}).json()['id']
    item=await execute_tool('create_skill',payload(),uid,sid,None)
    assert item['enabled'] and client.get('/api/skills',headers=auth).json()['total']==1
    assert 'document' not in item
    result=await execute_tool('activate_skill',{'skill_id':item['id']},uid,sid,None)
    assert result['instructions']==payload()['text'] and result['resources']==['references/checklist.md']
    assert await execute_tool('read_skill_resource',{'skill_id':item['id'],'path':'references/checklist.md'},uid,sid,None)=={'path':'references/checklist.md','text':'问题、方法、实验和限制。'}
    with pytest.raises(HTTPException):await execute_tool('activate_skill',{'skill_id':'quality'},uid,sid,None)
    with pytest.raises(HTTPException):await execute_tool('set_skill_enabled',{'skill_id':'quality','enabled':False},uid,sid,None)
    with pytest.raises(ValueError):await execute_tool('create_skill',{**payload(),'user_id':1},uid,sid,None)
    proposal=await execute_tool('update_interest',{'patch':'## 核心兴趣\n- 图论'},uid,sid,None)
    assert proposal['requires_confirmation'] and one('SELECT COUNT(*) n FROM interest_profile WHERE user_id=?',(uid,))['n']==0
    skills.set_enabled(item['id'],False,uid)
    assert skills.session_context(sid,uid)==[] and skills.available(uid)==[]


def test_lazy_resources_and_frozen_skill_versions(client,accounts,monkeypatch):
    user=accounts[1];uid=user['user']['id'];item=create(client,user)
    sid=client.post('/api/chat/sessions',headers=headers(user),json={}).json()['id']
    opened=[];original=Path.read_bytes
    def track(path,*args,**kwargs):opened.append(path);return original(path,*args,**kwargs)
    skills.read_bundle.cache_clear();monkeypatch.setattr(Path,'read_bytes',track)
    with skills.invocation():
        active=skills.activate(item['id'],uid,sid)
        assert not any(p.name=='checklist.md' for p in opened)
        updated=skills.save(item['id'],skills.SkillEdit(**payload(text='新步骤',resources={'references/checklist.md':'新资料'},expected_revision=1)),uid)
        assert skills.session_context(sid,uid)[0]['instructions']==active['instructions']
        assert skills.resource(item['id'],'references/checklist.md',uid,sid)['text']=='问题、方法、实验和限制。'
    assert skills.session_context(sid,uid)[0]['instructions']==updated['text']
    assert skills.resource(item['id'],'references/checklist.md',uid,sid)['text']=='新资料'


@pytest.mark.asyncio
async def test_chat_skill_event_persists_without_answer_and_reloads_context(client,accounts,monkeypatch):
    from app.agent import chat
    uid=accounts[1]['user']['id'];auth=headers(accounts[1]);sid=client.post('/api/chat/sessions',headers=auth,json={}).json()['id']
    calls=[]
    async def stream(messages,tools):
        calls.append(messages)
        if len(calls)==1:
            yield N(content=None,tool_calls=[N(index=0,id='skill-call',function=N(name='create_skill',arguments=dumps(payload())))])
        else:yield N(content='',tool_calls=[])
    monkeypatch.setattr(chat.models,'stream',stream)
    response=[e async for e in chat.chat_events(sid,uid,'保存为技能')]
    assert any('event: skill' in e for e in response) and not any('event: error' in e for e in response)
    saved=client.get(f'/api/chat/sessions/{sid}/messages',headers=auth).json()[-1]
    assert saved['skill_events'][0]['action']=='create_skill' and saved['proposals']==[]


def test_user_deletion_removes_private_bundles_but_preserves_published_shared(client,accounts):
    admin,user=accounts;uid=user['user']['id'];item=create(client,user);ident=item['id']
    pid=skills.propose(ident,uid)['id'];skills.review(pid,True,admin['user']['id'])
    assert client.delete('/api/admin/users/'+str(uid),headers=headers(admin)).status_code==200
    assert not (skills.root()/ident).exists()
    assert one('SELECT id FROM agent_skills WHERE id=?',(pid,))
    assert (skills.root()/pid).exists()


def test_discovery_is_bounded_and_does_not_read_packages(client,accounts,monkeypatch):
    uid=accounts[1]['user']['id']
    for n in range(22):create(client,accounts[1],name='method-'+str(n))
    def no_packages(*args):raise AssertionError('发现技能不应读取正文')
    monkeypatch.setattr(skills,'read_bundle',no_packages)
    assert len(skills.available(uid))==20
    assert len(skills.available(uid,limit=12))==12
    assert len(skills.available(uid,'method-21'))==1
    catalog=client.get('/api/skills?page=2',headers=headers(accounts[1])).json()
    assert len(catalog['items'])==2 and catalog['total']==22 and 'text' not in catalog['items'][0]


def test_source_validation_and_form_edits_preserve_standard_metadata(client,accounts):
    user=accounts[1];auth=headers(user)
    source='---\nname: licensed-method\ndescription: Research comparison\nlicense: MIT\ncompatibility: Website tools\nmetadata:\n  title: 方法比较\n  author: researcher\n---\nOriginal steps\n'
    validated=client.post('/api/skills/validate',headers=auth,json=payload(document=source)).json()
    assert validated['name']=='licensed-method' and validated['text']=='Original steps\n'
    item=client.post('/api/skills',headers=auth,json=validated).json()
    updated=client.put('/api/skills/'+item['id'],headers=auth,json=payload(name='licensed-method',text='Updated steps\n',expected_revision=1)).json()
    meta,body=skills.parse_document(updated['document'])
    assert body=='Updated steps\n' and meta['license']=='MIT' and meta['compatibility']=='Website tools'
    assert meta['metadata']['author']=='researcher'


@pytest.mark.asyncio
async def test_agent_inspects_and_reenables_own_disabled_skill_without_activation(client,accounts,monkeypatch):
    admin,user=accounts;uid=user['user']['id'];aid=admin['user']['id']
    sid=client.post('/api/chat/sessions',headers=headers(user),json={}).json()['id']
    admin_sid=client.post('/api/chat/sessions',headers=headers(admin),json={}).json()['id']
    item=create(client,user);other=create(client,admin,name='other-private-method')
    shared=skills.propose(item['id'],uid)['id'];skills.review(shared,True,aid)
    pending=skills.propose(other['id'],aid)['id']
    skills.set_enabled(item['id'],False,uid)
    skills.set_enabled(shared,False,aid,True)
    opened=[];original=Path.read_bytes
    def track(path,*args,**kwargs):opened.append(path);return original(path,*args,**kwargs)
    skills.read_bundle.cache_clear();monkeypatch.setattr(Path,'read_bytes',track)
    assert await execute_tool('list_skills',{},uid,sid,None)==[]
    found=await execute_tool('list_skills',{'query':'compare-methods','include_disabled':True},uid,sid,None)
    assert [s['id'] for s in found]==[item['id']] and found[0]['enabled']==0
    assert not opened
    latest=await execute_tool('get_skill',{'skill_id':item['id']},uid,sid,None)
    assert latest['text']==item['text'] and latest['revision']==1 and latest['enabled']==0
    assert latest['resources']==['references/checklist.md'] and not any(p.name=='checklist.md' for p in opened)
    assert not one('SELECT 1 FROM chat_session_skills WHERE session_id=?',(sid,))
    assert one('SELECT enabled FROM agent_skills WHERE id=?',(item['id'],))['enabled']==0
    for ident in (other['id'],shared,pending,'quality'):
        with pytest.raises(HTTPException) as error:await execute_tool('get_skill',{'skill_id':ident},uid,sid,None)
        assert error.value.status_code==404
    with pytest.raises(HTTPException):await execute_tool('get_skill',{'skill_id':item['id']},aid,admin_sid,None)
    with pytest.raises(ValueError):await execute_tool('get_skill',{'skill_id':item['id'],'user_id':uid},uid,sid,None)
    with pytest.raises(ValueError):await execute_tool('list_skills',{'include_disabled':'true'},uid,sid,None)
    await execute_tool('set_skill_enabled',{'skill_id':item['id'],'enabled':True},uid,sid,None)
    assert one('SELECT enabled FROM agent_skills WHERE id=?',(item['id'],))['enabled']==1
    assert not one('SELECT 1 FROM chat_session_skills WHERE session_id=?',(sid,))


@pytest.mark.asyncio
async def test_agent_reads_latest_for_merge_without_replacing_running_snapshot(client,accounts):
    user=accounts[1];uid=user['user']['id'];item=create(client,user)
    sid=client.post('/api/chat/sessions',headers=headers(user),json={}).json()['id']
    with skills.invocation():
        active=skills.activate(item['id'],uid,sid)
        updated=skills.save(item['id'],skills.SkillEdit(**payload(text='用户刚修改的新步骤\n',expected_revision=1)),uid)
        latest=await execute_tool('get_skill',{'skill_id':item['id']},uid,sid,None)
        assert latest['text']==updated['text'] and latest['revision']==2
        assert skills.session_context(sid,uid)[0]['instructions']==active['instructions']
        with pytest.raises(HTTPException) as conflict:
            await execute_tool('update_skill',{'skill_id':item['id'],**{k:latest[k] for k in ('name','title','description','text')},'expected_revision':1},uid,sid,None)
        assert conflict.value.status_code==409
        result=await execute_tool('update_skill',{'skill_id':item['id'],**{k:latest[k] for k in ('name','title','description')},'text':latest['text']+'增加结果检查。\n','expected_revision':latest['revision']},uid,sid,None)
        saved=skills.detail(item['id'],uid)
        assert result['revision']==3 and saved['text'].startswith(updated['text'])
        assert saved['resources']==item['resources'] and saved['allowed_tools']==item['allowed_tools']
        assert skills.session_context(sid,uid)[0]['instructions']==active['instructions']
    assert skills.session_context(sid,uid)[0]['instructions']==saved['text']


@pytest.mark.asyncio
async def test_disabling_system_skill_skips_new_jobs_and_freezes_running_call(client,accounts,monkeypatch):
    from app.llm import runtime as models
    from app.pipeline import embed
    def no_model(*args,**kwargs):raise AssertionError('停用后不应调用模型')
    with models.model_snapshot():
        skills.set_enabled('quality',False,accounts[0]['user']['id'],True)
        assert prompts.skill_enabled('quality') is True
    assert prompts.skill_enabled('quality') is False
    monkeypatch.setattr(embed.models,'complete',no_model)
    assert await embed.assess_quality()==0
    from app.pipeline import redo
    with pytest.raises(HTTPException):redo.preview('assess_quality',redo.RedoOptions())
    assert client.post('/api/admin/skills/detail/quality/enabled',headers=headers(accounts[0]),json={'enabled':True}).status_code==200
