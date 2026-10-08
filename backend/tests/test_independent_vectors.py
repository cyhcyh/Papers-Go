import asyncio
import json
import threading
import time
from contextlib import contextmanager

import pytest

from app import vector_store, background_load
from app.config import now, settings
from app.db import connect, execute, one, pack, set_paper_vector
from app.llm import vector_rebuild, runtime
from app.pipeline.expire import purge_batch
from .conftest import headers


def test_vectors_are_external_and_recommendation_feedback_uses_real_vector(client,accounts,papers):
    assert one('SELECT embedding FROM papers WHERE id=?',(papers[1],))['embedding']==vector_store.READY
    assert vector_store.get(papers[1])==pack([0.,1.,0.,0.])
    user=accounts[0]
    from app.interest.profile import put_profile,current
    put_profile(user['user']['id'],'## 核心兴趣\n- [w:1] graph',{},'test',pack([1.,0,0,0]))
    before=current(user['user']['id'])['embedding']
    response=client.post('/api/interactions',headers=headers(user),json={'paper_id':papers[1],'action':'like'})
    assert response.status_code==200
    assert current(user['user']['id'])['embedding']!=before
    assert len(client.get('/api/feed/today',headers=headers(user)).json()['items'])>0


def test_expiry_is_immediate_and_vector_cleanup_is_exact_and_durable(client,accounts,papers):
    keep=vector_store.get(papers[1]);name=vector_store.active()['name']
    execute("UPDATE papers SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",(papers[0],))
    assert purge_batch(now())==1
    assert vector_store.get(papers[0]) is None
    assert one('SELECT * FROM vector_cleanup WHERE space=? AND paper_id=?',(name,papers[0]))
    # Closing all handles simulates recovery; the durable queue survives.
    vector_store.close()
    assert vector_store.cleanup_batch()==1
    with vector_store.reader(name) as db:
        assert not db.execute('SELECT paper_id FROM paper_vectors WHERE paper_id=?',(papers[0],)).fetchone()
        assert not db.execute('SELECT paper_id FROM papers_vec WHERE paper_id=?',(papers[0],)).fetchone()
    assert vector_store.get(papers[1])==keep
    assert one('SELECT COUNT(*) n FROM users')['n']==2


def test_regeneration_does_not_get_deleted_by_an_old_cleanup_record(client,papers):
    execute('UPDATE papers SET embedding=NULL WHERE id=?',(papers[0],))
    assert set_paper_vector(papers[0],[0,1,0,0])
    assert vector_store.cleanup_batch()==0
    assert vector_store.get(papers[0])==pack([0,1,0,0])


def test_business_commit_failure_preserves_old_redo_vector(client,papers,monkeypatch):
    original=vector_store.get(papers[0]);real_connect=connect
    @contextmanager
    def failing_connect(*args,**kwargs):
        with real_connect(*args,**kwargs) as db:
            yield db
            if db.in_transaction:
                raise RuntimeError('business commit rejected')
    monkeypatch.setattr('app.db.connect',failing_connect)
    with pytest.raises(RuntimeError,match='commit rejected'):
        set_paper_vector(papers[0],[0,1,0,0])
    assert vector_store.get(papers[0])==original


def test_background_vector_write_does_not_lock_business_database(client,accounts,papers):
    name=vector_store.active()['name'];entered=threading.Event();release=threading.Event()
    def hold():
        with vector_store.writer(name) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE metadata SET value=value+1 WHERE name='revision'")
            entered.set();assert release.wait(5)
    thread=threading.Thread(target=hold);thread.start()
    try:
        assert entered.wait(3)
        start=time.perf_counter()
        response=client.post('/api/interactions',headers=headers(accounts[0]),json={'paper_id':papers[1],'action':'save'})
        assert response.status_code==200
        assert time.perf_counter()-start<2
        assert client.get('/api/feed/today').status_code==200
    finally:
        release.set();thread.join(5)


def test_initial_migration_reuses_legacy_vectors_and_keeps_business_records(client):
    first=execute('INSERT INTO papers(title,abstract,embedding,created_at,ingested_date) VALUES(?,?,?,?,?)',
                  ('Legacy paper','Original abstract',pack([0,1,0,0]),now(),'2026-10-05'))
    with connect() as db:
        db.execute('INSERT INTO papers_vec VALUES(?,?)',(first,pack([0,1,0,0])))
    before=one('SELECT * FROM papers WHERE id=?',(first,))
    value=vector_store.ensure()
    assert vector_store.get(first)==pack([0,1,0,0])
    assert one('SELECT * FROM papers WHERE id=?',(first,))==before
    assert value['epoch']=='legacy'


def test_background_load_signal_is_bounded_and_ignores_stale_process(client):
    path=settings().data_dir/'.web-load.json'
    path.write_text(json.dumps({'at':time.time(),'active':2,'slow_until':time.time()+10}),encoding='utf-8')
    background_load._delay_cache=(None,0,None)
    assert background_load.delay(.02)==.4
    path.write_text(json.dumps({'at':time.time()-20,'active':100,'slow_until':time.time()+10}),encoding='utf-8')
    background_load._delay_cache=(None,0,None)
    assert background_load.delay(.02)==.02


@pytest.mark.parametrize('redo',[False,True])
def test_external_commit_failure_repairs_new_flags_and_preserves_redo(client,papers,monkeypatch,redo):
    paper_id=papers[0] if redo else execute(
        'INSERT INTO papers(title,abstract,created_at,ingested_date) VALUES(?,?,?,?)',
        ('New vector','Abstract',now(),'2026-10-05'))
    original=vector_store.get(paper_id)
    real_writer=vector_store.writer
    @contextmanager
    def failing_writer(name):
        with real_writer(name) as db:
            yield db
            raise RuntimeError('vector commit rejected')
    monkeypatch.setattr(vector_store,'writer',failing_writer)
    with pytest.raises(RuntimeError,match='vector commit rejected'):
        set_paper_vector(paper_id,[0,1,0,0])
    assert vector_store.get(paper_id)==original
    assert not one('SELECT * FROM vector_publications WHERE paper_id=?',(paper_id,))
    if not redo:assert one('SELECT embedding FROM papers WHERE id=?',(paper_id,))['embedding'] is None


def test_build_reads_do_not_block_live_vector_reads(client,papers):
    live=vector_store.active()['name'];build=vector_store.create(4)
    entered=threading.Event();release=threading.Event()
    def hold():
        with vector_store.reader(build):
            entered.set();assert release.wait(5)
    thread=threading.Thread(target=hold);thread.start()
    try:
        assert entered.wait(3)
        started=time.perf_counter()
        assert vector_store.get(papers[0])==pack([1,0,0,0])
        assert time.perf_counter()-started<2
        assert vector_store._business_reader.in_transaction is False
    finally:
        release.set();thread.join(5)


def test_recovery_does_not_reset_a_newer_publication(client,papers,monkeypatch):
    ident=papers[0];name=vector_store.active()['name'];old='2000-01-01T00:00:00+00:00'
    execute('INSERT INTO vector_publications VALUES(?,?,0,?)',(name,ident,old))
    execute('UPDATE papers SET title=? WHERE id=?',('Changed source',ident))
    real_reader=vector_store.reader
    @contextmanager
    def changed_reader(space):
        with real_reader(space) as db:
            yield db
        execute('UPDATE vector_publications SET created_at=? WHERE space=? AND paper_id=?',(now(),name,ident))
    monkeypatch.setattr(vector_store,'reader',changed_reader)
    assert vector_store.repair_publications(name,[ident])==1
    assert one('SELECT embedding FROM papers WHERE id=?',(ident,))['embedding']==vector_store.READY
    assert one('SELECT created_at FROM vector_publications WHERE paper_id=?',(ident,))['created_at']!=old


def test_trend_materials_use_external_semantic_vectors(client,accounts,papers):
    from app.pipeline import direction_trends
    from app.interest.profile import put_profile,current
    structured={'category_selection':{'categories':['arxiv:cs.AI'],'topics':{}}}
    uid=accounts[0]['user']['id']
    put_profile(uid,'## 核心兴趣\n- [w:1] Unknown focus',structured,'test')
    profile=current(uid)
    # No legacy index remains; evidence must still use the active external file.
    execute('DELETE FROM papers_vec')
    result=direction_trends.research_materials(profile,structured,vectors=[[0,1,0,0]])
    assert [p['id'] for p in result['papers']]==[papers[1]]


def test_undo_after_model_switch_keeps_current_profile_dimension(client,accounts,papers):
    from app.interest.profile import put_profile,current
    user=accounts[0];uid=user['user']['id']
    put_profile(uid,'## 核心兴趣\n- [w:1] graph',{},'test',pack([1,0,0,0]))
    event=client.post('/api/interactions',headers=headers(user),json={'paper_id':papers[1],'action':'like'})
    assert event.status_code==200
    import struct
    name=vector_store.create(8);new_vector=struct.pack('<8f',1,0,0,0,0,0,0,0)
    with connect() as db:
        vector_store.activate(db,name,8,model_epoch=name)
        db.execute('UPDATE interest_profile SET embedding=? WHERE id=?',(new_vector,current(uid)['id']))
    response=client.post('/api/interactions',headers=headers(user),json={'action':'undo','target_id':event.json()['id']})
    assert response.status_code==200 and response.json()['liked'] is False
    assert current(uid)['embedding']==new_vector
