import asyncio
import json

import pytest
from cryptography.fernet import Fernet
from app.config import today, now, settings
from app.db import rows, one, execute, dumps
from app.interest.profile import current, put_profile
from app.llm import runtime
from .conftest import headers, finish_interest_updates


def test_home_filters_subscriptions_but_browse_remains_available(client, accounts, papers):
    account = accounts[0]
    auth = headers(account)
    execute('UPDATE papers SET primary_category=?,categories=? WHERE id=?', ('quant-ph',dumps(['quant-ph','math.CO']),papers[1]))
    execute('UPDATE papers SET primary_category=? WHERE id=?', ('cs.CL',papers[2]))
    execute('DELETE FROM paper_topics WHERE paper_id=?',(papers[2],))
    execute('INSERT INTO paper_topics VALUES(?,?,.9)',(papers[2],3))
    selection = {'categories':['arxiv:math.CO'],'topics':{}}
    assert client.post('/api/profile/init',headers=auth,json={'category_selection':selection}).status_code==200
    finish_interest_updates(client)
    feed = client.get('/api/feed/today',headers=auth).json()
    assert [p['id'] for p in feed['items']] == [papers[1]]
    assert feed['items'][0]['source_label']=='组合数学 · math.CO'
    assert feed['items'][0]['primary_category']=='quant-ph'
    assert [p['id'] for p in client.get('/api/recommendations',headers=auth).json()['items']]==[papers[1]]
    assert client.get('/api/browse?range=all',headers=auth).json()['total']==3
    selection = {'categories':[],'topics':{'arxiv:cs.AI':[3]}}
    assert client.put('/api/profile',headers=auth,json={'form':{},'category_selection':selection}).status_code==200
    finish_interest_updates(client)
    assert [p['id'] for p in client.get('/api/feed/today',headers=auth).json()['items']]==[papers[0]]
    assert len(client.get('/api/feed/today').json()['items'])==3


def test_history_keeps_current_and_three_previous_versions_with_monotonic_rollback(client, accounts):
    a,b = accounts
    for version in range(1,9):
        put_profile(a['user']['id'],f'方向 {version}',{},'manual')
    put_profile(b['user']['id'],'另一个用户',{},'manual')
    result = client.get('/api/profile',headers=headers(a)).json()
    assert result['current']['version']==8
    assert [p['version'] for p in result['history']]==[7,6,5]
    assert client.post('/api/profile/rollback',headers=headers(a),json={'version':1}).status_code==404
    finish_interest_updates(client)
    assert client.post('/api/profile/rollback',headers=headers(a),json={'version':5}).json()['version']==9
    finish_interest_updates(client)
    assert current(a['user']['id'])['content']=='方向 5'
    assert [p['version'] for p in rows('SELECT version FROM interest_profile WHERE user_id=? ORDER BY version',(a['user']['id'],))]==[6,7,8,9]
    assert current(b['user']['id'])['content']=='另一个用户'


def test_keys_are_encrypted_preserved_and_never_returned(client, accounts):
    auth = headers(accounts[0])
    config = client.get('/api/admin/models',headers=auth).json()
    config['connections'][1]['api_key']='private-test-credential'
    result = client.put('/api/admin/models',headers=auth,json=config)
    assert result.status_code==200
    raw = one("SELECT value FROM app_settings WHERE name='models'")['value']
    assert 'private-test-credential' not in raw
    assert 'api_key_encrypted' in raw and 'api_key' not in result.json()['connections'][1]
    assert 'api_key_encrypted' not in result.text
    assert runtime.configuration()['connections'][1]['api_key']=='private-test-credential'
    assert 'private-test-credential' not in runtime.safe_error(ValueError('provider echoed private-test-credential'))
    assert settings().model_key_file.exists()
    assert client.put('/api/admin/models',headers=auth,json=result.json()).status_code==200
    assert runtime.configuration()['connections'][1]['api_key']=='private-test-credential'
    settings().model_key_file.write_bytes(Fernet.generate_key())
    failed = client.get('/api/admin/models',headers=auth)
    assert failed.status_code==503 and 'private-test-credential' not in failed.text


def test_existing_plaintext_keys_migrate_on_startup(client):
    from app.db import init_db
    config = runtime.legacy_defaults()
    config['connections'][1]['api_key'] = 'legacy-test-credential'
    execute("INSERT OR REPLACE INTO app_settings VALUES('models',?,?)", (dumps(config),now()))
    init_db()
    assert 'legacy-test-credential' not in one("SELECT value FROM app_settings WHERE name='models'")['value']
    assert runtime.configuration()['connections'][1]['api_key']=='legacy-test-credential'


@pytest.mark.asyncio
async def test_classification_can_finish_multiple_batches_and_resume(client, monkeypatch):
    from app.pipeline import classify as module
    from app.db import connect
    with connect() as db:
        db.executemany('INSERT INTO papers(title,created_at,ingested_date,primary_category) VALUES(?,?,?,?)', [('test',now(),today(),'cs.AI') for _ in range(105)])
    calls = []
    async def ready(): pass
    async def process(paper):
        calls.append(paper['id'])
        execute('UPDATE papers SET classified=1 WHERE id=?',(paper['id'],))
    monkeypatch.setattr(module,'check_service',ready)
    monkeypatch.setattr(module,'classify_paper',process)
    assert await module.classify(limit=3)==3
    assert await module.classify()==102
    assert len(calls)==len(set(calls))==105
    progress = json.loads(one("SELECT progress FROM source_status WHERE name='classify'")['progress'])
    assert progress['completed']==102 and progress['failed']==0 and progress['pending']==0


@pytest.mark.asyncio
async def test_stopping_classification_keeps_completed_papers(client, papers, monkeypatch):
    from app.pipeline import classify as module
    started = asyncio.Event()
    async def ready(): pass
    async def process(paper):
        if paper['id']==papers[0]:
            execute('UPDATE papers SET classified=1 WHERE id=?',(paper['id'],))
        else:
            started.set()
            await asyncio.Event().wait()
    monkeypatch.setattr(module,'check_service',ready)
    monkeypatch.setattr(module,'classify_paper',process)
    task = asyncio.create_task(module.classify())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert one('SELECT classified FROM papers WHERE id=?',(papers[0],))['classified']==1
    assert one('SELECT classified FROM papers WHERE id=?',(papers[1],))['classified']==0
    progress = json.loads(one("SELECT progress FROM source_status WHERE name='classify'")['progress'])
    assert progress['completed']==1 and progress['pending']==2
