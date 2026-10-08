import copy
import json
import pytest
from app import prompts, recommendation_settings
from app.db import one, execute, dumps
from app.interest.profile import put_profile
from app.llm import runtime as models
from app.pipeline.score import ranking_key, ranked_page, scoring_context, scored_papers
from .conftest import headers


def test_recommendation_permissions_and_preserve_other_settings(client, accounts):
    admin, user = accounts
    path='/api/admin/recommendation'
    assert client.get(path).status_code==401
    assert client.get(path,headers=headers(user)).status_code==403
    assert client.put(path,headers=headers(user),json=recommendation_settings.defaults()).status_code==403
    auth=headers(admin)
    config=client.get(path,headers=auth).json()
    assert config['value']==config['defaults']==recommendation_settings.defaults()
    site=client.get('/api/admin/site',headers=auth).json()
    updated={'personal':{'interest':.57,'quality':.3,'diversity':.1,'author':.03},'guest':{'quality':.4,'recency':.55,'author':.05}}
    assert client.put(path,headers=auth,json=updated).json()['value']==updated
    assert client.get('/api/admin/site',headers=auth).json()=={**site,'recommendation':updated}
    # A previously loaded basic form must not reset subsequently saved weights.
    assert client.put('/api/admin/site',headers=auth,json=site).status_code==200
    assert client.get(path,headers=auth).json()['value']==updated
    assert client.get('/api/site').json().keys()=={'name','description','logo_url','favicon_url'}
    assert client.put(path,headers=auth,json=config['defaults']).json()['value']==config['defaults']


@pytest.mark.parametrize('key,value', [('interest',-.1),('quality',1.1),('diversity',0),('interest',None)])
def test_recommendation_invalid_weights_do_not_write(client,accounts,key,value):
    before=recommendation_settings.configuration()
    invalid=copy.deepcopy(before);invalid['personal'][key]=value
    assert client.put('/api/admin/recommendation',headers=headers(accounts[0]),json=invalid).status_code==422
    assert recommendation_settings.configuration()==before


def test_guest_sum_and_empty_configuration_rejected(client,accounts):
    auth=headers(accounts[0]);body=recommendation_settings.defaults()
    body['guest']={'quality':.8,'recency':.3}
    assert client.put('/api/admin/recommendation',headers=auth,json=body).status_code==422
    assert client.put('/api/admin/recommendation',headers=auth,json={}).status_code==422


def test_saved_weights_replace_warm_guest_and_personal_caches(client,accounts,papers):
    auth=headers(accounts[0]);uid=accounts[1]['user']['id']
    put_profile(uid,'',{'category_selection':{'categories':['arxiv:cs.AI'],'topics':{}}},'test')
    execute('UPDATE papers SET quality_score=20 WHERE id=?',(papers[0],))
    execute('UPDATE papers SET quality_score=90 WHERE id=?',(papers[1],))
    guest_key=ranking_key(None,'recommendations');personal_key=ranking_key(uid,'recommendations')
    guest_before=ranked_page(None,'recommendations',0,20)
    personal_before=ranked_page(uid,'recommendations',0,20)
    context_before=scoring_context(uid)
    new={'personal':{'interest':0,'quality':1,'diversity':0},'guest':{'quality':1,'recency':0}}
    assert client.put('/api/admin/recommendation',headers=auth,json=new).status_code==200
    assert ranking_key(None,'recommendations')!=guest_key
    assert ranking_key(uid,'recommendations')!=personal_key
    assert scoring_context(uid) is not context_before
    for ident,old in [(None,guest_before),(uid,personal_before)]:
        updated=ranked_page(ident,'recommendations',0,20)
        assert updated['items'] and {p['id']:p['score'] for p in updated['items']}!={p['id']:p['score'] for p in old['items']}
        assert all(p['score']==p['quality_score'] for p in updated['items'])


def test_weight_config_is_not_queried_per_paper(client,accounts,papers,monkeypatch):
    uid=accounts[1]['user']['id'];calls=[]
    original=recommendation_settings.configuration
    def counted(db=None):
        calls.append(1);return original(db)
    monkeypatch.setattr(recommendation_settings,'configuration',counted)
    from app.db import rows
    candidates=rows('SELECT * FROM papers')
    assert len(scored_papers(uid,candidates*30))==90
    assert len(calls)==1


def test_skill_preserves_legacy_override_and_freezes_text_with_revision(client,accounts):
    auth=headers(accounts[0]);default=prompts.get('quality')
    execute("INSERT INTO app_settings(name,value,updated_at) VALUES('prompts',?,?)",(dumps({'quality':'旧的自定义评分规范'}),'2026-10-01'))
    # Simulate an upgrade from the pre-package database, before startup migration.
    from app.agent_skills import initialize
    from app.db import connect
    with connect() as db:
        db.execute("DELETE FROM agent_skills WHERE id='quality'")
        initialize(db)
    listed=client.get('/api/admin/skills',headers=auth).json()
    assert listed[0]['text']=='旧的自定义评分规范' and listed[0]['customized']
    assert listed[0]['default']==default
    original_revision=prompts.revision('quality')
    with models.model_snapshot():
        saved=client.put('/api/admin/skills/quality',headers=auth,json={'text':'新的评分规范'})
        assert saved.status_code==200
        assert prompts.get('quality')=='旧的自定义评分规范'
        assert prompts.revision('quality')==original_revision
    assert prompts.get('quality')=='新的评分规范'
    assert prompts.revision('quality')!=original_revision
    assert client.put('/api/admin/skills/quality',headers=auth,json={'text':default}).json()['customized'] is False
    assert 'quality' not in json.loads(one("SELECT value FROM app_settings WHERE name='prompts'")['value'])
    assert prompts.get('quality')==default


def test_skills_admin_only_and_cannot_edit_unbound_prompts(client,accounts):
    path='/api/admin/skills';auth=headers(accounts[0])
    assert client.get(path).status_code==401
    assert client.get(path,headers=headers(accounts[1])).status_code==403
    assert client.put(path+'/quality',headers=headers(accounts[1]),json={'text':'x'}).status_code==403
    assert client.put(path+'/chat',headers=auth,json={'text':'x'}).status_code==404
    assert client.put(path+'/quality',headers=auth,json={'text':' '}).status_code==400
