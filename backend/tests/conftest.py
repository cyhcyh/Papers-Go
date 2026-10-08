import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from app.config import settings, today, now
from app.db import execute, dumps, pack, set_paper_vector
from app.llm.ollama import ollama


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    monkeypatch.setenv('FRONTEND_DIR',str(Path(__file__).resolve().parents[2]/'frontend'/'dist'))
    monkeypatch.setenv('MODEL_KEY_FILE',str(tmp_path / 'secrets' / 'model-keys.key'))
    monkeypatch.setenv('MODEL_ENCRYPTION_KEY','')
    monkeypatch.setenv('SCHEDULER_ENABLED','false')
    monkeypatch.setenv('BOOTSTRAP_ENABLED','false')
    monkeypatch.setenv('PIPELINE_MODE','inline')
    monkeypatch.setenv('EMBEDDING_DIM','4')
    monkeypatch.setenv('JWT_SECRET','test-secret-is-at-least-thirty-two-characters')
    monkeypatch.setenv('LLM_API_KEY','')
    monkeypatch.setenv('REQUIRE_INVITE_CODE','false')
    settings.cache_clear()
    async def embed(texts): return [[1.,0.,0.,0.] for _ in texts]
    monkeypatch.setattr(ollama,'embed',embed)
    from app.llm import runtime
    original_complete=runtime.complete
    async def offline_trend(feature,messages,**kwargs):
        if feature=='interest_init':
            import json
            data=json.loads(messages[-1]['content'])
            if 'interests' in data:
                return {'interests':[{'original':t,'query':t+' — study its central research problem.'} for t in data['interests']]}
        if feature=='trend_report':
            import json
            return {'items':[{'direction':g['direction'],'insufficient':True,'summary':'','short_summary':'','paper_ids':[]} for g in json.loads(messages[-1]['content']).get('groups',[])]}
        return await original_complete(feature,messages,**kwargs)
    monkeypatch.setattr(runtime,'complete',offline_trend)
    original_configured=runtime.configured
    monkeypatch.setattr(runtime,'configured',lambda feature:True if feature=='interest_init' else original_configured(feature))
    from app.interest import profile_updates
    async def controlled_interest_queue():
        import asyncio
        await asyncio.Event().wait()
    # Tests explicitly consume saved drafts so model mocks and interleavings
    # remain deterministic; the real inline/worker lifecycle is tested separately.
    monkeypatch.setattr(profile_updates,'serve',controlled_interest_queue)
    from app import scheduler as task_scheduler
    monkeypatch.setattr(task_scheduler,'author_continuations',controlled_interest_queue)
    from app.pipeline import classify as classification
    from app.standard_topics import candidates
    async def offline_candidates(paper,blocked=(),limit=40):return candidates(paper,blocked,limit)
    monkeypatch.setattr(classification,'semantic_candidates',offline_candidates)
    async def offline_index():return 0
    monkeypatch.setattr(classification,'ensure_index',offline_index)
    from app.main import app
    with TestClient(app) as test_client:
        # Existing behavior tests use a small explicit fixture catalog; production starts with no enabled topics.
        from app.db import connect
        from .legacy_catalog import TOPICS, migrate_flat_topics
        from app.source_catalog import DEFAULT_ARXIV, DEFAULT_VENUES, ADDITIONAL_AI_VENUES, official_categories, invalidate
        with connect() as db:
            # Existing behavior tests simulate a configured site. Fresh-install
            # defaults are tested separately without this legacy fixture.
            official={s['code']:s for s in official_categories()}
            definitions=[('arxiv',code,label,1) for code,label in DEFAULT_ARXIV]+[('venue',code,code,int(code in DEFAULT_VENUES)) for code in DEFAULT_VENUES+ADDITIONAL_AI_VENUES]
            for order,(kind,code,label,enabled) in enumerate(definitions):
                db.execute('INSERT INTO source_categories(key,kind,code,label,fetch_enabled,guest_default,sort_order,feed_url,discipline,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (kind+':'+code,kind,code,label,enabled,enabled,order,'https://papers.cool/venue/'+code+'/feed' if kind=='venue' else None,official[code]['group'] if kind=='arxiv' else 'Computer Science',now()))
            for zh,en,parent in TOPICS:
                db.execute('INSERT INTO topics(name_zh,name_en,parent_id,created_at) VALUES(?,?,?,?)',(zh,en,parent,now()))
            migrate_flat_topics(db)
        invalidate()
        yield test_client
    from app import vector_store
    vector_store.close()
    settings.cache_clear()


@pytest.fixture
def accounts(client):
    a=client.post('/api/auth/register',json={'username':'alice','password':'secret123'}).json()
    b=client.post('/api/auth/register',json={'username':'bob','password':'secret123'}).json()
    return a,b


def headers(account):
    return {'Authorization':'Bearer '+account['access_token']}


def finish_interest_updates(client):
    from app.interest import profile_updates
    profile_updates.initialize()
    while job:=profile_updates.claim():
        assert client.portal.call(profile_updates.process,job), 'Interest preparation failed'


@pytest.fixture
def papers(client):
    ids=[]
    for i,(title,abstract,topic,vector) in enumerate([
      ('Graph Memory for Agents','We study agent memory. A verified result improves accuracy by 11 points.',3,[1.,0.,0.,0.]),
      ('Ramsey Graph Coloring','We study Ramsey theory and graph coloring.',15,[0.,1.,0.,0.]),
      ('Agent Evaluation Protocol','An evaluation method for agents.',4,[.8,.2,0.,0.])]):
        ident=execute('INSERT INTO papers(arxiv_id,title,abstract,authors,published,primary_category,created_at,ingested_date,tldr,scored,quality_score,base_quality_score) VALUES(?,?,?,?,?,?,?,?,?,1,50,50)',(f'2609.0000{i}',title,abstract,dumps(['Research Author' if i!=1 else 'Other Author']),today(),'cs.AI',now(),today(),'测试摘要'))
        execute('INSERT INTO paper_topics VALUES(?,?,.9)',(ident,topic))
        set_paper_vector(ident,vector)
        ids.append(ident)
    return ids
