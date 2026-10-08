import json
import pytest
from app.db import execute,one,rows
from app.config import now,today
from app.standard_topics import candidates,catalog
from app.pipeline.classify import classify_paper
from app.pipeline.topic_decision import predict_topic,ClassificationError
from app.llm import runtime
from .conftest import headers


def paper(title,abstract,category='cs.CL'):
    ident=execute('INSERT INTO papers(title,abstract,primary_category,created_at,ingested_date) VALUES(?,?,?,?,?)',(title,abstract,category,now(),today()))
    return one('SELECT * FROM papers WHERE id=?',(ident,))


def decision(key,evidence,**extra):
    return {'standard_key':key,'confidence':.9,'name_zh':'标准主题','reason':'对应论文的主要研究问题','evidence':evidence,'no_suitable_topic':False,**extra}


def test_candidates_recover_llm_direction_without_token_ring_and_deduplicate(client):
    p=paper('How Much Is an AI Token Worth?','We study scaling laws for large language model pretraining with AI-generated web text.')
    options=candidates(p)
    assert 'Natural Language Processing' in [e['label'] for e in candidates(p,semantic_scores={e['key']:.9 for e in catalog().values() if e['label']=='Natural Language Processing'})]
    assert len({e['code'].split('.')[-1] for e in options})==len(options)
    assert len(options)==40
    neural=[e for e in catalog().values() if e['label']=='Deep Learning']
    assert not any(e['label']=='Deep Learning' for e in candidates(p,{neural[0]['key']}))


@pytest.mark.asyncio
async def test_paraphrased_evidence_does_not_block_automatic_classification(client,accounts,monkeypatch):
    p=paper('Machine translation','We improve machine translation for ambiguous text.')
    key=next(e['key'] for e in candidates(p) if e['label']=='Natural Language Processing')
    async def complete(*a,**k):return decision(key,'Paraphrased abstract evidence')
    monkeypatch.setattr(runtime,'complete',complete)
    await classify_paper(p)
    assert one('SELECT classification_state FROM papers WHERE id=?',(p['id'],))['classification_state']=='awaiting_approval'
    assert client.get('/api/admin/classification-reviews',headers=headers(accounts[0])).status_code==404


@pytest.mark.asyncio
async def test_semantic_disagreement_does_not_override_model_choice(client,monkeypatch):
    p=paper('Training a tool agent','We train an agent with reinforcement learning in a synthetic environment.','cs.AI')
    opts=candidates(p);key=opts[-1]['key']
    for i,entry in enumerate(opts):entry['semantic_score']=.9 if i==0 else .4
    async def complete(*a,**k):return decision(key,'We train an agent with reinforcement learning',confidence=.99)
    monkeypatch.setattr(runtime,'complete',complete)
    value=await predict_topic(p,opts)
    assert not value.get('needs_review') and value['standard_key']==key


@pytest.mark.asyncio
async def test_invalid_code_retries_once_and_failure_retains_old_topic(client,papers,monkeypatch):
    p=one('SELECT * FROM papers WHERE id=?',(papers[0],));before=rows('SELECT * FROM paper_topics WHERE paper_id=?',(p['id'],));calls=[]
    async def complete(*a,**k):calls.append(1);return decision('CCS2012:not-a-candidate','We study agent memory.')
    monkeypatch.setattr(runtime,'complete',complete)
    with pytest.raises(ClassificationError,match='候选'):await classify_paper(p)
    assert len(calls)==2 and rows('SELECT * FROM paper_topics WHERE paper_id=?',(p['id'],))==before
    saved=one('SELECT * FROM paper_classifications WHERE paper_id=?',(p['id'],))
    assert saved['status']=='failed' and saved['attempts']==2 and '候选' in saved['error']
    assert any(json.loads(r['detail']).get('paper_id')==p['id'] for r in rows("SELECT detail FROM app_logs WHERE level='error' AND job='classify'"))


@pytest.mark.asyncio
async def test_reclassification_retains_previous_label_snapshot_and_one_topic(client,papers,monkeypatch):
    p=one('SELECT * FROM papers WHERE id=?',(papers[0],));key=next(e['key'] for e in candidates(p) if e['label']=='Multi-Agent Systems')
    async def complete(*a,**k):return decision(key,'We study agent memory.')
    monkeypatch.setattr(runtime,'complete',complete)
    await classify_paper(p)
    saved=one('SELECT * FROM paper_classifications WHERE paper_id=?',(p['id'],))
    assert json.loads(saved['previous_result'])
    assert len(rows('SELECT * FROM paper_topics WHERE paper_id=?',(p['id'],)))+len(rows('SELECT * FROM topic_pending_papers WHERE paper_id=?',(p['id'],)))==1


@pytest.mark.asyncio
async def test_high_confidence_suggestion_needs_no_scope_audit(client,monkeypatch):
    p=paper('A new bilingual task','We improve machine translation for ambiguous input.')
    opts=candidates(p);key=next(e['key'] for e in opts if e['label']=='Natural Language Processing')
    calls=[]
    async def complete(feature,messages,**kwargs):
        calls.append(messages[0]['content'])
        return decision(key,p['abstract'],no_suitable_topic=False)
    monkeypatch.setattr(runtime,'complete',complete)
    result=await predict_topic(p,opts)
    assert len(calls)==1
    assert 'needs_review' not in result and result['attempts']==1
