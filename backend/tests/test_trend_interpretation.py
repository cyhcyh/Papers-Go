import json
from datetime import date,timedelta
import pytest
from app import prompts
from app.config import now,today,settings
from app.db import execute,dumps,one
from app.interest.profile import put_profile
from app.pipeline import direction_trends as analysis
from .test_arxiv_daily import batch,interest_topic,mocked,FULL,daily_topics


def interest(uid,topic):
    put_profile(uid,'## 核心兴趣（长期）\n- [w:1] Agent memory',
                {'category_selection':{'categories':[],'topics':{'arxiv:cs.AI':[topic]}}},'manual')


def old_paper(topic,age,*,venue=None,category='cs.AI',abstract='Agent memory mechanisms with explicit task conditions.'):
    stamp=(date.fromisoformat(today())-timedelta(days=age)).isoformat()
    pid=execute('INSERT INTO papers(title,abstract,primary_category,venue,published,created_at,ingested_date) VALUES(?,?,?,?,?,?,?)',
                ('Agent memory prior result',abstract,category,venue,stamp,now(),today()))
    execute('INSERT INTO paper_topics VALUES(?,?,.9)',(pid,topic))
    return pid


def materials(uid):
    profile,structured,_=analysis.profile_context(uid)
    return analysis.research_materials(profile,structured)


def test_complete_abstracts_are_deduplicated_in_the_model_payload(client,accounts,papers,batch):
    topic,ids=interest_topic(batch,'Agent memory',1);uid=accounts[0]['user']['id'];interest(uid,topic)
    abstract='Agent memory methodology. '*55+'The decisive result holds only for the stated task family.'
    execute('UPDATE papers SET abstract=? WHERE id=?',(abstract,ids[0]))
    result=materials(uid);payload=analysis.model_payload(result)
    evidence=next(p for p in payload['papers'] if p['id']==ids[0])
    assert evidence['abstract']==abstract and len(abstract)>1100
    assert all('papers' not in group for group in payload['groups'])
    assert len(payload['papers'])==len({p['id'] for p in payload['papers']})
    assert dumps(payload).count(abstract)==1
    assert sum(ids[0] in g['paper_ids'] for g in payload['groups'])==2
    assert analysis.input_tokens(prompts.get('trend_report'))+analysis.input_tokens(dumps(payload))<=settings().trend_input_tokens-2000


def test_recent_comparisons_are_bounded_related_and_never_today_or_conference(client,accounts,papers,batch):
    topic,ids=interest_topic(batch,'Agent memory',2);uid=accounts[0]['user']['id'];interest(uid,topic)
    recent=old_paper(topic,3);month=old_paper(topic,20)
    excluded={old_paper(topic,31),old_paper(topic,0),old_paper(topic,2,venue='AAAI'),old_paper(topic,2,category='math.CO')}
    other,_=interest_topic(batch,'Unrelated topic',0);excluded.add(old_paper(other,2))
    result=materials(uid);group=next(g for g in result['groups'] if g.get('kind')!='daily_overview')
    assert {p['id'] for p in group['reference_papers']}=={recent,month}
    assert group['reference_papers'][0]['id']==recent
    payload=analysis.model_payload(result);records={p['id']:p for p in payload['papers']}
    assert not excluded.intersection(records)
    assert all(records[i]['evidence_kind']=='current' for i in ids)
    assert all(records[i]['evidence_kind']=='reference' for i in (recent,month))
    assert result['groups'][0]['focus_topics'][0]['count']==2
    assert all(recent not in g['paper_ids'] for g in payload['groups'])


@pytest.mark.asyncio
async def test_history_without_today_progress_does_not_trigger_a_model_call(client,accounts,papers,batch,monkeypatch):
    topic,_=interest_topic(batch,'Agent memory',0);uid=accounts[0]['user']['id'];interest(uid,topic);old_paper(topic,3)
    calls=mocked(monkeypatch)
    assert await analysis.ensure_direction_trend(uid) and not calls
    assert all(g['status']=='insufficient' for g in analysis.trend_summary(uid)['items'])


@pytest.mark.asyncio
async def test_only_full_analyses_need_repairs_and_short_overview_is_structured(client,accounts,papers,batch,monkeypatch):
    topic,ids=interest_topic(batch,'Agent memory',1);uid=accounts[0]['user']['id'];interest(uid,topic);calls=[]
    async def complete(feature,messages,**kwargs):
        payload=json.loads(messages[-1]['content']);calls.append(payload)
        if 'groups' in payload:
            return {'items':[{'direction':g['direction'],'summary':'需要修正组织方式。' if g.get('kind')!='daily_overview' else '',
                             'short_summary':'不需要的开场。\n- **Agent memory**：明确了任务条件。' if g.get('kind')=='daily_overview' else '',
                             'paper_ids':g['paper_ids'][:1],'focus_directions':[t['name'] for t in g.get('focus_topics',[])] if g.get('kind')=='daily_overview' else [],'daily_topics':daily_topics(g,g['paper_ids'][:1]) if g.get('kind')=='daily_overview' else [],'insufficient':False} for g in payload['groups']]}
        return {'items':[{'direction':g['direction'],'summary':'当前成果明确了方法的任务条件。\n\n1. **方法适用性**：该结果将任务假设与方法表现联系起来，便于判断能否用于相似任务。' if g['kind']!='daily_overview' else '',
                         'short_summary':'今日研究聚焦**Agent memory**，新结果明确了方法的任务条件，便于判断在相似任务中的适用性。' if g['kind']=='daily_overview' else ''} for g in payload['items']]}
    monkeypatch.setattr(analysis.models,'complete',complete)
    assert await analysis.ensure_direction_trend(uid) and len(calls)==2
    assert len(calls[1]['items'])==1 and calls[1]['items'][0]['kind']!='daily_overview'
    result=analysis.trend_summary(uid)['items']
    assert all(g['status']=='ready' for g in result)
    assert len(result[1]['summary'])<300
    assert result[0]['short_summary'].startswith('今日研究聚焦**Agent memory**')
    assert '明确了任务条件' not in result[0]['short_summary']


@pytest.mark.asyncio
async def test_reference_only_claim_is_rejected_and_keeps_previous_report(client,accounts,papers,batch,monkeypatch):
    topic,_=interest_topic(batch,'Agent memory',1);uid=accounts[0]['user']['id'];interest(uid,topic);reference=old_paper(topic,3)
    mocked(monkeypatch);assert await analysis.ensure_direction_trend(uid)
    previous=analysis.cached(uid)['items_json']
    async def invalid(feature,messages,**kwargs):
        data=json.loads(messages[-1]['content'])
        return {'items':[{'direction':g['direction'],'summary':FULL if g.get('kind')!='daily_overview' else '',
                         'short_summary':'今日研究聚焦**Agent memory**，明确方法条件。' if g.get('kind')=='daily_overview' else '',
                         'paper_ids':[reference] if g.get('kind')!='daily_overview' else g['paper_ids'][:1],
                         'focus_directions':[t['name'] for t in g.get('focus_topics',[])] if g.get('kind')=='daily_overview' else [],
                         'daily_topics':daily_topics(g) if g.get('kind')=='daily_overview' else [],
                         'insufficient':False} for g in data['groups']]}
    monkeypatch.setattr(analysis.models,'complete',invalid)
    assert not await analysis._generate(uid,force=True)
    assert analysis.cached(uid)['items_json']==previous
    assert '当日论文' in analysis.cached(uid)['error']


def test_named_research_questions_can_be_bold_in_natural_paragraphs():
    short='今日研究聚焦**Zarankiewicz问题**与**Ramsey Theory**。\n\n重要成果明确了问题的适用条件。'
    assert analysis.valid_text('',short,daily=True)
    assert analysis.valid_text('1. **具体问题**：这里有一项有条件的明确进展。','')
    assert not analysis.valid_text('1. **具体问题**：进展。','额外短版')


@pytest.mark.parametrize('levels',['substantial','uncertain',('breakthrough','incremental')])
def test_daily_progress_is_brief_and_does_not_repeat_identical_labels(levels):
    levels=[levels]*2 if isinstance(levels,str) else list(levels)
    topics=[{'name':name,'progress':level,'paper_ids':[i]} for i,(name,level) in enumerate(zip(['主题甲','主题乙'],levels),1)]
    text=analysis.daily_overview_text(topics,[1,2],{1,2})
    assert '**主题甲**' in text and '**主题乙**' in text
    if levels[0]==levels[1]:
        assert '（' not in text
        if levels[0]=='uncertain':assert '尚难判断' in text and '进展。' not in text
        else:assert text.count(analysis.PROGRESS_LABELS[levels[0]])==1
    else:
        assert text=='今日研究聚焦**主题甲**和**主题乙**。'


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid',['missing','unknown_progress','uncovered_paper','duplicate_name','unrelated_evidence'])
async def test_invalid_structured_overview_keeps_previous_result(client,accounts,papers,batch,monkeypatch,invalid):
    topic,ids=interest_topic(batch,'Agent memory',2);uid=accounts[0]['user']['id'];interest(uid,topic)
    mocked(monkeypatch);assert await analysis.ensure_direction_trend(uid)
    before=analysis.cached(uid)['items_json']
    async def complete(feature,messages,**kwargs):
        payload=json.loads(messages[-1]['content']);items=[]
        for g in payload['groups']:
            daily=g.get('kind')=='daily_overview';topics=daily_topics(g) if daily else []
            if daily:
                if invalid=='missing':topics=[]
                elif invalid=='unknown_progress':topics[0]['progress']='solved conjecture with new bound'
                elif invalid=='uncovered_paper':topics[0]['paper_ids']=topics[0]['paper_ids'][:1]
                elif invalid=='duplicate_name':topics.append(dict(topics[0]))
                else:topics[0]['paper_ids']=[999999]
            items.append({'direction':g['direction'],'summary':'' if daily else FULL,'short_summary':'Unwanted detailed theorem results.',
                          'paper_ids':g['paper_ids'],'focus_directions':[t['name'] for t in g.get('focus_topics',[])] if daily else [],
                          'daily_topics':topics,'insufficient':False})
        return {'items':items}
    monkeypatch.setattr(analysis.models,'complete',complete)
    assert not await analysis._generate(uid,force=True)
    assert analysis.cached(uid)['items_json']==before


def test_overview_does_not_drop_fitting_evidence_after_four_papers(client,accounts,papers,batch):
    topic,ids=interest_topic(batch,'Agent memory',8);uid=accounts[0]['user']['id'];interest(uid,topic)
    result=materials(uid);overview=result['groups'][0];payload=analysis.model_payload(result)
    assert {p['id'] for p in overview['papers']}==set(ids)
    assert {p['id'] for p in payload['papers']}==set(ids)
    assert analysis.input_tokens(prompts.get('trend_report'))+analysis.input_tokens(dumps(payload))<=settings().trend_input_tokens-2000


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['reversed','duplicate','missing_evidence','unselected_evidence'])
async def test_natural_overview_keeps_interest_order_and_evidence_coverage(client,accounts,papers,batch,monkeypatch,mode):
    first,first_ids=interest_topic(batch,'Agent memory',1);second,second_ids=interest_topic(batch,'Retrieval grounding',1)
    uid=accounts[0]['user']['id']
    put_profile(uid,'## 核心兴趣（长期）\n- [w:1] Agent memory\n- [w:0.8] Retrieval grounding',
                {'category_selection':{'categories':[],'topics':{'arxiv:cs.AI':[first,second]}}},'manual')
    mocked(monkeypatch);assert await analysis.ensure_direction_trend(uid)
    previous=analysis.cached(uid)['items_json']
    async def invalid(feature,messages,**kwargs):
        data=json.loads(messages[-1]['content']);items=[]
        for group in data['groups']:
            daily=group.get('kind')=='daily_overview';selected=[t['name'] for t in group.get('focus_topics',[])] if daily else []
            ids=list(group['paper_ids'])
            if daily:
                if mode=='reversed':selected.reverse()
                elif mode=='duplicate':selected=[selected[0],selected[0]]
                elif mode=='missing_evidence':ids=first_ids
                else:selected=selected[:1]
            items.append({'direction':group['direction'],'summary':'' if daily else FULL,
                          'short_summary':'今日研究聚焦**Agent memory**和**Retrieval grounding**，成果明确了方法的适用条件。' if daily else '',
                          'focus_directions':selected,'paper_ids':ids,'insufficient':False})
        return {'items':items}
    monkeypatch.setattr(analysis.models,'complete',invalid)
    assert not await analysis._generate(uid,force=True)
    assert analysis.cached(uid)['items_json']==previous and analysis.cached(uid)['error']
