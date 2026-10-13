import pytest
from app.config import now,today
from app.db import execute
from .conftest import headers


@pytest.mark.parametrize('kind',['like','save','history'])
def test_independent_multiple_filters_use_or_within_and_between_fields(client,accounts,papers,kind):
    admin,other=accounts
    execute("UPDATE papers SET primary_category='math.CO' WHERE id=?",(papers[1],))
    execute("UPDATE papers SET primary_category='cs.LG' WHERE id=?",(papers[2],))
    for ident in papers:
        execute('INSERT INTO user_paper_state(user_id,paper_id,liked,saved,seen,last_browsed_at,updated_at) VALUES(?,?,1,1,1,?,?)',(admin['user']['id'],ident,now(),now()))
    foreign=execute('INSERT INTO papers(title,primary_category,authors,created_at,ingested_date) VALUES(?,?,?,?,?)',('Foreign memory','cs.AI','[]',now(),today()))
    execute('INSERT INTO paper_topics VALUES(?,?,.9)',(foreign,3))
    execute('INSERT INTO user_paper_state(user_id,paper_id,liked,saved,seen,last_browsed_at) VALUES(?,?,1,1,1,?)',(other['user']['id'],foreign,now()))
    def ids(**filters):
        response=client.get('/api/library',headers=headers(admin),params={'type':kind,**filters})
        assert response.status_code==200,response.text
        return {p['id'] for p in response.json()['items']}
    assert ids(topic_ids=[3,15])=={papers[0],papers[1]}
    assert ids(categories=['arxiv:cs.AI','arxiv:cs.LG'])=={papers[0],papers[2]}
    assert ids(categories=['arxiv:cs.AI','arxiv:math.CO'],topic_ids=[3,15])=={papers[0],papers[1]}
    assert ids(categories=['arxiv:cs.LG'],topic_ids=[3,15])==set()
    assert ids(topic_ids=[3,15],query='Ramsey')=={papers[1]}
    assert ids(category='arxiv:math.CO',topic_id=15)=={papers[1]}
    assert ids(categories=['arxiv:cs.AI','arxiv:cs.AI'],topic_ids=[3,3])=={papers[0]}


def test_topic_picker_includes_active_unassigned_topics_without_counts_or_duplicates(client,accounts):
    ident=execute('INSERT INTO topics(name_zh,name_en,status,category_keys,created_at) VALUES(?,?,?,?,?)',('未归类方向','Unassigned direction','active','[]',now()))
    execute('INSERT INTO topics(name_zh,name_en,status,category_keys,created_at) VALUES(?,?,?,?,?)',('停用方向','Disabled direction','disabled','[]',now()))
    response=client.get('/api/library/topics',headers=headers(accounts[0]))
    assert response.status_code==200
    data=response.json()
    assert len({t['id'] for t in data})==len(data)
    assert any(t['id']==ident for t in data)
    assert not any(t['name_en']=='Disabled direction' for t in data)
    assert all('paper_count' not in t for t in data)
    assert client.get('/api/library/topics').status_code==401


def test_multiselect_history_pages_keep_all_filters_and_newest_first(client,accounts):
    user=accounts[0];expected=[]
    for index in range(31):
        category='cs.AI' if index%2 else 'math.CO'
        ident=execute('INSERT INTO papers(title,primary_category,authors,created_at,ingested_date) VALUES(?,?,?,?,?)',('Paper '+str(index),category,'[]',now(),today()))
        execute('INSERT INTO paper_topics VALUES(?,?,.9)',(ident,3 if index%2 else 15))
        execute('INSERT INTO user_paper_state(user_id,paper_id,seen,last_browsed_at,updated_at) VALUES(?,?,1,?,?)',(user['user']['id'],ident,now(),now()))
        expected.insert(0,ident)
    params={'type':'history','categories':['arxiv:cs.AI','arxiv:math.CO'],'topic_ids':[3,15],'limit':20}
    first=client.get('/api/library',headers=headers(user),params=params).json()
    assert [p['id'] for p in first['items']]==expected[:20]
    second=client.get('/api/library',headers=headers(user),params={**params,'cursor':first['next_cursor']}).json()
    assert [p['id'] for p in second['items']]==expected[20:] and second['next_cursor'] is None
    assert client.get('/api/library',headers=headers(user),params={'topic_ids':[0]}).status_code==422
    assert client.get('/api/library',headers=headers(user),params={'categories':['arxiv:no-such-category']}).status_code==400
