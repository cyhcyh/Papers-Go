import json

import httpx
import pytest
from openai import AsyncOpenAI

from app.db import execute, one, rows
from app.llm import runtime
from app.llm.provider import cloud, parse_json_answer
from app.pipeline import embed, fulltext_cache, tldr
from app.pipeline.classify import classify_paper
from app.pipeline.topic_decision import predict_topic
from app.standard_topics import catalog
from app.topic_retrieval import retrieve
from .test_research_areas import area, decision, draft, paper


@pytest.mark.parametrize('content', ['{"value":1}', '```json\n{"value":1}\n```', '```\n{"value":1}\n```'])
def test_json_accepts_only_one_complete_object_or_surrounding_fence(content):
    assert parse_json_answer(content)=={'value':1}


@pytest.mark.parametrize('content', [
    'Here is JSON: ```json\n{"value":1}\n```',
    '```json\n{"value":1}\n``` trailing text',
    '{"value":1}{"value":2}',
    r'{"formula":"\gamma"}',
])
def test_json_does_not_repair_invalid_responses(content):
    with pytest.raises(ValueError):parse_json_answer(content)


@pytest.mark.asyncio
async def test_cloud_schema_is_sent_and_fenced_json_cache_is_reusable(client,monkeypatch):
    requests=[]
    def respond(request):
        body=json.loads(request.content);requests.append(body)
        return httpx.Response(200,json={'id':'test','object':'chat.completion','created':0,
            'model':'test','choices':[{'index':0,'finish_reason':'stop','message':{
                'role':'assistant','content':'<think>hidden</think>```json\n{"value":1}\n```'}}],
            'usage':{'prompt_tokens':5,'completion_tokens':5,'total_tokens':10}})
    monkeypatch.setattr(cloud,'client',lambda:AsyncOpenAI(base_url='https://example.test/v1',api_key='test',
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))))
    binding={'kind':'cloud','base_url':'https://example.test/v1','model':'test','id':'cloud',
        'feature':'brief','thinking':'auto','json_schema':{'type':'object','required':['value']}}
    messages=[{'role':'user','content':'a paper'}]
    with runtime.bind(binding):
        assert await cloud.complete(messages,json_mode=True,cache_seconds=60)=={'value':1}
        assert await cloud.complete(messages,json_mode=True,cache_seconds=60)=={'value':1}
        assert await cloud.complete(messages)== '```json\n{"value":1}\n```'
    assert len(requests)==2 and len(messages)==1
    assert 'JSON Schema' in requests[0]['messages'][-1]['content']
    assert requests[0]['max_tokens']==16384 and requests[0]['response_format']=={'type':'json_object'}
    assert requests[1]['messages']==messages and 'response_format' not in requests[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('feature', ['brief','quality'])
@pytest.mark.parametrize('thinking,budget', [('auto',16384),('on',16384),('off',2048)])
async def test_length_truncation_preserves_saved_brief_and_quality(client,papers,monkeypatch,feature,thinking,budget):
    # Even a syntactically valid response is incomplete when the provider marks it truncated.
    payload=({'title_zh':'论文译名','problem':'研究给定图的最大边数','contribution_result':'证明最大边数的精确上界'}
             if feature=='brief' else {'summary':'证明精确上界','contribution':80,'evidence_insufficient':True})
    before=one('SELECT brief_json,tldr,skeleton,scored,quality_score FROM papers WHERE id=?',(papers[0],))
    requests=[]
    def respond(request):
        body=json.loads(request.content);requests.append(body)
        return httpx.Response(200,json={'id':'test','object':'chat.completion','created':0,
            'model':'test','choices':[{'index':0,'finish_reason':'length','message':{
                'role':'assistant','content':json.dumps(payload,ensure_ascii=False)}}],
            'usage':{'prompt_tokens':5,'completion_tokens':budget,'total_tokens':budget+5}})
    monkeypatch.setattr(cloud,'client',lambda:AsyncOpenAI(base_url='https://example.test/v1',api_key='test',
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))))
    config=runtime.defaults()
    config['routes'][feature]={'primary':{'connection_id':'cloud','model':'test','thinking':thinking,'reasoning_effort':'auto'},'fallback':None}
    generate=tldr.generate_brief if feature=='brief' else embed.score_paper
    with runtime.model_snapshot(config,replace=True),pytest.raises(ValueError,match='模型输出达到长度上限'):
        await generate(one('SELECT * FROM papers WHERE id=?',(papers[0],)))
    assert len(requests)==1 and requests[0]['max_tokens']==budget
    assert one('SELECT brief_json,tldr,skeleton,scored,quality_score FROM papers WHERE id=?',(papers[0],))==before


@pytest.mark.asyncio
async def test_placeholder_brief_preserves_previous_result(client,papers,monkeypatch):
    before=one('SELECT brief_json,tldr FROM papers WHERE id=?',(papers[0],))
    async def complete(*args,**kwargs):
        assert 'contribution_result' in kwargs['schema']['properties']
        return kwargs['validate']({'title_zh':'论文译名','problem':'研究问题','contribution_result':'主要贡献和结果'})
    monkeypatch.setattr(runtime,'complete',complete)
    with pytest.raises(ValueError,match='字段标签'):
        await tldr.generate_brief(one('SELECT * FROM papers WHERE id=?',(papers[0],)))
    assert one('SELECT brief_json,tldr FROM papers WHERE id=?',(papers[0],))==before


@pytest.mark.asyncio
@pytest.mark.parametrize('cached', [False,True])
async def test_abstract_evidence_flag_is_program_controlled_without_changing_score(client,papers,monkeypatch,cached):
    if cached:fulltext_cache.store(papers[0],{'sections':[{'section':'Method','text':'A full method.'}]})
    async def complete(*args,**kwargs):
        return kwargs['validate']({'summary':'提出新的算法并改善已有界','contribution':80,'evidence_insufficient':False})
    monkeypatch.setattr(runtime,'complete',complete)
    await embed.score_paper(one('SELECT * FROM papers WHERE id=?',(papers[0],)))
    saved=json.loads(one('SELECT skeleton FROM papers WHERE id=?',(papers[0],))['skeleton'])
    assert saved['contribution']==80 and saved['evidence_insufficient'] is (not cached)


@pytest.mark.asyncio
async def test_classification_uses_existing_topic_name_and_normalizes_abstention(client,monkeypatch):
    from app.db import connect
    from app.standard_topics import queue_or_assign
    p=paper();chosen=area('Reinforcement Learning')
    with connect() as db:
        ident,_=queue_or_assign(db,p,chosen,.9,'管理员确定的强化学习')
    topic=one('SELECT * FROM topics WHERE id=?',(ident,))
    async def choose(*args,**kwargs):return decision(chosen['key'],name_zh='错误的另一个主题名字')
    monkeypatch.setattr(runtime,'complete',choose)
    assert (await predict_topic(p,[chosen]))['name_zh']==topic['name_zh']
    calls=[]
    async def abstain(*args,**kwargs):
        calls.append(1)
        if len(calls)==1:return decision(None,confidence=.1,name_zh=None,no_suitable_topic=True)
        return {**decision(chosen['key']),'new_topic':None}
    monkeypatch.setattr(runtime,'complete',abstain)
    assert (await predict_topic(p,[chosen]))['standard_key']==chosen['key']
    assert len(calls)==2


@pytest.mark.asyncio
async def test_empty_catalog_can_still_queue_agent_proposal(client,monkeypatch):
    execute('DELETE FROM research_areas')
    from app.standard_topics import bump
    from app.db import connect
    with connect() as db:bump(db)
    calls=[]
    async def propose(*args,**kwargs):
        calls.append(1)
        assert 'new_topic' in kwargs['schema']['properties']
        return {**decision(None,confidence=.1,name_zh=None,no_suitable_topic=True),'new_topic':draft()}
    monkeypatch.setattr(runtime,'complete',propose)
    p=paper();await classify_paper(p)
    assert len(calls)==1
    assert one('SELECT classification_state FROM papers WHERE id=?',(p['id'],))['classification_state']=='awaiting_approval'
    assert len(rows('SELECT * FROM topic_pending_papers WHERE paper_id=?',(p['id'],)))==1


def test_candidate_reservation_keeps_source_discipline_and_cross_discipline_choices():
    def entry(i,discipline,label):
        return {'key':str(i),'code':str(i),'system':discipline,'discipline':discipline,'label':label,'path':label}
    entries=[entry(i,'Computer Science','Large model training') for i in range(50)]
    entries+=[entry(50+i,'Mathematics','Intersecting families') for i in range(15)]
    semantic={e['key']:1-i/100 for i,e in enumerate(entries)}
    p={'title':'Large model training','abstract':'Large model training'}
    before=retrieve(p,entries,40,semantic)
    after=retrieve(p,entries,40,semantic,{'Mathematics'})
    assert all(e['discipline']=='Computer Science' for e in before)
    assert len(after)==len({e['key'] for e in after})==40
    assert sum(e['discipline']=='Mathematics' for e in after)==10
    assert after[0]['discipline']=='Computer Science'


def test_source_category_hint_recovers_combinatorics_without_changing_catalog(client):
    entries=list(catalog().values())
    p={'title':'Intersecting integer partitions: star bounds and counterexamples at every scale',
       'abstract':'Largest intersecting families of integer partitions with common parts counted with multiplicity.'}
    options=retrieve(p,entries,40,source_disciplines={'Mathematics'},source_labels=['Combinatorics'])
    assert 'Enumerative Combinatorics' in [e['label'] for e in options]
    assert len(options)==40 and any(e['discipline']!='Mathematics' for e in options)
    assert len(catalog())==len(entries)
