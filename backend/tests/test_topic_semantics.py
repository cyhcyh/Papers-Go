import pytest
from app import topic_semantics as semantics
from app.standard_topics import catalog
from app.llm import runtime
from app.config import settings


def test_retrieval_query_removes_formula_noise_without_changing_evidence():
    p={'title':'Bounds for $r$-uniform structures','abstract':r'We prove $a+b=c$ using \textbf{new bounds}.','primary_category':'math.CO'}
    text=semantics.paper_text(p)
    assert 'Research area: Combinatorics' in text and 'new bounds' in text
    assert '$' not in text and r'\textbf' not in text
    assert p['abstract']==r'We prove $a+b=c$ using \textbf{new bounds}.'


@pytest.mark.asyncio
async def test_semantic_candidates_use_full_paths_and_reuse_index(client,monkeypatch):
    entries=[]
    for label in ('Natural Language Processing','Computer Vision','Deep Learning'):
        entries.append(next(e for e in catalog().values() if e['label']==label))
    monkeypatch.setattr(semantics,'index_entries',lambda:entries)
    calls=[]
    async def embed(texts):
        calls.append(texts)
        return [[1.,0.,0.,0.] if 'Natural Language Processing' in t or 'bilingual' in t.casefold() else [0.,1.,0.,0.] for t in texts]
    monkeypatch.setattr(runtime,'embed',embed)
    p={'title':'Bilingual visual disambiguation','abstract':'Resolving ambiguities across languages.','primary_category':'cs.CL','venue':None,'categories':'[]'}
    first=await semantics.semantic_candidates(p)
    second=await semantics.semantic_candidates(p)
    assert any('Description:' in t and 'Research discipline:' in t for t in calls[0])
    assert len(calls)==3  # One catalogue batch, then one embedding for each paper.
    assert first[0]['label']=='Natural Language Processing' and second[0]['label']=='Natural Language Processing'
    assert first[0]['semantic_score']==pytest.approx(1)
    assert first[0]['semantic_rank']==1


@pytest.mark.asyncio
async def test_invalid_index_vectors_are_not_cached_and_build_can_resume(client,monkeypatch):
    entry=next(e for e in catalog().values() if e['label']=='Natural Language Processing')
    monkeypatch.setattr(semantics,'index_entries',lambda:[entry])
    async def invalid(texts):return [[float('nan'),0,0,0]]
    monkeypatch.setattr(runtime,'embed',invalid)
    with pytest.raises(ValueError,match='无效'):await semantics.ensure_index()
    with semantics.cache() as db:assert db.execute('SELECT COUNT(*) FROM entries').fetchone()[0]==0
    async def valid(texts):return [[1,0,0,0]]
    monkeypatch.setattr(runtime,'embed',valid)
    assert await semantics.ensure_index()==1
    with semantics.cache() as db:assert db.execute('SELECT COUNT(*) FROM entries').fetchone()[0]==1


@pytest.mark.asyncio
async def test_catalog_edit_reembeds_only_changed_area(client,monkeypatch):
    from app.db import connect
    from app.standard_topics import save_area
    first=next(e for e in catalog().values() if e['label']=='Computer Vision')
    second=next(e for e in catalog().values() if e['label']=='Natural Language Processing')
    monkeypatch.setattr(semantics,'index_entries',lambda:[catalog()[first['key']],catalog()[second['key']]])
    calls=[]
    async def embed(texts):calls.append(texts);return [[1,0,0,0] for _ in texts]
    monkeypatch.setattr(runtime,'embed',embed)
    assert await semantics.ensure_index()==2
    with connect() as db:save_area(db,first['key'],first['discipline'],first['label'],'Updated scope for the existing research direction.')
    assert await semantics.ensure_index()==2
    assert [len(batch) for batch in calls]==[2,1]
    assert 'Updated scope' in calls[1][0]
