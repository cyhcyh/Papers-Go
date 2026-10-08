import json
import httpx
import pytest
from app.config import now,today,settings
from app.db import execute,rows,one,dumps,set_paper_vector
from app.pipeline.fetch import upsert_paper,ingest_conference_batch
from app.source_deletion import deletion_impact,delete_source
from app.interest.profile import put_profile,current
from .conftest import headers


def paper(title,code=None,categories=(),venue=None,year=None):
    return upsert_paper({'arxiv_id':str(abs(hash(title)))+'.id','title':title,'abstract':'Research description','authors':[], 'primary_category':code,'categories':list(categories),'venue':venue,'venue_year':year,'published':today()})

@pytest.fixture(autouse=True)
def controlled_deletion_consumer(monkeypatch):
    from app import source_deletion_queue as queue
    async def wait():
        import asyncio
        await asyncio.Event().wait()
    monkeypatch.setattr(queue,'serve',wait)


def drain(response):
    from app import source_deletion_queue as queue
    ident=response.json()['id']
    for _ in range(100):
        job=one('SELECT * FROM source_deletion_jobs WHERE id=?',(ident,))
        if job['status']=='done':return queue.public(job)
        queue.step(ident)
    raise AssertionError('Deletion did not complete')


def remove(client,account,key,expected=None):
    info=expected or client.get('/api/admin/source-categories/'+key+'/impact',headers=headers(account)).json()
    response=client.request('DELETE','/api/admin/source-categories/'+key,headers=headers(account),json={'confirm_code':info['code'],'delete_papers':info['delete_papers'],'keep_papers':info['keep_papers']})
    if response.status_code==202:drain(response)
    return response


def test_arxiv_delete_exclusive_cleanup_shared_preservation_and_interest_update(client,accounts):
    admin,user=accounts;uid=user['user']['id'];paper('Exclusive AI','cs.AI');exclusive=one('SELECT MAX(id) id FROM papers')['id']
    paper('Shared AI and Math','cs.AI',('cs.AI','math.CO'));shared=one('SELECT MAX(id) id FROM papers')['id']
    paper('Unrelated Math','math.CO')
    set_paper_vector(exclusive,[1,0,0,0]);set_paper_vector(shared,[1,0,0,0])
    cache=settings().data_dir/'pdf';cache.mkdir();path=cache/f'{exclusive}.pdf';path.write_bytes(b'test PDF cache')
    execute('UPDATE papers SET pdf_path=? WHERE id=?',(str(path),exclusive))
    for ident in (exclusive,shared):
        execute('INSERT INTO user_paper_state(user_id,paper_id,saved) VALUES(?,?,1)',(uid,ident))
        execute("INSERT INTO reading_cards(paper_id,status,card_json,created_at) VALUES(?,'ready','{}',?)",(ident,now()))
        event=execute("INSERT INTO interactions(user_id,paper_id,action,created_at) VALUES(?,?,'save',?)",(uid,ident,now()))
        execute("INSERT INTO interactions(user_id,paper_id,action,target_id,created_at) VALUES(?,?,'undo',?,?)",(uid,ident,event,now()))
    put_profile(uid,'My interests',{'category_selection':{'categories':['arxiv:cs.AI'],'topics':{},'weights':{'arxiv:cs.AI':1}}},'manual')
    impact=deletion_impact('arxiv:cs.AI');assert impact['delete_papers']==1 and impact['keep_papers']==1
    assert remove(client,user,'arxiv:cs.AI',impact).status_code==403
    result=remove(client,admin,'arxiv:cs.AI');assert result.status_code==202
    assert drain(result)['removed_pdf_files']==1 and not path.exists()
    assert one('SELECT id FROM papers WHERE id=?',(exclusive,)) is None
    for table in ('papers_vec','reading_cards','paper_sources','paper_categories','interactions','user_paper_state'):
        assert not rows('SELECT * FROM '+table+' WHERE paper_id=?',(exclusive,))
    retained=one('SELECT * FROM papers WHERE id=?',(shared,));assert retained['primary_category']=='math.CO' and json.loads(retained['categories'])==['math.CO']
    assert one('SELECT saved FROM user_paper_state WHERE paper_id=?',(shared,))['saved']==1
    assert len(rows('SELECT id FROM papers'))==2
    data=json.loads(current(uid)['structured']);assert data['category_selection']=={'categories':[],'topics':{},'weights':{}} and data['source_selection_removed']
    assert client.get('/api/recommendations',headers=headers(user)).json()['items']==[]
    assert client.get('/api/browse?category=arxiv:cs.AI').status_code==400


def test_venue_delete_retains_arxiv_and_other_conference_sources(client,accounts):
    for title,primary,categories in [('Venue only',None,()),('Arxiv overlap','cs.AI',('cs.AI',)),('Two venues',None,())]:paper(title,primary,categories,'AAAI',2026)
    exclusive,arxiv,multi=[p['id'] for p in rows('SELECT id FROM papers ORDER BY id')]
    execute('INSERT INTO paper_sources VALUES(?,?,?,?)',('https://papers.cool/venue/other@OpenReview',multi,'ICML',2026))
    result=remove(client,accounts[0],'venue:AAAI');assert result.status_code==202
    assert drain(result)['deleted_papers']==1 and drain(result)['kept_papers']==2
    assert one('SELECT venue FROM papers WHERE id=?',(arxiv,))['venue'] is None
    assert one('SELECT venue FROM papers WHERE id=?',(multi,))['venue']=='ICML'
    assert client.get('/api/browse?category=arxiv:cs.AI&range=all').json()['total']==1
    assert client.get('/api/browse?category=venue:ICML&range=all').json()['total']==1
    assert not one('SELECT id FROM papers WHERE id=?',(exclusive,))
    assert not rows('SELECT * FROM paper_sources WHERE venue=?',('AAAI',))


def test_stale_delete_preview_must_be_confirmed_again_and_paper_ids_never_reused(client,accounts):
    paper('First','cs.AI');impact=deletion_impact('arxiv:cs.AI');paper('Second','cs.AI')
    maximum=one('SELECT MAX(id) id FROM papers')['id']
    assert remove(client,accounts[0],'arxiv:cs.AI',impact).status_code==409 and len(rows('SELECT id FROM papers'))==2
    assert remove(client,accounts[0],'arxiv:cs.AI').status_code==202
    assert set_paper_vector(maximum,[1,0,0,0]) is False and not rows('SELECT * FROM papers_vec')
    paper('New math','math.CO');assert one('SELECT id FROM papers')['id']>maximum


def test_deleted_venue_cannot_be_reinserted_by_an_inflight_batch(client,accounts):
    assert remove(client,accounts[0],'venue:AAAI').status_code==202
    from .test_pipeline_controls_and_conferences import atom
    from app.pipeline.fetch import parse_conference_feed
    _,papers=parse_conference_feed(atom(),'AAAI')
    assert ingest_conference_batch(papers,'venue:AAAI') is None
    assert not rows('SELECT id FROM papers')


@pytest.mark.asyncio
async def test_deleted_arxiv_source_is_skipped_when_response_arrives(client,accounts,monkeypatch):
    import app.pipeline.fetch as fetch
    from app.source_catalog import invalidate
    from .test_arxiv_sync import atom
    execute("UPDATE source_categories SET fetch_enabled=(code='cs.AI') WHERE kind='arxiv'");invalidate()
    original=httpx.AsyncClient
    def handler(request):
        delete_source('arxiv:cs.AI',accounts[0]['user']['id'])
        return httpx.Response(200,text=atom([7]))
    monkeypatch.setattr(fetch.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    assert await fetch.fetch_arxiv(limit=1)==0 and not rows('SELECT id FROM papers')


def test_batch_delete_counts_union_and_preserves_only_unselected_sources(client,accounts):
    admin,_=accounts
    paper('AI only','cs.AI')
    paper('Two removed arxiv classes','cs.AI',('cs.AI','cs.CL'))
    paper('Three arxiv classes','cs.AI',('cs.AI','cs.CL','math.CO'))
    paper('Two removed venues',None,(),'AAAI',2026)
    two_venues=one('SELECT MAX(id) id FROM papers')['id']
    execute('INSERT INTO paper_sources VALUES(?,?,?,?)',('https://papers.cool/venue/test-batch@ICML',two_venues,'ICML',2026))
    paper('Other venue remains',None,(),'AAAI',2026)
    retained=one('SELECT MAX(id) id FROM papers')['id']
    execute('INSERT INTO paper_sources VALUES(?,?,?,?)',('https://papers.cool/venue/test-batch@NeurIPS',retained,'NeurIPS',2026))
    paper('Removed arxiv and venue','cs.AI',('cs.AI',),'AAAI',2026)
    keys=['arxiv:cs.AI','arxiv:cs.CL','venue:AAAI','venue:ICML']
    info=client.post('/api/admin/source-categories/batch/impact',headers=headers(admin),json={'keys':keys}).json()
    assert info['delete_papers']==4 and info['keep_papers']==2 and info['confirmation']=='删除 4 个分类'
    result=client.request('DELETE','/api/admin/source-categories/batch',headers=headers(admin),json={'keys':keys,'confirm_text':info['confirmation'],'delete_papers':4,'keep_papers':2})
    assert result.status_code==202
    drain(result)
    assert len(rows('SELECT id FROM papers'))==2
    assert one('SELECT venue FROM papers WHERE id=?',(retained,))['venue']=='NeurIPS'
    math=one("SELECT * FROM papers WHERE title='Three arxiv classes'")
    assert math['primary_category']=='math.CO' and json.loads(math['categories'])==['math.CO']
    assert not rows("SELECT key FROM source_categories WHERE key IN ('arxiv:cs.AI','arxiv:cs.CL','venue:AAAI','venue:ICML')")
    from app.db import connect
    with connect() as db:assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_batch_settings_permissions_atomic_updates_and_stale_delete(client,accounts):
    admin,user=accounts;keys=['arxiv:cs.AI','arxiv:cs.CL']
    body={'keys':keys,'fetch_enabled':False,'guest_default':False}
    assert client.patch('/api/admin/source-categories/batch',json=body,headers=headers(user)).status_code==403
    assert client.patch('/api/admin/source-categories/batch',json={**body,'keys':[*keys,'missing']},headers=headers(admin)).status_code==404
    assert one("SELECT fetch_enabled FROM source_categories WHERE key='arxiv:cs.AI'")['fetch_enabled']==1
    assert client.patch('/api/admin/source-categories/batch',json=body,headers=headers(admin)).status_code==200
    assert all(not s['fetch_enabled'] and not s['guest_default'] for s in rows("SELECT * FROM source_categories WHERE key IN ('arxiv:cs.AI','arxiv:cs.CL')"))
    paper('First batch','cs.AI');info=client.post('/api/admin/source-categories/batch/impact',headers=headers(admin),json={'keys':keys}).json()
    paper('Second batch','cs.CL')
    result=client.request('DELETE','/api/admin/source-categories/batch',headers=headers(admin),json={'keys':keys,'confirm_text':info['confirmation'],'delete_papers':info['delete_papers'],'keep_papers':info['keep_papers']})
    assert result.status_code==409 and len(rows('SELECT id FROM papers'))==2
    assert len(rows("SELECT key FROM source_categories WHERE key IN ('arxiv:cs.AI','arxiv:cs.CL')"))==2


def test_submission_is_async_idempotent_and_blocks_inflight_source_writes(client,accounts):
    from app.source_catalog import sources_to_fetch
    admin,user=accounts
    paper('Pending venue',None,(),'AAAI',2026)
    info=deletion_impact('venue:AAAI')
    body={'confirm_code':info['code'],'delete_papers':1,'keep_papers':0}
    response=client.request('DELETE','/api/admin/source-categories/venue:AAAI',headers=headers(admin),json=body)
    assert response.status_code==202 and response.json()['status']=='queued'
    assert len(rows('SELECT id FROM papers'))==1  # Request only queues the task.
    assert client.get('/api/health').json()['papers']==1
    duplicate=client.request('DELETE','/api/admin/source-categories/venue:AAAI',headers=headers(admin),json=body)
    assert duplicate.json()['id']==response.json()['id']
    assert all(s['key']!='venue:AAAI' for s in sources_to_fetch('venue'))
    assert client.get('/api/admin/source-categories/deletions',headers=headers(user)).status_code==403
    assert client.post(f"/api/admin/source-categories/deletions/{response.json()['id']}/retry",headers=headers(user)).status_code==403
    assert client.patch('/api/admin/source-categories/batch',headers=headers(admin),json={'keys':['venue:AAAI'],'fetch_enabled':True}).status_code==409
    assert ingest_conference_batch([], 'venue:AAAI') is None
    drain(response)
    assert not rows('SELECT id FROM papers')


def test_deletion_resumes_after_restart_and_file_failure_without_losing_progress(client,accounts,monkeypatch):
    from app import source_deletion_queue as queue
    for number in range(205):paper('Resume '+str(number),'cs.AI')
    info=deletion_impact('arxiv:cs.AI')
    response=client.request('DELETE','/api/admin/source-categories/arxiv:cs.AI',headers=headers(accounts[0]),json={'confirm_code':'cs.AI','delete_papers':info['delete_papers'],'keep_papers':0})
    ident=response.json()['id']
    queue.step(ident)  # Prepare the durable work list.
    queue.step(ident)  # Commit the first 100 paper deletions, before cache removal.
    assert len(rows('SELECT id FROM papers'))==105
    cache=settings().data_dir/'pdf';cache.mkdir();(cache/'1.pdf').write_bytes(b'cached PDF')
    def blocked(*args):raise PermissionError('A temporary cache permission failure')
    original=queue.remove_pdf
    monkeypatch.setattr(queue,'remove_pdf',blocked)
    with pytest.raises(PermissionError) as error:queue.step(ident)
    queue.fail(ident,error.value)
    assert one('SELECT status FROM source_deletion_jobs WHERE id=?',(ident,))['status']=='failed'
    queue.recover()  # A failed task waits for an explicit administrator retry.
    assert one('SELECT status FROM source_deletion_jobs WHERE id=?',(ident,))['status']=='failed'
    monkeypatch.setattr(queue,'remove_pdf',original)
    assert client.post(f'/api/admin/source-categories/deletions/{ident}/retry',headers=headers(accounts[0])).status_code==200
    queue.step(ident)
    assert one('SELECT processed FROM source_deletion_jobs WHERE id=?',(ident,))['processed']==100
    queue.step(ident)
    queue.recover()  # Resume an interrupted running task at its saved phase.
    result=drain(response)
    assert result['processed']==result['total']==result['deleted_papers']==205
    assert not rows('SELECT id FROM papers') and not rows('SELECT * FROM source_deletion_items')
    from app.db import connect
    with connect() as db:assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_background_delete_rechecks_new_remaining_sources_and_new_papers(client,accounts):
    from app import source_deletion_queue as queue
    paper('Newly shared','cs.AI',('cs.AI','cs.DM'))
    info=deletion_impact('arxiv:cs.AI')
    response=client.request('DELETE','/api/admin/source-categories/arxiv:cs.AI',headers=headers(accounts[0]),json={'confirm_code':'cs.AI','delete_papers':info['delete_papers'],'keep_papers':0})
    queue.step(response.json()['id'])
    assert client.post('/api/admin/source-categories',headers=headers(accounts[0]),json={'kind':'arxiv','code':'cs.DM','label':'Discrete Mathematics'}).status_code==200
    paper('Arrived after preparing','cs.AI',('cs.AI','math.CO'))
    result=drain(response)
    assert result['kept_papers']==2 and result['deleted_papers']==0
    assert len(rows('SELECT id FROM papers'))==2
    assert all(p['primary_category']!='cs.AI' and 'cs.AI' not in json.loads(p['categories']) for p in rows('SELECT primary_category,categories FROM papers'))


def test_retained_paper_updated_by_inflight_fetch_is_cleaned_again_and_counted_once(client,accounts):
    from app import source_deletion_queue as queue
    paper('Updated shared paper','cs.AI',('cs.AI','math.CO'))
    ident=one('SELECT id FROM papers')['id']
    response=client.request('DELETE','/api/admin/source-categories/arxiv:cs.AI',headers=headers(accounts[0]),json={'confirm_code':'cs.AI','delete_papers':0,'keep_papers':1})
    queue.step(response.json()['id']);queue.step(response.json()['id'])
    assert one('SELECT primary_category FROM papers')['primary_category']=='math.CO'
    execute('UPDATE papers SET primary_category=?,categories=? WHERE id=?',('cs.AI',dumps(['cs.AI','math.CO']),ident))
    result=drain(response)
    assert result['kept_papers']==result['total']==result['processed']==1
    assert one('SELECT primary_category FROM papers')['primary_category']=='math.CO'
    assert client.get('/api/admin/source-categories/deletions',headers=headers(accounts[0])).json()[0]['status']=='done'


@pytest.mark.parametrize('keys',[['arxiv:cs.AI'],['arxiv:cs.AI','venue:ICML']])
def test_source_removal_preserves_topic_status_and_reuses_unassigned_topics(client,accounts,keys):
    from app.db import connect
    from app.standard_topics import catalog,queue_or_assign
    admin,user=accounts;auth=headers(admin)
    areas=list(catalog().values())[:3]
    topics=[]
    for area,status in zip(areas,('active','proposed','disabled')):
        ident=execute('INSERT INTO topics(name_zh,name_en,status,category_keys,standard_key,discipline,description,created_at) VALUES(?,?,?,?,?,?,?,?)',
                      (area['name_zh'],area['label'],status,dumps(keys),area['key'],area['discipline'],area['description'],now()))
        topics.append(one('SELECT * FROM topics WHERE id=?',(ident,)))
    active_id=topics[0]['id']
    put_profile(user['user']['id'],'My interests',{'topic_ids':[active_id],'category_selection':{'categories':keys,'topics':{},'weights':{}}},'manual')
    paper('Exclusive paper with retained topic','cs.AI')
    existing_paper=one('SELECT * FROM papers')
    with connect() as db:assert queue_or_assign(db,existing_paper,areas[0],.9)==(active_id,'ready')
    if len(keys)==1:
        assert remove(client,admin,keys[0]).status_code==202
    else:
        impact=client.post('/api/admin/source-categories/batch/impact',headers=auth,json={'keys':keys}).json()
        response=client.request('DELETE','/api/admin/source-categories/batch',headers=auth,json={'keys':keys,'confirm_text':impact['confirmation'],'delete_papers':impact['delete_papers'],'keep_papers':impact['keep_papers']})
        assert response.status_code==202;drain(response)
    for original in topics:
        stored=one('SELECT * FROM topics WHERE id=?',(original['id'],))
        assert stored=={**original,'category_keys':'[]'}
    assert not rows('SELECT * FROM papers')
    assert json.loads(current(user['user']['id'])['structured'])['topic_ids']==[active_id]
    assert client.post('/api/admin/source-categories',headers=auth,json={'kind':'arxiv','code':'cs.AI','label':'人工智能'}).status_code==200
    before=len(rows('SELECT id FROM topics'))
    paper('New paper reuses retained topic','cs.AI')
    incoming=one('SELECT * FROM papers')
    with connect() as db:assert queue_or_assign(db,incoming,areas[0],.9)==(active_id,'ready')
    assert len(rows('SELECT id FROM topics'))==before
    assert json.loads(one('SELECT category_keys FROM topics WHERE id=?',(active_id,))['category_keys'])==['arxiv:cs.AI']
    with connect() as db:
        with pytest.raises(ValueError,match='已停用'):queue_or_assign(db,incoming,areas[2],.9)
