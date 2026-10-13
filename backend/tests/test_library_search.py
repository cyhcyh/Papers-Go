import pytest
from app.config import now,today
from app.db import execute,dumps
from .conftest import headers


def test_library_search_uses_personal_scope_and_all_search_fields(client,accounts,papers):
    admin,other=accounts
    client.post('/api/interactions',headers=headers(admin),json={'paper_id':papers[0],'action':'like'})
    client.post('/api/interactions',headers=headers(admin),json={'paper_id':papers[1],'action':'save'})
    client.post('/api/interactions',headers=headers(other),json={'paper_id':papers[2],'action':'like'})
    def ids(kind,query):return [p['id'] for p in client.get('/api/library',headers=headers(admin),params={'type':kind,'query':query}).json()['items']]
    assert ids('like','Memory')==[papers[0]]
    assert ids('like','verified result')==[papers[0]]
    assert ids('save','Other Author')==[papers[1]]
    assert ids('save','Memory')==[]
    assert ids('history','Ramsey')==[papers[1]]
    assert ids('history','Evaluation Protocol')==[]
    assert ids('like','')==[papers[0]]


def test_history_search_finds_unloaded_entries_and_keeps_cursor_filtered(client,accounts):
    admin,other=accounts
    ids=[]
    for index in range(27):
        ident=execute('INSERT INTO papers(title,abstract,authors,created_at,ingested_date) VALUES(?,?,?,?,?)',
                      ('needle '+str(index),'a searchable abstract',dumps(['某位作者']),now(),today()))
        execute('INSERT INTO user_paper_state(user_id,paper_id,seen,last_browsed_at,updated_at) VALUES(?,?,1,?,?)',(admin['user']['id'],ident,now(),now()))
        ids.append(ident)
    foreign=execute('INSERT INTO papers(title,authors,created_at,ingested_date) VALUES(?,?,?,?)',('needle foreign','[]',now(),today()))
    execute('INSERT INTO user_paper_state(user_id,paper_id,seen,last_browsed_at) VALUES(?,?,1,?)',(other['user']['id'],foreign,now()))
    params={'type':'history','query':'某位作者','limit':20}
    first=client.get('/api/library',headers=headers(admin),params=params).json()
    assert len(first['items'])==20 and first['next_cursor']
    params['cursor']=first['next_cursor']
    second=client.get('/api/library',headers=headers(admin),params=params).json()
    assert len(second['items'])==7 and second['next_cursor'] is None
    assert {p['id'] for p in first['items']+second['items']}==set(ids)
    old=client.get('/api/library',headers=headers(admin),params={'type':'history','query':'needle 0'}).json()
    assert [p['id'] for p in old['items']]==[ids[0]]


@pytest.mark.parametrize('kind',['like','save','history'])
def test_library_category_topic_search_intersection_is_personal(client,accounts,papers,kind):
    admin,other=accounts
    # One paper can belong to several sources, but the same topic filter still applies.
    execute("INSERT OR IGNORE INTO paper_categories VALUES(?,'arxiv:math.co')",(papers[1],))
    for ident in papers:
        execute('INSERT INTO user_paper_state(user_id,paper_id,liked,saved,seen,last_browsed_at,updated_at) VALUES(?,?,1,1,1,?,?)',(admin['user']['id'],ident,now(),now()))
    foreign=execute('INSERT INTO papers(title,primary_category,authors,created_at,ingested_date) VALUES(?,?,?,?,?)',('Ramsey foreign','math.CO','[]',now(),today()))
    execute('INSERT INTO paper_topics VALUES(?,?,.9)',(foreign,15))
    execute('INSERT INTO user_paper_state(user_id,paper_id,liked,saved,seen,last_browsed_at,updated_at) VALUES(?,?,1,1,1,?,?)',(other['user']['id'],foreign,now(),now()))
    def ids(**filters):
        response=client.get('/api/library',headers=headers(admin),params={'type':kind,**filters})
        assert response.status_code==200,response.text
        return [p['id'] for p in response.json()['items']]
    assert ids(category='arxiv:math.CO',topic_id=15,query='Ramsey')==[papers[1]]
    assert ids(category='arxiv:math.CO',topic_id=3)==[]
    assert ids(category='arxiv:math.CO',topic_id=15,query='Memory')==[]
    assert set(ids())==set(papers)


def test_filtered_history_cursor_retains_order_and_only_matching_records(client,accounts):
    admin,_=accounts;expected=[]
    for index in range(30):
        category='math.CO' if index%5 else 'cs.AI'
        ident=execute('INSERT INTO papers(title,primary_category,authors,created_at,ingested_date) VALUES(?,?,?,?,?)',('History '+str(index),category,'[]',now(),today()))
        execute('INSERT INTO paper_topics VALUES(?,?,.9)',(ident,15 if index%3 else 3))
        execute('INSERT INTO user_paper_state(user_id,paper_id,seen,last_browsed_at,updated_at) VALUES(?,?,1,?,?)',(admin['user']['id'],ident,f'2026-10-12T{index//2:02d}:{index%2:02d}:00+00:00',now()))
        if index%5 and index%3:expected.insert(0,ident)
    params={'type':'history','category':'arxiv:math.CO','topic_id':15,'limit':5}
    found=[]
    while True:
        response=client.get('/api/library',headers=headers(admin),params=params)
        assert response.status_code==200,response.text
        page=response.json();found.extend(p['id'] for p in page['items'])
        if not page['next_cursor']:break
        params['cursor']=page['next_cursor']
    assert found==expected
    invalid=client.get('/api/library',headers=headers(admin),params={'category':'arxiv:not-real'})
    assert invalid.status_code==400
