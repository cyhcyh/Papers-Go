import json
from datetime import date,timedelta
import pytest
from app import prompts
from app.config import now,today,settings,Settings
from app.db import execute,dumps,one,rows
from app.interest.profile import put_profile
from app.pipeline import direction_trends as analysis
from .test_arxiv_daily import batch,interest_topic,mocked,FULL,daily_topics
from .conftest import headers


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
    assert evidence['directions']==[g['direction'] for g in payload['groups'] if ids[0] in g['allowed_paper_ids']]
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


def test_default_budget_changes_without_overriding_explicit_configuration(monkeypatch):
    monkeypatch.delenv('TREND_INPUT_TOKENS',raising=False)
    assert Settings(_env_file=None).trend_input_tokens==20000
    assert Settings(_env_file=None,trend_input_tokens=8000).trend_input_tokens==8000


def test_output_format_rules_preserve_administrator_prompt(monkeypatch):
    custom='管理员保留的研究编辑要求。'
    monkeypatch.setattr(prompts,'get',lambda key:custom)
    for key in ('trend_report','trend_rewrite'):
        effective=analysis._prompt(key)
        assert effective.startswith(custom+'\n\n')
        assert '$...$' in effective and '$$...$$' in effective
        assert '两个反斜杠' in effective and '(paper:' in effective
    assert prompts.get('trend_report')==custom


def test_larger_budget_keeps_current_and_reference_sample_limits(client,accounts,papers,batch,monkeypatch):
    topic,ids=interest_topic(batch,'Agent memory',15);uid=accounts[0]['user']['id'];interest(uid,topic)
    refs=[old_paper(topic,age) for age in range(1,7)]
    for pid in [*ids,*refs]:
        execute('UPDATE papers SET abstract=? WHERE id=?',('Agent memory theory and task conditions. '*45,pid))
    monkeypatch.setattr(settings(),'trend_input_tokens',8000)
    small=next(g for g in materials(uid)['groups'] if g.get('kind')!='daily_overview')
    monkeypatch.setattr(settings(),'trend_input_tokens',20000)
    large=next(g for g in materials(uid)['groups'] if g.get('kind')!='daily_overview')
    assert len(large['papers'])==12 and len(large['reference_papers'])==4
    assert len(large['papers'])>len(small['papers'])
    assert [p['id'] for p in large['papers']][:len(small['papers'])]==[p['id'] for p in small['papers']]


@pytest.mark.asyncio
@pytest.mark.parametrize('length',[0,1500,3600])
async def test_cited_formula_report_keeps_source_urls_and_uses_one_call(client,accounts,papers,batch,monkeypatch,length):
    topic,ids=interest_topic(batch,'Agent memory',1);uid=accounts[0]['user']['id'];interest(uid,topic)
    reference=old_paper(topic,3);calls=[]
    for pid in [ids[0],reference]:
        execute('UPDATE papers SET abs_url=? WHERE id=?',(f'https://arxiv.org/abs/2610.{pid:05d}',pid))
    full=f'当日研究明确了方法的适用条件。\n\n1. **主要进展**。在[任务条件研究](paper:{ids[0]})中，复杂度为 $\\Theta(n\\sqrt{{\\log n}})$。与[近期对照](paper:{reference})比较时仍须保留研究条件。'
    if length:full+=('研究条件与原文一致。'*400)[:length-len(full)]
    async def complete(feature,messages,**kwargs):
        payload=json.loads(messages[-1]['content']);calls.append(messages);items=[]
        for group in payload['groups']:
            daily=group.get('kind')=='daily_overview';selected=group['paper_ids'][:1] if daily else [ids[0],reference]
            items.append({'direction':group['direction'],'summary':'' if daily else full,'short_summary':'',
                'paper_ids':selected,'focus_directions':[t['name'] for t in group.get('focus_topics',[])] if daily else [],
                'daily_topics':daily_topics(group,selected) if daily else [],'insufficient':False})
        return json.loads(dumps({'items':items}))
    monkeypatch.setattr(analysis.models,'complete',complete)
    assert await analysis.ensure_direction_trend(uid) and len(calls)==1
    report=analysis.trend_summary(uid)
    assert report['items'][1]['summary']==full
    assert all(p['url']==f"https://arxiv.org/abs/2610.{p['id']:05d}" for p in report['papers'])
    assert {p['id'] for p in report['papers']}=={ids[0],reference}
    assert '两个反斜杠' in calls[0][0]['content']
    assert not await analysis.ensure_direction_trend(uid) and len(calls)==1
    response=client.get('/api/trends?include_movements=false',headers=headers(accounts[0]))
    assert response.status_code==200
    data=response.json()
    assert data['report']['papers']==report['papers']
    assert data['report']['items'][1]['summary']==full and len(calls)==1


def test_full_report_limit_preserves_short_overview_rules():
    prefix='本期概述。\n\n1. **研究进展**\n'
    full=prefix+'研'*(3600-len(prefix))
    assert analysis.FULL_MAX==3600 and analysis.valid_text(full,'')
    assert not analysis.valid_text(full+'研','')
    assert not analysis.valid_text(full,'非空短版')
    assert analysis.valid_text('','今日研究聚焦**具体问题**。',daily=True)
    assert not analysis.valid_text('','研'*401,daily=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('citation',['999999','not-an-id'])
async def test_bad_inline_reference_keeps_previous_report(client,accounts,papers,batch,monkeypatch,citation):
    topic,_=interest_topic(batch,'Agent memory',1);uid=accounts[0]['user']['id'];interest(uid,topic)
    mocked(monkeypatch);assert await analysis.ensure_direction_trend(uid)
    previous=analysis.cached(uid)['items_json']
    original=analysis.models.complete
    async def invalid(*args,**kwargs):
        result=await original(*args,**kwargs)
        for item in result['items']:
            if item['summary']:item['summary']+=f' [未列入证据的论文](paper:{citation})'
        return result
    monkeypatch.setattr(analysis.models,'complete',invalid)
    assert not await analysis._generate(uid,force=True)
    assert analysis.cached(uid)['items_json']==previous
    assert '趋势论文引用' in analysis.cached(uid)['error']


@pytest.mark.asyncio
async def test_sixteen_assigned_references_are_accepted_in_one_call(client,accounts,papers,batch,monkeypatch):
    topic,ids=interest_topic(batch,'Agent memory',12);uid=accounts[0]['user']['id'];interest(uid,topic)
    refs=[old_paper(topic,age) for age in range(1,5)];calls=[]
    async def complete(feature,messages,**kwargs):
        data=json.loads(messages[-1]['content']);calls.append(kwargs['schema']);items=[]
        for group in data['groups']:
            daily=group.get('kind')=='daily_overview';evidence=group['allowed_paper_ids']
            assert evidence==list(dict.fromkeys(group['paper_ids']+group['reference_ids']))
            items.append({'direction':group['direction'],'summary':'' if daily else FULL,'short_summary':'',
                          'paper_ids':evidence,'focus_directions':[t['name'] for t in group.get('focus_topics',[])] if daily else [],
                          'daily_topics':daily_topics(group,evidence) if daily else [],'insufficient':False})
        return {'items':items}
    monkeypatch.setattr(analysis.models,'complete',complete)
    assert await analysis.ensure_direction_trend(uid) and len(calls)==1
    evidence=analysis.trend_summary(uid)['papers']
    assert {p['id'] for p in evidence if p['direction']=='Agent memory'}==set(ids+refs)
    variants=calls[0]['properties']['items']['items']['oneOf']
    item_schema=variants[0]
    assert item_schema['properties']['paper_ids']['maxItems']==16
    assert item_schema['properties']['paper_ids']['uniqueItems'] is True
    assert item_schema['properties']['daily_topics']['items']['properties']['paper_ids']['maxItems']==16
    for group in materials(uid)['groups']:
        branch=next(v['properties'] for v in variants if v['properties']['direction']['enum']==[group['direction']])
        assert set(branch['paper_ids']['items']['enum'])=={p['id'] for p in group['papers']+group['reference_papers']}
        if group.get('kind')=='daily_overview':
            assert branch['focus_directions']['maxItems']==0
    assert '1～16' in analysis._prompt('trend_report')


@pytest.mark.parametrize('ids,reason',[
    (['1'],'invalid_type'),([True],'invalid_type'),(None,'invalid_type'),
    (list(range(1,18)),'invalid_count'),([],'invalid_count'),
    ([1,1],'duplicate_ids'),([1,999],'outside_group'),([2],'missing_current')])
def test_evidence_failure_logs_the_specific_reason_without_truncation(client,ids,reason):
    group={'direction':'通用方向','papers':[{'id':1}],'reference_papers':[{'id':2}]}
    before=list(ids) if isinstance(ids,list) else ids
    with pytest.raises(ValueError):analysis.validate_evidence(group,ids)
    assert ids==before
    detail=json.loads(one("SELECT detail FROM app_logs WHERE message='趋势论文证据校验未通过' ORDER BY id DESC LIMIT 1")['detail'])
    assert detail['reason']==reason and detail['allowed_ids']==[1,2]
    assert detail['evidence_count']==(len(ids) if isinstance(ids,list) else None)


@pytest.mark.asyncio
async def test_other_direction_paper_is_rejected_even_when_in_shared_pool(client,accounts,papers,batch,monkeypatch):
    first,_=interest_topic(batch,'Agent memory',1);second,_=interest_topic(batch,'Retrieval grounding',1);uid=accounts[0]['user']['id']
    put_profile(uid,'## 核心兴趣（长期）\n- [w:1] Agent memory\n- [w:0.8] Retrieval grounding',
                {'category_selection':{'categories':[],'topics':{'arxiv:cs.AI':[first,second]}}},'manual')
    calls=mocked(monkeypatch);assert await analysis.ensure_direction_trend(uid)
    previous=analysis.cached(uid)['items_json'];original=analysis.models.complete
    async def invalid(feature,messages,**kwargs):
        result=await original(feature,messages,**kwargs);payload=json.loads(messages[-1]['content'])
        foreign=next(g for g in payload['groups'] if g['direction']=='Retrieval grounding')['paper_ids'][0]
        assert foreign in {p['id'] for p in payload['papers']}
        next(i for i in result['items'] if i['direction']=='Agent memory')['paper_ids'].append(foreign)
        return result
    monkeypatch.setattr(analysis.models,'complete',invalid)
    assert not await analysis._generate(uid,force=True) and len(calls)==2
    assert analysis.cached(uid)['items_json']==previous
    assert '未分配给本方向' in analysis.cached(uid)['error']
    detail=json.loads(one("SELECT detail FROM app_logs WHERE message='趋势论文证据校验未通过' ORDER BY id DESC LIMIT 1")['detail'])
    assert detail['reason']=='outside_group' and len(detail['invalid_ids'])==1


@pytest.mark.asyncio
async def test_failed_report_marks_task_failed_but_cached_report_is_a_normal_skip(client,accounts,papers,batch,monkeypatch):
    import asyncio
    from app import scheduler
    from app.pipeline import trends
    topic,_=interest_topic(batch,'Agent memory',1);uid=accounts[0]['user']['id'];interest(uid,topic)
    calls=mocked(monkeypatch)
    assert await trends.trend_report()==1
    assert await trends.trend_report()==0 and len(calls)==1
    original=analysis.models.complete
    async def invalid(*args,**kwargs):
        result=await original(*args,**kwargs)
        result['items'][0]['paper_ids']=list(range(1,18))
        return result
    monkeypatch.setattr(analysis.models,'complete',invalid)
    previous=analysis.cached(uid)['items_json']
    assert not await analysis._generate(uid,force=True)
    await scheduler._run_job('trend_report',None,asyncio.current_task())
    status=one("SELECT * FROM source_status WHERE name='trend_report'")
    assert status['error'] and '1 份未完成' in status['error'] and not status['running']
    assert status['last_success'] is None
    assert analysis.cached(uid)['items_json']==previous and len(calls)==2
    assert one("SELECT message FROM app_logs WHERE kind='task' AND job='trend_report' ORDER BY id DESC LIMIT 1")['message']=='任务失败'
    detail=json.loads(one("SELECT detail FROM app_logs WHERE message='趋势整理结果' ORDER BY id DESC LIMIT 1")['detail'])
    assert detail['completed']==0 and detail['failed']==1


def test_schema_still_allows_insufficient_evidence_with_empty_arrays():
    schema=analysis.report_schema([{'direction':'通用方向','papers':[{'id':1}],'reference_papers':[]}])
    item=schema['properties']['items']['items']['oneOf'][0]
    assert item['properties']['paper_ids'].get('minItems',0)==0
    assert item['properties']['daily_topics'].get('minItems',0)==0
    assert item['properties']['direction']['enum']==['通用方向']


def test_shared_evidence_requires_both_direction_match_and_supplied_material():
    first={'id':1,'evidence_kind':'current'}
    shared={1:first,2:{'id':2,'evidence_kind':'current'},3:{'id':3,'evidence_kind':'current'},
            5:{'id':5,'evidence_kind':'reference'}}
    group={'direction':'通用方向','papers':[first],'reference_papers':[],'_matched_ids':{1,2,4,5}}
    allowed,current_ids=analysis.evidence_scope(group,shared)
    assert set(allowed)=={1,2,5} and current_ids=={1,2}
    analysis.validate_evidence(group,[2,5],shared)
    for excluded in (3,4):
        with pytest.raises(ValueError,match='未分配给本方向'):
            analysis.validate_evidence(group,[1,excluded],shared)
    with pytest.raises(ValueError,match='当日论文'):
        analysis.validate_evidence(group,[5],shared)
    daily={**group,'kind':'daily_overview'}
    assert set(analysis.evidence_scope(daily,shared)[0])=={1}
    variant=analysis.report_schema([group],shared)['properties']['items']['items']['oneOf'][0]
    assert variant['properties']['paper_ids']['items']['enum']==[1,2,5]
    assert variant['properties']['paper_ids']['maxItems']==16


@pytest.mark.asyncio
async def test_matching_overview_paper_can_support_full_report_without_resampling(client,accounts,papers,batch,monkeypatch):
    topic,ids=interest_topic(batch,'Agent memory',2);uid=accounts[0]['user']['id'];interest(uid,topic)
    source=materials(uid);full=next(g for g in source['groups'] if g.get('kind')!='daily_overview')
    reused=ids[1]
    full['papers']=[p for p in full['papers'] if p['id']!=reused];full['sampled_count']=len(full['papers'])
    assert reused in full['_matched_ids']
    assert reused in {p['id'] for p in source['groups'][0]['papers']}
    execute('UPDATE papers SET abs_url=? WHERE id=?',('https://arxiv.org/abs/2610.12345',reused))
    payload=analysis.model_payload(source);assigned=next(g for g in payload['groups'] if g['direction']==full['direction'])
    assert reused in assigned['allowed_paper_ids'] and reused in assigned['paper_ids']
    assert all('_matched_ids' not in g for g in payload['groups'])
    assert len(payload['papers'])==len({p['id'] for g in source['groups'] for p in g['papers']+g['reference_papers']})
    assert next(p for p in payload['papers'] if p['id']==reused)['directions']==[g['direction'] for g in payload['groups']]
    shared={p['id']:p for p in payload['papers']}
    tokens=analysis.input_tokens(analysis._prompt('trend_report'))+analysis.input_tokens(dumps(payload))+analysis.input_tokens(dumps(analysis.report_schema(source['groups'],shared)))
    assert tokens<=settings().trend_input_tokens-2000
    monkeypatch.setattr(analysis,'research_materials',lambda *args,**kwargs:source)
    calls=mocked(monkeypatch);original=analysis.models.complete
    async def complete(*args,**kwargs):
        result=await original(*args,**kwargs)
        item=next(i for i in result['items'] if i['direction']==full['direction'])
        item.update(paper_ids=[reused],summary=f'当日材料明确了方法的适用范围。\n\n1. **方法与任务条件**：[当日研究](paper:{reused})说明结论取决于所研究的任务条件。')
        return result
    monkeypatch.setattr(analysis.models,'complete',complete)
    assert await analysis.ensure_direction_trend(uid) and len(calls)==1
    report=analysis.trend_summary(uid)
    evidence=next(p for p in report['papers'] if p['direction']==full['direction'])
    assert evidence['id']==reused and evidence['historical'] is False
    assert evidence['url']=='https://arxiv.org/abs/2610.12345'
    assert next(i for i in report['items'] if i['direction']==full['direction'])['sampled_count']==1
    assert reused not in {p['id'] for p in full['papers']}


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy_field',[False,True])
async def test_daily_interest_coverage_is_computed_from_verified_evidence(client,accounts,papers,batch,monkeypatch,legacy_field):
    first,first_ids=interest_topic(batch,'Agent memory',1);second,_=interest_topic(batch,'Retrieval grounding',1);uid=accounts[0]['user']['id']
    put_profile(uid,'## 核心兴趣（长期）\n- [w:1] Agent memory\n- [w:0.8] Retrieval grounding',
                {'category_selection':{'categories':[],'topics':{'arxiv:cs.AI':[first,second]}}},'manual')
    calls=mocked(monkeypatch);original=analysis.models.complete
    async def complete(*args,**kwargs):
        result=await original(*args,**kwargs)
        daily=result['items'][0]
        daily['paper_ids']=first_ids
        daily['daily_topics']=[{'name':'Agent memory','progress':'substantial','paper_ids':first_ids}]
        if not legacy_field:daily['focus_directions']=[]
        return result
    monkeypatch.setattr(analysis.models,'complete',complete)
    assert await analysis.ensure_direction_trend(uid) and len(calls)==1
    overview=analysis.trend_summary(uid)['items'][0]['short_summary']
    assert '**Agent memory**' in overview and 'Retrieval grounding' not in overview


@pytest.mark.asyncio
@pytest.mark.parametrize('topics,accepted',[('omitted',True),([],True),(None,False),([{}],False)])
async def test_unused_full_report_topics_may_be_omitted_but_never_contain_overview_data(client,accounts,papers,batch,monkeypatch,topics,accepted):
    topic,_=interest_topic(batch,'Agent memory',1);uid=accounts[0]['user']['id'];interest(uid,topic)
    calls=mocked(monkeypatch);original=analysis.models.complete
    async def complete(*args,**kwargs):
        result=await original(*args,**kwargs)
        item=next(i for i in result['items'] if i['direction']=='Agent memory')
        if topics=='omitted':
            item.pop('daily_topics');item.pop('focus_directions')
        else:item['daily_topics']=topics
        return result
    monkeypatch.setattr(analysis.models,'complete',complete)
    assert await analysis.ensure_direction_trend(uid) is accepted
    assert len(calls)==1
