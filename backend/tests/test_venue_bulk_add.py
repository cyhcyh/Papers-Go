import pytest
from app.config import now
from app.db import execute, one, rows
from app import source_catalog
from .conftest import headers

URL='/api/admin/source-categories/venue/batch'


def test_venue_batch_saves_independent_settings_and_skips_duplicates(client,accounts):
    auth=headers(accounts[0])
    original=one("SELECT * FROM source_categories WHERE key='venue:ICML'")
    execute("DELETE FROM source_categories WHERE code IN ('COLT','UAI')")
    response=client.post(URL,headers=auth,json={'venues':[
        {'code':'COLT','label':'理论会议','discipline':'Mathematics','fetch_enabled':False,'guest_default':True,'sort_order':5},
        {'code':'UAI','discipline':'Statistics','fetch_enabled':True,'guest_default':False,'sort_order':30},
        {'code':'colt','label':'重复项不会覆盖'},
        {'code':'icml','label':'已有来源不会覆盖','fetch_enabled':False},
    ]})
    assert response.status_code==200,response.text
    assert response.json()=={'created':['venue:COLT','venue:UAI'],'skipped':['venue:ICML']}
    colt=one("SELECT * FROM source_categories WHERE key='venue:COLT'")
    uai=one("SELECT * FROM source_categories WHERE key='venue:UAI'")
    assert (colt['label'],colt['discipline'],colt['fetch_enabled'],colt['guest_default'],colt['sort_order'])==('理论会议','Mathematics',0,1,5)
    assert (uai['label'],uai['discipline'],uai['fetch_enabled'],uai['guest_default'],uai['sort_order'])==('UAI','Statistics',1,0,30)
    assert colt['feed_url']=='https://papers.cool/venue/COLT/feed'
    assert uai['feed_url']=='https://papers.cool/venue/UAI/feed'
    assert one("SELECT * FROM source_categories WHERE key='venue:ICML'")==original
    assert not rows('SELECT id FROM pipeline_commands')
    again=client.post(URL,headers=auth,json={'venues':[{'code':'COLT'},{'code':'UAI'}]})
    assert again.json()=={'created':[],'skipped':['venue:COLT','venue:UAI']}
    assert one("SELECT * FROM source_categories WHERE key='venue:COLT'")==colt
    public={item['key']:item for item in client.get('/api/categories').json()}
    assert public['venue:COLT']['discipline']=='Mathematics' and public['venue:UAI']['discipline']=='Statistics'


def test_venue_catalog_supplies_each_conferences_own_discipline(client,accounts,monkeypatch):
    monkeypatch.setitem(source_catalog.VENUE_CATALOG,'MATHCONF',{'label':'A mathematics conference','discipline':'Mathematics'})
    monkeypatch.setitem(source_catalog.VENUE_CATALOG,'PHYSCONF',{'label':'A physics conference','discipline':'Physics'})
    auth=headers(accounts[0])
    choices=client.get('/api/admin/source-categories/venues',headers=auth).json()
    assert next(item for item in choices if item['code']=='MATHCONF')['discipline']=='Mathematics'
    response=client.post(URL,headers=auth,json={'venues':[{'code':'MATHCONF'},{'code':'PHYSCONF'}]})
    assert response.status_code==200,response.text
    assert one("SELECT discipline FROM source_categories WHERE key='venue:MATHCONF'")['discipline']=='Mathematics'
    assert one("SELECT discipline FROM source_categories WHERE key='venue:PHYSCONF'")['discipline']=='Physics'
    single=client.post('/api/admin/source-categories',headers=auth,json={'kind':'venue','code':'PHYSCONF','label':'Physics'})
    assert single.status_code==409


@pytest.mark.parametrize('invalid',[
    {'code':'UNKNOWNCONF'}, {'code':'cs.AI'}, {'code':'COLT','discipline':'invalid'},
    {'code':'COLT','sort_order':10001}, {'code':'COLT','label':''},
])
def test_invalid_venue_batch_never_partially_saves(client,accounts,invalid):
    execute("DELETE FROM source_categories WHERE key='venue:UAI'")
    before=rows('SELECT * FROM source_categories ORDER BY key')
    response=client.post(URL,headers=headers(accounts[0]),json={'venues':[{'code':'UAI'},invalid]})
    assert response.status_code in (400,422)
    assert rows('SELECT * FROM source_categories ORDER BY key')==before


def test_venue_batch_requires_admin_and_a_selection(client,accounts):
    body={'venues':[{'code':'COLT'}]}
    assert client.post(URL,json=body).status_code==401
    assert client.post(URL,headers=headers(accounts[1]),json=body).status_code==403
    assert client.post(URL,headers=headers(accounts[0]),json={'venues':[]}).status_code==422


def test_deleting_venue_blocks_the_whole_batch(client,accounts):
    execute("DELETE FROM source_categories WHERE key='venue:UAI'")
    ident=execute("INSERT INTO source_deletion_jobs(source_keys,source_info,actor_id,status,total,created_at,updated_at) VALUES('[]','[]',?,'failed',0,?,?)",(accounts[0]['user']['id'],now(),now()))
    execute('INSERT INTO source_deletion_sources VALUES(?,?)',('venue:COLT',ident))
    response=client.post(URL,headers=headers(accounts[0]),json={'venues':[{'code':'UAI'},{'code':'COLT'}]})
    assert response.status_code==409
    assert not one("SELECT * FROM source_categories WHERE key='venue:UAI'")
