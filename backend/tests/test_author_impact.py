import asyncio
import json
import httpx
import pytest
from app.config import now,today
from app.db import execute,one,dumps,rows
from app.pipeline.author_impact import influence,confirmed_authors,apply_impact,author_impact,RateLimited,title_text


def work():
    return {'id':'https://openalex.org/W1','title':'Exact research paper','publication_year':int(today()[:4]),
            'authorships':[{'author':{'id':'https://openalex.org/A1','display_name':'Alice Researcher'}},
                          {'author':{'id':'https://openalex.org/A2','display_name':'Bob Scientist'}}]}


def test_matching_requires_paper_identity_and_authors():
    paper={'title':'Exact research paper','published':today(),'authors':dumps(['Alice Researcher','Bob Scientist'])}
    assert len(confirmed_authors(paper,work()))==2
    wrong=work();wrong['title']='Different unrelated paper'
    assert not confirmed_authors(paper,wrong)
    wrong=work();wrong['authorships'][0]['author']['display_name']='Different Alice'
    assert not confirmed_authors(paper,wrong)
    wrong=work();wrong['publication_year']=2000
    assert not confirmed_authors(paper,wrong)


def test_influence_is_monotonic_diminishing_and_bounded():
    assert influence(0)==0
    assert 0<influence(1)<influence(5)<influence(20)==influence(1000)==100
    assert influence(2)-influence(1)>influence(11)-influence(10)


def cached(author_id, count):
    year=int(today()[:4])
    execute('INSERT OR REPLACE INTO author_impact_cache(author_id,name,highly_cited_count,first_year,last_year,fetched_at,validated) VALUES(?,?,?,?,?,?,1)',
            (author_id,author_id,count,year-9,year,now()))


def seed():
    return execute('INSERT INTO papers(title,abstract,authors,primary_category,published,created_at,ingested_date,quality_score,scored,base_quality_score) VALUES(?,?,?,?,?,?,?,?,1,60)',
                   ('Exact research paper','research',dumps(['Alice Researcher','Bob Scientist']),'cs.AI',today(),now(),today(),60))


def test_bonus_is_maximum_not_sum_and_never_stacks(client):
    ident=seed()
    for a,count in [('A1',20),('A2',5)]:
        execute('INSERT INTO paper_author_links VALUES(?,?,?,?)',(ident,a,a,1))
        cached(a,count)
    assert apply_impact(ident)==100
    assert one('SELECT quality_score FROM papers WHERE id=?',(ident,))['quality_score']==63
    apply_impact(ident)
    assert one('SELECT quality_score FROM papers WHERE id=?',(ident,))['quality_score']==63
    execute('UPDATE papers SET base_quality_score=99 WHERE id=?',(ident,));apply_impact(ident)
    assert one('SELECT quality_score FROM papers WHERE id=?',(ident,))['quality_score']==100


def test_missing_data_preserves_quality_and_changed_authors_clear_bonus(client):
    ident=seed();apply_impact(ident)
    assert one('SELECT quality_score FROM papers WHERE id=?',(ident,))['quality_score']==60
    execute('INSERT INTO paper_author_links VALUES(?,?,?,?)',(ident,'A1','Alice',1))
    cached('A1',20)
    apply_impact(ident)
    execute('UPDATE papers SET authors=? WHERE id=?',(dumps(['Changed Author']),ident))
    assert one('SELECT quality_score,author_impact FROM papers WHERE id=?',(ident,))=={'quality_score':60,'author_impact':0}


def test_background_job_matches_once_and_caches_metrics(client,monkeypatch):
    from app.pipeline import author_impact as module
    ident=seed();called=[]
    def handle(request):
        called.append(request.url.path)
        if 'title.search:' in request.url.params.get('filter',''):return httpx.Response(200,json={'results':[work()]})
        filters=request.url.params['filter']
        assert 'citation_normalized_percentile.is_in_top_10_percent:true' in filters
        assert 'from_publication_date:'+str(int(today()[:4])-9)+'-01-01' in filters
        assert 'to_publication_date:'+today() in filters
        assert request.url.params['per_page']=='1'
        return httpx.Response(200,json={'meta':{'count':20}})
    original=httpx.AsyncClient
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    assert asyncio.run(author_impact())==1
    assert asyncio.run(author_impact())==0
    assert called==['/works','/works','/works']
    assert one('SELECT quality_score FROM papers WHERE id=?',(ident,))['quality_score']==63


def test_rate_limit_stops_without_discarding_unfinished_match(client,monkeypatch):
    from app.pipeline import author_impact as module
    ident=seed();original=httpx.AsyncClient
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(lambda request:httpx.Response(429)),**kwargs))
    with pytest.raises(RateLimited):asyncio.run(author_impact())
    assert one('SELECT * FROM author_work_matches WHERE paper_id=?',(ident,)) is None


@pytest.mark.parametrize('category',['math.CO','cs.AI','quant-ph','astro-ph.GA','q-bio.BM','econ.TH','eess.SP','stat.AP'])
def test_all_disciplines_share_normalized_signal(client, category):
    ident=seed();execute('UPDATE papers SET primary_category=? WHERE id=?',(category,ident))
    execute('INSERT INTO paper_author_links VALUES(?,?,?,?)',(ident,'A1','Alice',1))
    cached('A1',5)
    assert apply_impact(ident)==influence(5)


def test_shared_author_cache_updates_all_linked_papers_without_extra_calls(client,monkeypatch):
    from app.pipeline import author_impact as module
    a,b=seed(),seed();calls=[]
    execute('UPDATE papers SET author_impact=25 WHERE id=?',(a,))
    for ident in (a,b):
        execute('INSERT INTO author_work_matches VALUES(?,?,?,?)',(ident,'W1','matched',now()))
        execute('INSERT INTO paper_author_links VALUES(?,?,?,?)',(ident,'A1','Alice',1))
    def handle(request):
        calls.append(request.url.params['filter'])
        return httpx.Response(200,json={'meta':{'count':20}})
    original=httpx.AsyncClient
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    assert asyncio.run(author_impact())==2
    assert len(calls)==1
    assert rows('SELECT author_impact,quality_score FROM papers')==[{'author_impact':100.,'quality_score':63.}]*2
    assert asyncio.run(author_impact())==0 and len(calls)==1


def test_openalex_limit_resume_does_not_search_paper_again(client,monkeypatch):
    from app.pipeline import author_impact as module
    ident=seed();calls=[];limited=True
    def handle(request):
        calls.append(str(request.url))
        if 'title.search:' in request.url.params.get('filter',''):
            return httpx.Response(200,json={'results':[work()]})
        return httpx.Response(429) if limited else httpx.Response(200,json={'meta':{'count':20}})
    original=httpx.AsyncClient
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    with pytest.raises(RateLimited):asyncio.run(author_impact())
    assert len(calls)==2
    assert one('SELECT status,work_id FROM author_work_matches WHERE paper_id=?',(ident,))=={'status':'pending','work_id':'W1'}
    with pytest.raises(RateLimited):asyncio.run(author_impact())
    assert len(calls)==3
    limited=False
    assert asyncio.run(author_impact())==1
    assert len(calls)==5 and sum('title.search' in call for call in calls)==1


def test_title_formatting_and_initials_keep_paper_identity_checks():
    p={'title':r'Exact $\mathrm{research}$ paper','published':today(),'authors':dumps(['A. Researcher','B. Scientist'])}
    assert len(confirmed_authors(p,work()))==2
    assert title_text(p['title']).strip()=='Exact  research  paper'
    different=work();different['authorships'][0]['author']['display_name']='Alex Researcher'
    p['authors']=dumps(['Alice Researcher','Bob Scientist'])
    assert not confirmed_authors(p,different)


def test_ambiguous_initials_and_conflicting_full_names_are_not_linked():
    p={'title':'Exact research paper','published':today(),'authors':dumps(['A. Researcher','Bob Scientist'])}
    ambiguous=work()
    ambiguous['authorships'].append({'author':{'id':'https://openalex.org/A3','display_name':'Alex Researcher'}})
    assert not confirmed_authors(p,ambiguous)


def test_measured_zero_has_a_known_flag_and_metadata_change_clears_it(client):
    ident=seed()
    execute('UPDATE papers SET authors=? WHERE id=?',(dumps(['Alice']),ident))
    execute('INSERT INTO paper_author_links VALUES(?,?,?,?)',(ident,'A1','Alice',1))
    cached('A1',0);apply_impact(ident)
    assert one('SELECT author_impact_known FROM papers WHERE id=?',(ident,))['author_impact_known']==1
    execute('UPDATE papers SET title=? WHERE id=?',('Changed paper',ident))
    assert one('SELECT author_impact_known FROM papers WHERE id=?',(ident,))['author_impact_known']==0


def test_zero_for_only_some_authors_does_not_mark_all_authors_as_known(client):
    ident=seed()
    execute('INSERT INTO paper_author_links VALUES(?,?,?,?)',(ident,'A1','Alice',1))
    cached('A1',0);apply_impact(ident)
    assert one('SELECT author_impact_known FROM papers WHERE id=?',(ident,))['author_impact_known']==0


def test_retired_budget_is_removed_without_losing_key_or_blocking_queries(client,monkeypatch):
    from app.pipeline import author_impact as module
    from app.task_settings import update_advanced,configuration,public_configuration
    from app.db import connect
    update_advanced('author_impact',{'openalex_api_key':'test-key','batch_size':10})
    with connect() as db:
        saved=json.loads(db.execute("SELECT value FROM app_settings WHERE name='task_center'").fetchone()['value'])
        saved['advanced']['author_impact']['daily_credits']=1
        db.execute("UPDATE app_settings SET value=? WHERE name='task_center'",(dumps(saved),))
        db.execute("INSERT INTO app_settings(name,value,updated_at) VALUES('openalex_usage',?,?)",(dumps({'blocked':True}),now()))
        module.initialize(db)
    assert configuration()['advanced']['author_impact']['openalex_api_key']=='test-key'
    assert 'daily_credits' not in public_configuration()['advanced']['author_impact']
    assert one("SELECT value FROM app_settings WHERE name='openalex_usage'") is None
    seed();calls=[]
    def handle(request):
        calls.append(request)
        assert request.headers['Authorization']=='Bearer test-key'
        return httpx.Response(200,json={'results':[work()]} if 'title.search:' in request.url.params.get('filter','') else {'meta':{'count':20}})
    original=httpx.AsyncClient
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    assert asyncio.run(author_impact())==1 and len(calls)==3


def test_invalid_count_and_incomplete_refresh_preserve_previous_result(client,monkeypatch):
    from app.pipeline import author_impact as module
    ident=seed()
    execute('UPDATE papers SET author_impact=100,quality_score=63 WHERE id=?',(ident,))
    execute('INSERT INTO author_work_matches VALUES(?,?,?,?)',(ident,'W1','matched',now()))
    for author in ('A1','A2'):execute('INSERT INTO paper_author_links VALUES(?,?,?,?)',(ident,author,author,1))
    def handle(request):
        return httpx.Response(200,json={'meta':{'count':5 if 'A1' in request.url.params['filter'] else 'bad'}})
    original=httpx.AsyncClient
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    with pytest.raises(ValueError):asyncio.run(author_impact())
    assert one('SELECT author_impact,quality_score FROM papers WHERE id=?',(ident,))=={'author_impact':100.,'quality_score':63.}
    assert one("SELECT COUNT(*) n FROM author_impact_cache")['n']==1


def test_current_year_and_expired_cache_refresh_once(client,monkeypatch):
    from app.pipeline import author_impact as module
    ident=seed();year=int(today()[:4])
    execute('INSERT INTO author_work_matches VALUES(?,?,?,?)',(ident,'W1','matched',now()))
    execute('INSERT INTO paper_author_links VALUES(?,?,?,?)',(ident,'A1','Alice',1))
    execute('INSERT INTO author_impact_cache(author_id,name,highly_cited_count,first_year,last_year,fetched_at,validated) VALUES(?,?,?,?,?,?,1)',('A1','Alice',10,year-10,year-1,now()))
    calls=[];original=httpx.AsyncClient
    def handle(request):
        calls.append(1)
        return httpx.Response(200,json={'cited_by_count':5} if '/authors/' in request.url.path else {'meta':{'count':0}})
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    assert asyncio.run(author_impact())==1
    assert one('SELECT last_year,highly_cited_count FROM author_impact_cache')=={'last_year':year,'highly_cited_count':0}
    assert asyncio.run(author_impact())==0 and len(calls)==2


def test_stop_absorbed_by_transport_does_not_publish_author_count(client,monkeypatch):
    from app.pipeline import author_impact as module
    from app.pipeline_control import cancellation_scope
    ident=seed();requested=asyncio.Event()
    execute('INSERT INTO author_work_matches VALUES(?,?,?,?)',(ident,'W1','pending',None))
    execute('INSERT INTO paper_author_links VALUES(?,?,?,?)',(ident,'A1','Alice',1))
    def handle(request):requested.set();return httpx.Response(200,json={'meta':{'count':20}})
    original=httpx.AsyncClient
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    async def run():
        with cancellation_scope(requested):await author_impact()
    with pytest.raises(asyncio.CancelledError):asyncio.run(run())
    assert one('SELECT COUNT(*) n FROM author_impact_cache')['n']==0


def test_explicit_priority_applies_to_any_category(client,monkeypatch):
    from app.pipeline import author_impact as module
    from app.task_settings import update_advanced
    a,b=seed(),seed();seen=[]
    execute('UPDATE papers SET primary_category=? WHERE id=?',('math.CO',a))
    update_advanced('author_impact',{'priority_category':'math.CO','batch_size':1})
    async def match(self,paper):seen.append(paper['id']);return None,[]
    monkeypatch.setattr(module.OpenAlex,'match',match)
    assert asyncio.run(author_impact())==1 and seen==[a]


def test_fragmented_author_uses_history_only_with_unique_coauthor_evidence(client,monkeypatch):
    from app.pipeline import author_impact as module
    first,second=seed(),seed();calls=[]
    for ident in (first,second):
        execute('INSERT INTO author_work_matches VALUES(?,?,?,?)',(ident,'W1','matched',now()))
        execute('INSERT INTO paper_author_links VALUES(?,?,?,?)',(ident,'A1','Alice Researcher',1))
    def handle(request):
        calls.append(str(request.url))
        if request.url.path=='/authors/A1':return httpx.Response(200,json={'cited_by_count':0,'counts_by_year':[]})
        if request.url.path=='/authors':return httpx.Response(200,json={'results':[
            {'id':'https://openalex.org/A3','display_name':'Alice Researcher','cited_by_count':100},
            {'id':'https://openalex.org/A4','display_name':'Alice Researcher','cited_by_count':200}]})
        if 'search' in request.url.params:
            evidence=work();evidence['id']='https://openalex.org/W9'
            evidence['authorships'][0]['author']['id']='https://openalex.org/A3'
            return httpx.Response(200,json={'results':[evidence]})
        count=0 if 'A1' in request.url.params['filter'] else 5
        return httpx.Response(200,json={'meta':{'count':count}})
    original=httpx.AsyncClient
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    assert asyncio.run(author_impact())==2
    assert one("SELECT author_id,evidence_work_id FROM author_identity_aliases WHERE fragment_id='A1'")=={'author_id':'A3','evidence_work_id':'W9'}
    assert {r['author_id'] for r in rows('SELECT author_id FROM paper_author_links')}=={'A3'}
    assert rows('SELECT author_impact FROM papers')==[{'author_impact':influence(5)}]*2
    assert sum('/authors?' in url for url in calls)==1
    assert asyncio.run(author_impact())==0


def test_same_name_without_coauthor_evidence_stays_unknown(client,monkeypatch):
    from app.pipeline import author_impact as module
    ident=seed()
    execute('INSERT INTO author_work_matches VALUES(?,?,?,?)',(ident,'W1','matched',now()))
    execute('INSERT INTO paper_author_links VALUES(?,?,?,?)',(ident,'A1','Alice Researcher',1))
    def handle(request):
        if request.url.path=='/authors/A1':return httpx.Response(200,json={'cited_by_count':0})
        if request.url.path=='/authors':return httpx.Response(200,json={'results':[
            {'id':'https://openalex.org/A3','display_name':'Alice Researcher','cited_by_count':10000}]})
        if 'search' in request.url.params:
            evidence=work();evidence['authorships'][1]['author']['display_name']='Unrelated Person'
            return httpx.Response(200,json={'results':[evidence]})
        return httpx.Response(200,json={'meta':{'count':0}})
    original=httpx.AsyncClient
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    assert asyncio.run(author_impact())==1
    assert one('SELECT author_impact_known FROM papers WHERE id=?',(ident,))['author_impact_known']==0
    assert one("SELECT validated FROM author_impact_cache WHERE author_id='A1'")['validated']==0
    assert one('SELECT COUNT(*) n FROM author_identity_aliases')['n']==0
    assert asyncio.run(author_impact())==0


def test_two_historical_identities_are_not_selected_by_citation_count(client,monkeypatch):
    from app.pipeline.author_impact import OpenAlex
    ident=seed();p=rows('SELECT * FROM papers WHERE id=?',(ident,))[0]
    def handle(request):
        if request.url.path=='/authors':return httpx.Response(200,json={'results':[
            {'id':'https://openalex.org/A3','display_name':'Alice Researcher','cited_by_count':10},
            {'id':'https://openalex.org/A4','display_name':'Alice Researcher','cited_by_count':10000}]})
        evidence=[]
        for aid in ('A3','A4'):
            w=work();w['authorships'][0]['author']['id']='https://openalex.org/'+aid;evidence.append(w)
        return httpx.Response(200,json={'results':evidence})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
            assert await OpenAlex(c).historical_identity('Alice Researcher',p) is None
    asyncio.run(run())
