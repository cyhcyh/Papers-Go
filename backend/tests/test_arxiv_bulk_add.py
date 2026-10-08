import pytest
from app.source_catalog import official_categories
from app.db import one,rows,execute
from .conftest import headers

URL='/api/admin/source-categories/arxiv/batch'

def test_math_discipline_batch_add_skips_existing_and_defaults_on(client,accounts):
    codes=[c['code'] for c in official_categories() if c['group']=='Mathematics']
    before=one("SELECT * FROM source_categories WHERE key='arxiv:math.CO'")
    response=client.post(URL,headers=headers(accounts[0]),json={'codes':codes})
    assert response.status_code==200,response.text
    result=response.json();assert len(result['created'])==31 and result['skipped']==['arxiv:math.CO']
    assert one("SELECT * FROM source_categories WHERE key='arxiv:math.CO'")==before
    stored=rows("SELECT * FROM source_categories WHERE discipline='Mathematics'")
    assert {s['code'] for s in stored}==set(codes)
    assert all(s['fetch_enabled'] and s['guest_default'] for s in stored if s['key']!='arxiv:math.CO')
    assert next(s for s in stored if s['code']=='math.NT')['label']=='数论'
    assert len([s for s in client.get('/api/categories').json() if s['discipline']=='Mathematics'])==32
    assert not rows('SELECT id FROM pipeline_commands')

def test_batch_add_all_disciplines_is_idempotent_without_overwriting(client,accounts):
    codes=[c['code'] for c in official_categories()];auth=headers(accounts[0])
    originals=rows('SELECT * FROM source_categories')
    result=client.post(URL,headers=auth,json={'codes':codes,'fetch_enabled':True,'guest_default':True,'sort_order':250}).json()
    assert len(result['created'])==146 and len(result['skipped'])==9
    assert one("SELECT COUNT(*) n FROM source_categories WHERE kind='arxiv'")['n']==155
    assert len({s['discipline'] for s in rows("SELECT discipline FROM source_categories WHERE kind='arxiv'")})==8
    assert all(one('SELECT * FROM source_categories WHERE key=?',(s['key'],))==s for s in originals)
    quantum=one("SELECT * FROM source_categories WHERE key='arxiv:quant-ph'")
    assert quantum['fetch_enabled']==1 and quantum['guest_default']==1 and quantum['sort_order']==250
    second=client.post(URL,headers=auth,json={'codes':[codes[0],*codes,codes[0]]}).json()
    assert not second['created'] and len(second['skipped'])==155
    assert one("SELECT * FROM source_categories WHERE key='arxiv:quant-ph'")==quantum
    assert one("SELECT COUNT(*) n FROM source_categories WHERE kind='venue'")['n']==19

@pytest.mark.parametrize('codes',[['math.NT','cs.invalid'],['math.nt'],['venue:ICML']])
def test_invalid_batch_does_not_partially_add(client,accounts,codes):
    before=rows('SELECT * FROM source_categories')
    response=client.post(URL,headers=headers(accounts[0]),json={'codes':codes})
    assert response.status_code==400
    assert rows('SELECT * FROM source_categories')==before

def test_batch_add_permissions_and_options(client,accounts):
    body={'codes':['math.NT','econ.EM','quant-ph','math.NT'],'fetch_enabled':False,'guest_default':False}
    assert client.post(URL,json=body).status_code==401
    assert client.post(URL,headers=headers(accounts[1]),json=body).status_code==403
    assert client.post(URL,headers=headers(accounts[0]),json={'codes':[]}).status_code==422
    assert client.post(URL,headers=headers(accounts[0]),json={**body,'sort_order':10001}).status_code==422
    result=client.post(URL,headers=headers(accounts[0]),json=body).json()
    assert result['created']==['arxiv:math.NT','arxiv:econ.EM','arxiv:quant-ph']
    assert not result['skipped']
    quantum=one("SELECT fetch_enabled,guest_default FROM source_categories WHERE key='arxiv:quant-ph'")
    assert not quantum['fetch_enabled'] and not quantum['guest_default']

def test_existing_case_variant_is_skipped_and_cannot_override_settings(client,accounts):
    execute("UPDATE source_categories SET key='arxiv:CS.AI',label='自定义名称',sort_order=900 WHERE key='arxiv:cs.AI'")
    before=one("SELECT * FROM source_categories WHERE key='arxiv:CS.AI'")
    result=client.post(URL,headers=headers(accounts[0]),json={'codes':['cs.AI','cs.AI'],'fetch_enabled':False}).json()
    assert result=={'created':[],'skipped':['arxiv:cs.AI']}
    assert one("SELECT * FROM source_categories WHERE key='arxiv:CS.AI'")==before
