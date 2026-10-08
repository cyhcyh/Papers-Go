import asyncio
import json
import math

import pytest

from app.config import settings, now, today
from app.db import connect, execute, one, rows, dumps, pack, validate_vectors
from app.pipeline import embed, quality, community, redo, fulltext_cache, parse
from app.llm import runtime as models
from app.interest.profile import put_profile, profile_embedding
from app import prompts
from .conftest import headers


@pytest.mark.parametrize('bad', [[0.,0.,0.,0.], [float('nan'),1.,0.,0.], [float('inf'),1.,0.,0.], [1.,2.], [True,0.,0.,0.], [1e-50,0.,0.,0.]])
def test_reject_bad_vectors_before_storage(client,bad):
    with pytest.raises(ValueError):pack(bad)
    with pytest.raises(ValueError):validate_vectors([bad],1)
    with pytest.raises(ValueError):validate_vectors([[1.,0.,0.,0.]],2)


def test_external_signals_are_bounded_and_monotonic():
    paper={'venue':None,'venue_rank':None,'author_impact':0,'hf_upvotes':0,'github_stars':0}
    assessment={'contribution':80,'evidence':80}
    assert quality.quality_score(paper,assessment)==80
    previous=80
    for votes in (1,2,10,10000):
        current=quality.quality_score({**paper,'hf_upvotes':votes},assessment)
        assert previous<=current<=82
        previous=current
    assert quality.quality_score({**paper,'author_impact':100},assessment)==83
    assert quality.quality_score({**paper,'venue':'ICML','venue_rank':'oral'},assessment)>quality.quality_score({**paper,'venue':'ICML','venue_rank':'spotlight'},assessment)>quality.quality_score({**paper,'venue':'ICML'},assessment)
    assert quality.quality_score({**paper,'hf_upvotes':10000,'author_impact':100},{'contribution':100,'evidence':100})==100
    assert quality.components(paper,{'contribution':80,'evidence':None})['content']==80
    assert quality.components(paper,{'contribution':80,'evidence':0})['content']==80


def test_pending_quality_and_true_zero_are_distinct(client,papers,accounts):
    ident=execute('INSERT INTO papers(title,abstract,primary_category,published,ingested_date,created_at) VALUES(?,?,?,?,?,?)',('Pending','abstract','cs.AI',today(),today(),now()))
    execute('UPDATE papers SET author_impact=100 WHERE id=?',(ident,))
    with connect() as db:quality.recompute(db,ident)
    assert one('SELECT quality_score,base_quality_score FROM papers WHERE id=?',(ident,))=={'quality_score':50,'base_quality_score':None}
    execute('UPDATE papers SET scored=1,quality_score=0 WHERE id=?',(papers[0],))
    response=client.get('/api/browse?range=all&sort=date',headers=headers(accounts[0])).json()['items']
    by_id={p['id']:p for p in response}
    assert by_id[ident]['quality_status']=='pending' and by_id[ident]['quality_score'] is None
    assert by_id[papers[0]]['quality_score']==0 and by_id[papers[0]]['score']<by_id[ident]['score']


@pytest.mark.asyncio
async def test_quality_uses_one_call_and_cached_math_evidence_without_download(client,papers,monkeypatch):
    ident=papers[0];execute('UPDATE papers SET primary_category=?,scored=0 WHERE id=?',('math.CO',ident))
    fulltext_cache.store(ident,{'sections':[{'section':'Proof','text':'A proof of the bound.'}], 'text':'Proof\nA proof of the bound.'})
    calls=[]
    async def forbidden(*a,**k):raise AssertionError('quality must never download PDF')
    async def complete(feature,messages,**kwargs):
        calls.append((feature,messages,kwargs))
        return kwargs['validate']({'summary':'通过新构造改进已有界','contribution':80,'evidence_insufficient':True})
    monkeypatch.setattr(parse,'ensure_fulltext',forbidden);monkeypatch.setattr(models,'complete',complete)
    await embed.score_paper(one('SELECT * FROM papers WHERE id=?',(ident,)))
    saved=one('SELECT * FROM papers WHERE id=?',(ident,));assessment=json.loads(saved['skeleton'])
    assert len(calls)==1 and calls[0][0]=='quality'
    assert assessment['prompt_id']=='quality' and assessment['material_level']=='cached_excerpts'
    assert assessment['prompt_version']=='quality-contribution-v1.1' and assessment['formula_version']=='quality-v3'
    assert saved['quality_score']==80 and assessment['evidence_insufficient']
    assert 'evidence' not in assessment
    assert list(calls[0][2]['schema']['properties'])==['summary','contribution','evidence_insufficient']
    assert json.loads(calls[0][1][1]['content'])['excerpts'][0]['section']=='Proof'


@pytest.mark.asyncio
async def test_invalid_assessment_retains_old_result(client,papers,monkeypatch):
    ident=papers[0];old=one('SELECT quality_score,skeleton,scored FROM papers WHERE id=?',(ident,))
    async def invalid(*args,**kwargs):return {'contribution':999,'evidence':70,'summary':'bad'}
    monkeypatch.setattr(models,'complete',invalid)
    with pytest.raises(ValueError):await embed.score_paper(one('SELECT * FROM papers WHERE id=?',(ident,)))
    assert one('SELECT quality_score,skeleton,scored FROM papers WHERE id=?',(ident,))==old


def test_updates_recompute_from_assessment_without_drift(client,papers):
    ident=papers[0];assessment={'contribution':80,'evidence':70}
    execute('UPDATE papers SET skeleton=?,scored=1,author_impact=100 WHERE id=?',(dumps(assessment),ident))
    with connect() as db:
        quality.recompute(db,ident)
        community.update_signal(db,'hf_upvotes',1,'id=?',(ident,))
    first=one('SELECT quality_score FROM papers WHERE id=?',(ident,))['quality_score']
    assert first==83.04
    with connect() as db:
        community.update_signal(db,'hf_upvotes',1,'id=?',(ident,))
        quality.recompute(db,ident)
    assert one('SELECT quality_score FROM papers WHERE id=?',(ident,))['quality_score']==first
    execute('UPDATE papers SET authors=? WHERE id=?',(dumps(['New author']),ident))
    assert one('SELECT quality_score FROM papers WHERE id=?',(ident,))['quality_score']==80.04


@pytest.mark.asyncio
async def test_profiles_finish_before_quality_and_cloud_concurrency_is_two(client,papers,accounts,monkeypatch):
    put_profile(accounts[0]['user']['id'],'## 核心兴趣\n- [w:0.7] Graph theory',{},'test')
    execute('UPDATE papers SET scored=0')
    active=peak=0
    async def score(paper):
        nonlocal active,peak
        assert one('SELECT embedding FROM interest_profile')['embedding'] is not None
        active+=1;peak=max(peak,active)
        await asyncio.sleep(.02)
        execute('UPDATE papers SET scored=1 WHERE id=?',(paper['id'],));active-=1
    monkeypatch.setattr(models,'concurrency',lambda feature:2)
    monkeypatch.setattr(embed,'score_paper',score)
    await embed.build_vectors()
    assert peak==0
    await embed.assess_quality()
    assert peak==2
    progress=json.loads(one("SELECT progress FROM source_status WHERE name='build_vectors'")['progress'])
    assert [s['key'] for s in progress['stages']]==['embedding','profile_embedding']
    assert progress['pending']==0
    assert json.loads(one("SELECT progress FROM source_status WHERE name='assess_quality'")['progress'])['completed']==3


@pytest.mark.asyncio
async def test_invalid_profile_vector_count_is_rejected(client,monkeypatch):
    async def short(texts):return [[1.,0.,0.,0.]]
    monkeypatch.setattr(models,'embed',short)
    assert await profile_embedding('## 核心兴趣\n- [w:1] graph\n- [w:1] robot') is None


def test_prompt_version_is_frozen_with_text(client,accounts):
    original=prompts.revision('quality')
    with models.model_snapshot():
        client.put('/api/admin/prompts/quality',headers=headers(accounts[0]),json={'text':'新的统一评估指令'})
        assert prompts.revision('quality')==original
    assert prompts.revision('quality')!=original


def test_sample_redo_selects_only_requested_papers(client,papers):
    options=redo.RedoOptions(paper_ids=papers[:2],components=['embedding'])
    assert redo.preview('build_vectors',options)=={'papers':2,'profiles':0,'components':['embedding'],'operations':2}


def test_discipline_and_material_limits():
    assert quality.discipline({'primary_category':'math.CO'})==('数学','quality')
    assert quality.discipline({'primary_category':'cs.LG'})==('计算机科学','quality')
    assert quality.discipline({'primary_category':None,'venue':'ICLR'})==('计算机科学','quality')
    assert quality.discipline({'primary_category':'quant-ph'})==('物理学','quality')
    key,data=embed.quality_material({'title':'Title','abstract':'A'*9999,'primary_category':'math.CO'}, {'sections':[{'section':'Proof','text':'P'*99999}]*6})
    assert len(data['abstract'])==5000 and len(data['excerpts'])==3 and len(dumps(data))<12000


def test_trend_average_excludes_pending_and_updates_on_neutral_assessment(client,papers):
    from app.pipeline.trends import trend_stats
    ident=papers[0];topic=one('SELECT topic_id FROM paper_topics WHERE paper_id=?',(ident,))['topic_id']
    execute('UPDATE papers SET scored=0 WHERE id=?',(ident,));trend_stats()
    assert one('SELECT avg_quality FROM topic_daily_stats WHERE topic_id=?',(topic,))['avg_quality'] is None
    execute('UPDATE papers SET scored=1 WHERE id=?',(ident,));trend_stats()
    assert one('SELECT avg_quality FROM topic_daily_stats WHERE topic_id=?',(topic,))['avg_quality']==50


@pytest.mark.parametrize('assessment', [
    {'contribution':None,'evidence':None}, {'novelty':None,'rigor':None}, {}])
def test_all_unknown_has_no_quality_even_with_strong_external_signals(assessment):
    paper={'venue':'ICML','venue_rank':'oral','author_impact':100,'hf_upvotes':10000,'github_stars':10000}
    assert quality.quality_score(paper,assessment) is None
    assert quality.components(paper,assessment)['base'] is None
    assert quality.quality_score(paper,{'contribution':0,'evidence':0}) is not None
    assert quality.components(paper,{'contribution':None,'evidence':80})['content'] is None


@pytest.mark.asyncio
async def test_unknown_result_stays_completed_neutral_for_ranking_and_survives_signal_updates(client,papers,monkeypatch):
    ident=papers[0];calls=[]
    async def complete(feature,messages,**kwargs):
        calls.append(feature)
        return kwargs['validate']({'summary':'现有材料无法理解研究内容','contribution':None,'evidence_insufficient':False})
    monkeypatch.setattr(models,'complete',complete)
    await embed.score_paper(one('SELECT * FROM papers WHERE id=?',(ident,)))
    saved=one('SELECT * FROM papers WHERE id=?',(ident,))
    assert saved['scored']==1 and saved['quality_score'] is None and saved['base_quality_score'] is None
    unknown=client.get('/api/browse?range=all&sort=date').json()['items']
    item=next(p for p in unknown if p['id']==ident)
    assert item['quality_score'] is None and item['quality_status']=='limited' and math.isfinite(item['score'])
    unknown_score=item['score']
    execute('UPDATE papers SET scored=0 WHERE id=?',(ident,))
    pending=client.get('/api/browse?range=all&sort=date').json()['items']
    assert next(p for p in pending if p['id']==ident)['score']==unknown_score
    execute('UPDATE papers SET scored=1 WHERE id=?',(ident,))
    with connect() as db:
        community.update_signal(db,'hf_upvotes',100,'id=?',(ident,))
        db.execute('UPDATE papers SET author_impact=100 WHERE id=?',(ident,))
        quality.recompute(db,ident)
    execute('UPDATE papers SET authors=? WHERE id=?',(dumps(['New author']),ident))
    assert one('SELECT quality_score,base_quality_score,community_bonus FROM papers WHERE id=?',(ident,))=={
        'quality_score':None,'base_quality_score':None,'community_bonus':0}
    await embed.assess_quality()
    assert calls==['quality']


def test_unknown_migration_clears_only_unknown_scores_and_keeps_assessments(client,papers):
    unknown,valid,legacy=papers
    raw=dumps({'contribution':None,'evidence':None,'summary':'材料不足'})
    execute('UPDATE papers SET skeleton=?,quality_score=55,base_quality_score=50,community_bonus=2 WHERE id=?',(raw,unknown))
    old=one('SELECT quality_score,base_quality_score,scored,skeleton FROM papers WHERE id=?',(valid,))
    execute('UPDATE papers SET skeleton=? WHERE id=?',(dumps({'novelty':None,'rigor':None}),legacy))
    execute('DELETE FROM app_migrations WHERE name=?',(quality.VERSION,))
    with connect() as db:quality.initialize(db)
    assert one('SELECT quality_score,base_quality_score,scored,skeleton FROM papers WHERE id=?',(unknown,))=={
        'quality_score':None,'base_quality_score':None,'scored':1,'skeleton':raw}
    assert one('SELECT quality_score FROM papers WHERE id=?',(legacy,))['quality_score'] is None
    assert one('SELECT quality_score,base_quality_score,scored,skeleton FROM papers WHERE id=?',(valid,))==old
    with connect() as db:quality.initialize(db)
    assert one('SELECT quality_score FROM papers WHERE id=?',(unknown,))['quality_score'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('primary', ['math.CO','cs.LG','quant-ph'])
async def test_all_disciplines_use_same_admin_customizable_skill(client,accounts,papers,monkeypatch,primary):
    custom='统一的管理员评分指令'
    assert client.put('/api/admin/prompts/quality',headers=headers(accounts[0]),json={'text':custom}).status_code==200
    execute('UPDATE papers SET primary_category=? WHERE id=?',(primary,papers[0]))
    calls=[]
    async def complete(feature,messages,**kwargs):
        calls.append(messages)
        return kwargs['validate']({'summary':'新算法改善运行时间','contribution':70,'evidence_insufficient':True})
    monkeypatch.setattr(models,'complete',complete)
    await embed.score_paper(one('SELECT * FROM papers WHERE id=?',(papers[0],)))
    assert len(calls)==1 and calls[0][0]['content']==custom
    assert json.loads(one('SELECT skeleton FROM papers WHERE id=?',(papers[0],))['skeleton'])['prompt_id']=='quality'


def test_single_contribution_schema_preserves_material_flag():
    value=embed.QualityAssessment.model_validate({'summary':'新构造改进已有界','contribution':85,'evidence_insufficient':False})
    assert not value.evidence_insufficient and value.contribution==85
    unknown=embed.QualityAssessment.model_validate({'summary':'材料为空','contribution':None,'evidence_insufficient':False})
    assert unknown.evidence_insufficient


def test_contribution_migration_reuses_judgments_and_keeps_old_metadata(client,papers):
    records=[
        {'contribution':5,'evidence':None,'summary':'直接反例','prompt_version':'quality-reference-v1','formula_version':'quality-v2.1'},
        {'novelty':85,'rigor':None,'summary':'新定理'},
        {'contribution':None,'evidence':80,'summary':'未知贡献'},
    ]
    for ident,value in zip(papers,records):
        execute('UPDATE papers SET skeleton=?,quality_score=50,base_quality_score=50 WHERE id=?',(dumps(value),ident))
    before=rows('SELECT id,skeleton,scored,embedding FROM papers ORDER BY id')
    execute('DELETE FROM app_migrations WHERE name=?',(quality.VERSION,))
    with connect() as db:quality.initialize(db)
    assert [r['quality_score'] for r in rows('SELECT quality_score FROM papers ORDER BY id')]==[5,85,None]
    assert rows('SELECT id,skeleton,scored,embedding FROM papers ORDER BY id')==before
    with connect() as db:quality.initialize(db)
    assert [r['quality_score'] for r in rows('SELECT quality_score FROM papers ORDER BY id')]==[5,85,None]
