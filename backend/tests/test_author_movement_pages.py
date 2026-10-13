from datetime import date,timedelta
import pytest
from app.config import now,today
from app.db import execute,dumps
from .conftest import headers


def paper(title,authors,published):
    return execute('INSERT INTO papers(title,abstract,authors,published,primary_category,created_at,ingested_date) VALUES(?,?,?,?,?,?,?)',(title,'Research abstract',dumps(authors),published,'math.CO',now(),today()))


@pytest.mark.parametrize('indexed',[True,False])
def test_author_updates_paginate_beyond_thirty_keep_scope_and_matching_authors(client,accounts,monkeypatch,indexed):
    from app import paper_index
    monkeypatch.setattr(paper_index,'ready',lambda:indexed)
    admin,other=accounts
    saved=paper('Saved paper',['Ada Researcher'],(date.fromisoformat(today())-timedelta(days=30)).isoformat())
    execute('INSERT INTO user_paper_state(user_id,paper_id,saved,updated_at) VALUES(?,?,1,?)',(admin['user']['id'],saved,now()))
    other_saved=paper('Other user saved',['Private Interest'],today())
    execute('INSERT INTO user_paper_state(user_id,paper_id,saved,updated_at) VALUES(?,?,1,?)',(other['user']['id'],other_saved,now()))
    expected=[paper('New work '+str(i),['Ada Researcher','Other Coauthor'],today()) for i in range(37)]
    oldest=paper('Day seven',['Ada Researcher'],(date.fromisoformat(today())-timedelta(days=6)).isoformat())
    paper('Too old',['Ada Researcher'],(date.fromisoformat(today())-timedelta(days=7)).isoformat())
    paper('Future',['Ada Researcher'],(date.fromisoformat(today())+timedelta(days=1)).isoformat())
    paper('Not an exact name',['Ada'],today())
    found=[]
    for offset in range(0,40,5):
        response=client.get('/api/trends/author-movements',headers=headers(admin),params={'offset':offset,'limit':5})
        assert response.status_code==200,response.text
        page=response.json();assert page['total']==38
        assert len(page['items'])<=5
        assert all(p['authors']==['Ada Researcher'] for p in page['items'])
        found.extend(p['id'] for p in page['items'])
    assert found==list(reversed(expected))+[oldest]
    mine=client.get('/api/trends/author-movements',headers=headers(other)).json()
    assert mine['total']==1 and mine['items'][0]['id']==other_saved
    assert client.get('/api/trends/author-movements').status_code==401
    assert client.get('/api/trends/author-movements',headers=headers(admin),params={'offset':-1}).status_code==422


def test_pagination_endpoint_does_not_load_or_generate_direction_trends(client,accounts,monkeypatch):
    from app.pipeline import direction_trends
    def unexpected(*args,**kwargs):raise AssertionError('Author pagination must not load direction trends')
    monkeypatch.setattr(direction_trends,'trend_context',unexpected)
    response=client.get('/api/trends/author-movements',headers=headers(accounts[0]))
    assert response.status_code==200 and response.json()=={'items':[],'total':0,'offset':0,'limit':5}


def test_legacy_trends_keeps_movements_and_new_client_can_skip_them(client,accounts,monkeypatch):
    from app.pipeline import trends,direction_trends
    monkeypatch.setattr(direction_trends,'trend_context',lambda *a,**k:{'batch':None})
    monkeypatch.setattr(direction_trends,'scoped_papers',lambda *a,**k:[])
    monkeypatch.setattr(direction_trends,'coverage',lambda *a,**k:{})
    monkeypatch.setattr(trends,'trend_summary',lambda *a,**k:{'summary':'','created_at':None,'items':[],'period':{},'status':'empty'})
    calls=[]
    def movements(*args,**kwargs):
        calls.append(kwargs)
        return {'items':[{'id':1,'title':'Work','authors':['A'],'published':today(),'primary_category':'math.CO','venue':None}]}
    monkeypatch.setattr(trends,'author_movements',movements)
    old=trends.trend_data(accounts[0]['user']['id'])
    assert old['movements']==[{'id':1,'title':'Work','authors':['A'],'published':today()}]
    assert len(calls)==1 and calls[0]['count_total'] is False
    new=trends.trend_data(accounts[0]['user']['id'],include_movements=False)
    assert new['movements']==[] and len(calls)==1
