import copy
import json

import pytest

from app import recommendation_settings
from app.config import now, today
from app.db import connect, dumps, execute, one, pack, rows
from app.interest.profile import put_profile
from app.pipeline.score import browse_page, ranked_page, ranking_key, scored_papers
from .conftest import headers


def seed(quality=60, impact=100, known=1):
    return execute('''INSERT INTO papers(title,abstract,primary_category,published,ingested_date,
        created_at,quality_score,scored,base_quality_score,author_impact,author_impact_known)
        VALUES(?,?,?,?,?,?,?,1,?,?,?)''',
        ('Research paper','Research findings','cs.AI',today(),today(),now(),quality,quality,impact,known))


def weights(client, accounts, personal_author=0, guest_author=0):
    body={'personal':{'interest':1-personal_author,'quality':0,'diversity':0,'author':personal_author},
          'guest':{'quality':1-guest_author,'recency':0,'author':guest_author}}
    assert client.put('/api/admin/recommendation',headers=headers(accounts[0]),json=body).status_code==200
    return body


@pytest.mark.parametrize('kind',['guest','profile','semantic'])
def test_author_is_a_weighted_component_in_every_path(client,accounts,kind):
    weights(client,accounts,.1,.2)
    uid=None if kind=='guest' else accounts[1]['user']['id']
    ident=seed()
    if kind!='guest':
        put_profile(uid,'## 核心兴趣\n- [w:1] Research',{},'test',pack([1,0,0,0]) if kind=='semantic' else None)
    if kind=='semantic':
        from app.db import set_paper_vector
        set_paper_vector(ident,[.5,0,.866,0])
    paper=rows('SELECT * FROM papers WHERE id=?',(ident,))[0]
    zero=scored_papers(uid,[{**paper,'author_impact':0}])[0]
    influenced=scored_papers(uid,[paper])[0]
    assert influenced['score']==pytest.approx(zero['score']+(20 if kind=='guest' else 10))
    assert influenced['quality_score']==zero['quality_score']==60


@pytest.mark.parametrize('kind',['guest','profile','semantic'])
def test_unavailable_author_data_renormalizes_other_components(client,accounts,kind):
    uid=None if kind=='guest' else accounts[1]['user']['id']
    ident=seed(impact=0,known=0)
    if kind!='guest':
        put_profile(uid,'## 核心兴趣\n- [w:1] Research',{},'test',pack([1,0,0,0]) if kind=='semantic' else None)
    if kind=='semantic':
        from app.db import set_paper_vector
        set_paper_vector(ident,[.5,0,.866,0])
    paper=rows('SELECT * FROM papers WHERE id=?',(ident,))[0]
    weights(client,accounts)
    before=scored_papers(uid,[paper])[0]['score']
    weights(client,accounts,.2,.4)
    assert scored_papers(uid,[paper])[0]['score']==before
    assert scored_papers(uid,[{**paper,'author_impact_known':1}])[0]['score']==pytest.approx(before*(.6 if kind=='guest' else .8),abs=.1)


def test_only_author_weight_and_no_data_returns_neutral_score(client,accounts):
    weights(client,accounts,1,1)
    ident=seed(impact=0,known=0)
    paper=rows('SELECT * FROM papers WHERE id=?',(ident,))[0]
    assert scored_papers(None,[paper])[0]['score']==50
    assert scored_papers(None,[{**paper,'author_impact_known':1}])[0]['score']==0


def test_quality_author_bonus_remains_and_new_weight_does_not_modify_quality(client,accounts):
    from app.pipeline.author_impact import apply_impact
    ident=seed(impact=0,known=0);year=int(today()[:4])
    execute('INSERT INTO paper_author_links VALUES(?,?,?,?)',(ident,'A1','Author',1))
    execute('INSERT INTO author_impact_cache(author_id,name,highly_cited_count,first_year,last_year,fetched_at,validated) VALUES(?,?,?,?,?,?,1)',('A1','Author',20,year-9,year,now()))
    apply_impact(ident)
    assert one('SELECT quality_score FROM papers WHERE id=?',(ident,))['quality_score']==63
    weights(client,accounts)
    paper=rows('SELECT * FROM papers WHERE id=?',(ident,))[0]
    assert scored_papers(None,[paper])[0]['score']==63
    weights(client,accounts,0,.05)
    assert scored_papers(None,[paper])[0]['score']==pytest.approx(.95*63+5,abs=.1)
    assert one('SELECT quality_score FROM papers WHERE id=?',(ident,))['quality_score']==63


def test_separate_weights_invalidate_warm_cache_and_scores_are_bounded(client,accounts):
    seed(quality=100)
    assert ranked_page(None,'recommendations',0,20)['items'][0]['score']==100
    key=ranking_key(None,'recommendations')
    weights(client,accounts,.7,0)
    assert ranking_key(None,'recommendations')!=key
    weights(client,accounts,0,1)
    assert ranked_page(None,'recommendations',0,20)['items'][0]['score']==100
    current=recommendation_settings.configuration()
    assert current['personal']['author']==0 and current['guest']['author']==1


@pytest.mark.parametrize('group',['personal','guest'])
@pytest.mark.parametrize('value',[-.1,1.1,None,'nan','inf',.3])
def test_invalid_author_weights_or_totals_are_atomic(client,accounts,group,value):
    before=recommendation_settings.configuration();body=copy.deepcopy(before);body[group]['author']=value
    assert client.put('/api/admin/recommendation',headers=headers(accounts[0]),json=body).status_code==422
    assert recommendation_settings.configuration()==before


def test_legacy_migration_preserves_custom_proportions_and_original_bonus(client,accounts):
    old={'personal':{'interest':.7,'quality':.2,'diversity':.1},'guest':{'quality':.65,'recency':.35},'author':.05}
    with connect() as db:
        site=json.loads(db.execute("SELECT value FROM app_settings WHERE name='site'").fetchone()['value'])
        site['recommendation']=old
        db.execute("UPDATE app_settings SET value=? WHERE name='site'",(dumps(site),))
        recommendation_settings.initialize(db)
        stored=json.loads(db.execute("SELECT value FROM app_settings WHERE name='site'").fetchone()['value'])
        recommendation_settings.initialize(db)
        assert json.loads(db.execute("SELECT value FROM app_settings WHERE name='site'").fetchone()['value'])==stored
    current=recommendation_settings.configuration()
    assert current['personal']==pytest.approx({'interest':.665,'quality':.19,'diversity':.095,'author':.05})
    assert current['guest']==pytest.approx({'quality':.6175,'recency':.3325,'author':.05})


def test_previously_loaded_forms_preserve_individual_author_weights(client,accounts):
    weights(client,accounts,.1,.3)
    old={'personal':{'interest':.7,'quality':.2,'diversity':.1},'guest':{'quality':.6,'recency':.4}}
    response=client.put('/api/admin/recommendation',headers=headers(accounts[0]),json=old)
    assert response.status_code==200
    assert response.json()['value']['personal']==pytest.approx({'interest':.63,'quality':.18,'diversity':.09,'author':.1})
    assert response.json()['value']['guest']==pytest.approx({'quality':.42,'recency':.28,'author':.3})


def test_browse_streaming_rank_and_feed_use_the_same_author_scores(client,accounts):
    ordinary=seed(quality=60,impact=0)
    influential=seed(quality=50,impact=100)
    weights(client,accounts,0,.3)
    page=browse_page(None,'1=1',[],'score',0,20)
    assert [p['id'] for p in page['items']]==[influential,ordinary]
    direct=scored_papers(None,rows('SELECT * FROM papers'))
    assert {p['id']:p['score'] for p in page['items']}=={p['id']:p['score'] for p in direct}


def test_known_zero_changes_cached_ranking_even_when_quality_does_not_change(client,accounts):
    from app.pipeline.author_impact import apply_impact
    ident=seed(impact=0,known=0);year=int(today()[:4])
    execute('UPDATE papers SET authors=? WHERE id=?',(dumps(['Author']),ident))
    weights(client,accounts,0,.2)
    before=ranked_page(None,'recommendations',0,20)['items'][0]['score']
    key=ranking_key(None,'recommendations')
    execute('INSERT INTO paper_author_links VALUES(?,?,?,?)',(ident,'A1','Author',1))
    execute('INSERT INTO author_impact_cache(author_id,name,highly_cited_count,first_year,last_year,fetched_at,validated) VALUES(?,?,?,?,?,?,1)',('A1','Author',0,year-9,year,now()))
    apply_impact(ident)
    assert ranking_key(None,'recommendations')!=key
    assert ranked_page(None,'recommendations',0,20)['items'][0]['score']==pytest.approx(before*.8)


def test_scoring_does_not_lookup_author_data_per_paper(client,monkeypatch):
    import app.pipeline.author_impact as authors
    import app.pipeline.score as scoring
    ident=seed();paper=rows('SELECT * FROM papers WHERE id=?',(ident,))[0]
    def unexpected(*args,**kwargs):raise AssertionError('request path must not query author API/cache')
    monkeypatch.setattr(authors.OpenAlex,'get',unexpected)
    calls=[];original=scoring.rows
    def traced(sql,args=()):calls.append(sql);return original(sql,args)
    monkeypatch.setattr(scoring,'rows',traced)
    assert len(scored_papers(None,[paper]*100))==100
    assert not any('author_impact_cache' in sql or 'author_metrics' in sql or 'paper_author_links' in sql for sql in calls)
