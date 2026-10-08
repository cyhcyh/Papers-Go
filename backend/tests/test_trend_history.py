import json
import httpx
import pytest
from app.config import today
from app.db import one
from app.pipeline import trend_history as history


def atom():
    def entry(ident,category,published):
        return f'<entry><id>http://arxiv.org/abs/{ident}</id><title>Memory method</title><summary>Long context memory mechanisms.</summary><published>{published}T00:00:00Z</published><arxiv:primary_category term="{category}"/><category term="{category}"/></entry>'
    return '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">'+entry('2401.00001','cs.AI','2024-01-02')+entry('2401.00002','quant-ph','2024-01-03')+entry('2609.00001','cs.AI',today())+'</feed>'


@pytest.mark.asyncio
async def test_historical_metadata_is_scoped_cached_and_does_not_enter_feed(client,monkeypatch):
    calls=[]
    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200,text=atom())
    original=httpx.AsyncClient
    monkeypatch.setattr(history.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    spec=history.search_spec('记忆与长上下文','arxiv:cs.AI',{'name_en':'Memory and long context','category_keys':json.dumps(['arxiv:cs.AI','arxiv:cs.CL'])})
    spec['background_ids']=[]
    result=await history.references(spec)
    assert len(result)==1 and result[0]['id']=='arxiv:2401.00001'
    assert 'submittedDate' in calls[0] and 'long+context' in calls[0]
    assert await history.references(spec)==result and len(calls)==1
    assert one('SELECT COUNT(*) n FROM papers')['n']==0
    materials={'papers':[]}
    await history.supplement(materials,[spec])
    assert materials['historical_evidence']['available'] and materials['papers'][0]['directions']==['记忆与长上下文']


@pytest.mark.asyncio
async def test_failed_history_lookup_is_throttled(client,monkeypatch):
    calls=[]
    def handle(request):
        calls.append(1)
        return httpx.Response(503)
    original=httpx.AsyncClient
    monkeypatch.setattr(history.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    spec=history.search_spec('组合数学','arxiv:math.CO',None)
    spec['background_ids']=[]
    assert await history.references(spec)==[]
    assert await history.references(spec)==[] and len(calls)==1


def test_abstract_metadata_parser_validates_identity_and_categories():
    html='<meta name="citation_arxiv_id" content="2203.08913"><meta name="citation_title" content="Memorizing Transformers"><meta name="citation_date" content="2022/03/16"><div class="subjects">Machine Learning (cs.LG); Artificial Intelligence (cs.AI)</div><blockquote class="abstract"><span class="descriptor">Abstract:</span>Memory lookup.</blockquote>'
    paper=history.parse_abstract_page(html,'2203.08913')
    assert paper['abstract']=='Memory lookup.' and paper['published']=='2022-03-16'
    assert paper['categories']==['cs.LG','cs.AI']
    with pytest.raises(ValueError):
        history.parse_abstract_page(html,'9999.99999')
