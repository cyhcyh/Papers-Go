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
