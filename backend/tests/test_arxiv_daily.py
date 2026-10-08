import asyncio
import json
import threading
from datetime import date,timedelta
import httpx
import pytest
from app import prompts
from app.config import now,today,settings
from app.db import connect,execute,one,rows,dumps
from app.interest.profile import put_profile
from app.pipeline import arxiv_daily as daily,direction_trends as analysis
from app.pipeline.trends import trend_data,trend_report
from .conftest import headers

FULL=('当期材料围绕结构条件与构造方法两条研究主线展开，研究对象和适用条件不同，不能直接推广为一般猜想的解决。\n\n'
      '1. **结构条件与边界**：研究利用结构分解分析着色约束，讨论局部条件与整体性质之间的关系。这些结果细化了特定图类中结构假设与可用颜色之间的对应关系，证据支持的是所研究条件下的结论，仍需要检验结构假设改变时方法是否成立。\n'
      '2. **构造方法与适用范围**：另一条主线通过极值约束构造组合对象，比较所得边界与相关结构条件。具体构造使结论的适用范围更加明确，但不同图类之间的推广还需要进一步的理论依据。\n\n'
      '值得继续关注的是结构刻画与构造方法能否在相同问题上形成互补证据，以及结论对参数条件的依赖。')
SHORT='今日研究聚焦**图着色**与**组合对象的构造**，相关研究细化了方法的适用条件。\n\n值得关注的是对图着色约束的具体刻画。'

def interests(uid,keys=('arxiv:cs.AI',)):
    return put_profile(uid,'',{'category_selection':{'categories':list(keys),'topics':{}}},'manual')

def add_batch(ids,day=None):
    day=day or today()
    bid=execute('INSERT INTO arxiv_batches(announcement_date,scope,expected_count,complete,checked_at) VALUES(?,?,?,1,?)',(day,daily.scope(),len(ids),now()))
    with connect() as db:
        db.executemany('INSERT INTO arxiv_batch_papers VALUES(?,?,?)',[(bid,str(i),i) for i in ids])
    return bid

@pytest.fixture
def batch(client,papers):return add_batch(papers)

def daily_topics(group,ids=None):
    ids=group['paper_ids'][:8] if ids is None else ids
    return [{'name':t['name'],'progress':'substantial','paper_ids':[i for i in t['paper_ids'] if i in ids]}
            for t in group.get('focus_topics',[]) if set(t['paper_ids']).intersection(ids)]


def mocked(monkeypatch):
    calls=[]
    def short(group):
        return '今日相关研究围绕**'+'**、**'.join(t['name'] for t in group.get('focus_topics',[]))+'**展开。\n\n相关成果细化了方法及适用条件。'
    async def complete(feature,messages,**kwargs):
        value=json.loads(messages[-1]['content']);calls.append(value)
        if 'groups' not in value:return {'items':[{'direction':g['direction'],'summary':'' if g.get('kind')=='daily_overview' else FULL,'short_summary':short(g) if g.get('kind')=='daily_overview' else ''} for g in value['items']]}
        return {'items':[{'direction':g['direction'],'summary':'' if g.get('kind')=='daily_overview' else FULL,'short_summary':short(g) if g.get('kind')=='daily_overview' else '', 'paper_ids':g['paper_ids'][:8],'focus_directions':[t['name'] for t in g.get('focus_topics',[])] if g.get('kind')=='daily_overview' else [],'daily_topics':daily_topics(g) if g.get('kind')=='daily_overview' else [],'insufficient':False} for g in value['groups']]}
    monkeypatch.setattr(analysis.models,'complete',complete)
    return calls

def rss(items,day='Wed, 07 Oct 2026 00:00:00 -0400'):
    return '<rss xmlns:a="http://arxiv.org/schemas/atom"><channel><pubDate>'+day+'</pubDate>'+''.join(f'<item><guid>oai:arXiv.org:{ident}</guid><pubDate>{day}</pubDate><a:announce_type>{kind}</a:announce_type></item>' for ident,kind in items)+'</channel></rss>'

def test_rss_official_date_deduplicates_and_excludes_revisions():
    day,ids=daily.parse_rss(rss([('2610.00001v1','new'),('2610.00001v1','new'),('2609.00001v2','replace'),('2501.00001v1','cross')]))
    assert day=='2026-10-07' and ids=={'2610.00001'}
    with pytest.raises(ValueError):daily.parse_rss(rss([('2610.00001v1','unknown')]))
    with pytest.raises(ValueError):daily.parse_rss('<html>temporarily unavailable</html>')

@pytest.mark.asyncio
async def test_sync_hydrates_missing_new_papers_and_retains_weekend_batch(client,monkeypatch):
    from app.source_catalog import invalidate
    execute("UPDATE source_categories SET fetch_enabled=(code='math.CO')");invalidate()
    calls=[];empty=False
    async def get(client,url,**kwargs):
        calls.append(url)
        if 'rss.arxiv' in url:text=rss([] if empty else [('2610.00001v1','new'),('2610.00001v1','new'),('2601.00001v2','replace')])
        else:
            text='<feed xmlns="http://www.w3.org/2005/Atom" xmlns:x="http://arxiv.org/schemas/atom"><entry><id>https://arxiv.org/abs/2610.00001v1</id><title>A tree theorem</title><summary>A mathematical result.</summary><published>2026-10-05T00:00:00Z</published><x:primary_category term="math.CO"/><category term="math.CO"/></entry></feed>'
        return httpx.Response(200,text=text,request=httpx.Request('GET',url))
    monkeypatch.setattr(daily.arxiv_client,'get',get)
    first=await daily.sync_latest(force=True)
    assert first['expected_count']==1 and first['announcement_date']=='2026-10-07' and len(calls)==2
    assert one('SELECT COUNT(*) n FROM papers')['n']==1
    await daily.sync_latest(force=True);assert len(calls)==3
    empty=True;await daily.sync_latest(force=True);assert daily.latest()['id']==first['id']

@pytest.mark.asyncio
async def test_incomplete_multi_category_sync_keeps_previous_batch(client,papers,batch,monkeypatch):
    async def get(client,url,**kwargs):
        text=rss([('2610.00001v1','new')],'Wed, 07 Oct 2026 00:00:00 -0400' if 'cs.AI' in url else 'Tue, 06 Oct 2026 00:00:00 -0400')
        return httpx.Response(200,text=text,request=httpx.Request('GET',url))
    monkeypatch.setattr(daily.arxiv_client,'get',get)
    with pytest.raises(ValueError,match='同一天'):await daily.sync_latest(force=True)
    assert daily.latest()['id']==batch and len(rows('SELECT * FROM arxiv_batches'))==1

@pytest.mark.asyncio
async def test_analysis_reads_announcement_membership_not_submission_or_venue(client,accounts,papers,batch,monkeypatch):
    uid=accounts[0]['user']['id'];interests(uid,('arxiv:cs.AI','venue:AAAI'));calls=mocked(monkeypatch)
    execute("UPDATE papers SET published='2026-09-01' WHERE id=?",(papers[0],))
    execute("INSERT INTO papers(title,abstract,venue,published,created_at,ingested_date) VALUES('Conference paper','Abstract','AAAI',?,?,?)",(today(),now(),today()))
    assert await analysis.ensure_direction_trend(uid)
    value=analysis.trend_summary(uid)
    assert value['window']=='day' and value['period']['from']==value['period']['through']==today()
    assert value['items'][0]['kind']=='daily_overview' and value['items'][0]['status']=='ready'
    assert value['items'][1]['summary']==FULL and not value['items'][1]['short_summary']
    assert value['items'][0]['matched_count']==3 and len(calls)==1
    assert set(calls[0]['groups'][0]['paper_ids'])==set(papers)
    assert value['papers'][0]['published']<=today()

@pytest.mark.asyncio
async def test_http_only_queues_and_shared_cache_avoids_duplicate_calls(client,accounts,papers,batch,monkeypatch):
    for account in accounts:interests(account['user']['id'])
    calls=mocked(monkeypatch);uid=accounts[0]['user']['id']
    assert client.get('/api/trends/summary',headers=headers(accounts[0])).json()['status']=='pending'
    assert not calls and one('SELECT COUNT(*) n FROM arxiv_trend_requests')['n']==1
    assert await analysis.ensure_direction_trend(uid)
    assert not await analysis.ensure_direction_trend(accounts[1]['user']['id'])
    assert analysis.trend_summary(accounts[1]['user']['id'])['items']==analysis.trend_summary(uid)['items'] and len(calls)==1
    assert client.get('/api/trends/summary').json()['status']=='login_required'

@pytest.mark.asyncio
async def test_interest_change_and_unrelated_paper_updates(client,accounts,papers,batch,monkeypatch):
    uid=accounts[0]['user']['id'];interests(uid);mocked(monkeypatch);assert await analysis.ensure_direction_trend(uid)
    before=analysis.latest_material()
    execute('UPDATE papers SET quality_score=90 WHERE id=?',(papers[0],));assert analysis.latest_material()==before
    other=execute("INSERT INTO papers(title,abstract,created_at,ingested_date) VALUES('Old paper','Old abstract',?,?)",(now(),today()))
    execute("UPDATE papers SET abstract='Changed historical paper' WHERE id=?",(other,));assert not analysis.needs_update(uid)
    execute("UPDATE papers SET abstract='Changed latest evidence' WHERE id=?",(papers[0],));assert analysis.needs_update(uid)
    interests(uid,('arxiv:math.CO',));assert not analysis.trend_summary(uid)['items']

def test_hotspots_full_counts_and_coverage_are_scoped(client,accounts,papers,batch):
    uid=accounts[0]['user']['id'];interests(uid)
    execute("UPDATE papers SET primary_category='math.CO' WHERE id=?",(papers[1],))
    execute("UPDATE papers SET classified=1 WHERE id=?",(papers[0],))
    value=trend_data(uid)
    assert value['coverage']['matched_count']==2 and value['coverage']['classified_count']==1
    assert value['coverage']['classification_coverage']==50
    assert value['topics']==[]  # A single paper in a topic is not a daily hotspot.

def test_saved_author_updates_keep_seven_days_outside_daily_batch(client,accounts,papers,batch):
    uid=accounts[0]['user']['id'];interests(uid)
    execute('INSERT INTO user_paper_state(user_id,paper_id,saved,updated_at) VALUES(?,?,1,?)',(uid,papers[0],now()))
    recent=(date.fromisoformat(today())-timedelta(days=6)).isoformat()
    older=(date.fromisoformat(today())-timedelta(days=7)).isoformat()
    identifiers=[]
    for stamp in (recent,older):
        identifiers.append(execute("INSERT INTO papers(title,abstract,authors,primary_category,published,created_at,ingested_date) VALUES('Author new work','Abstract',?,'math.CO',?,?,?)",(dumps(['Research Author']),stamp,now(),today())))
    value=trend_data(uid)
    assert identifiers[0] in {p['id'] for p in value['movements']}
    assert identifiers[1] not in {p['id'] for p in value['movements']}
    assert value['coverage']['matched_count']==3


def test_daily_hotspots_require_three_and_cap_at_six(client,accounts,papers,batch):
    uid=accounts[0]['user']['id'];interests(uid)
    with connect() as db:
        for topic in range(3,10):
            for i in range(3):
                pid=db.execute("INSERT INTO papers(title,abstract,primary_category,created_at,ingested_date) VALUES('Current research','Abstract','cs.AI',?,?)",(now(),today())).lastrowid
                db.execute('INSERT INTO paper_topics VALUES(?,?,.9)',(pid,topic))
                db.execute('INSERT INTO arxiv_batch_papers VALUES(?,?,?)',(batch,'hot'+str(pid),pid))
    data=trend_data(uid)
    assert len(data['topics'])==6 and all(t['count']>=3 for t in data['topics'])
    assert all(t['id']!=15 for t in data['topics'])
    assert data['topics'][0]['count']==4


@pytest.mark.asyncio
async def test_overview_follows_interest_and_reuses_the_same_model_call(client,accounts,papers,batch,monkeypatch):
    uid=accounts[0]['user']['id']
    put_profile(uid,'## 核心兴趣（长期）\n- [w:0.9] memory',{'category_selection':{'categories':['arxiv:cs.AI'],'topics':{}}},'manual')
    execute("UPDATE papers SET abstract='Agent memory evaluation across tasks.' WHERE id=?",(papers[2],))
    calls=mocked(monkeypatch)
    assert await analysis.ensure_direction_trend(uid) and len(calls)==1
    daily=next(g for g in calls[0]['groups'] if g.get('kind')=='daily_overview')
    assert set(daily['paper_ids'])=={papers[0],papers[2]}
    assert [t['name'] for t in daily['focus_topics']]==['memory']
    item=next(i for i in analysis.trend_summary(uid)['items'] if i['kind']=='daily_overview')
    assert '**memory**' in item['short_summary'] and item['summary']==''


def test_daily_overview_uses_readable_paragraphs_without_an_arbitrary_minimum():
    assert analysis.valid_text('',SHORT,daily=True)
    assert analysis.valid_text('','今日研究聚焦**图着色**，出现一项新进展。',daily=True)
    assert not analysis.valid_text('','- **结构图论**：一项进展。',daily=True)
    assert not analysis.valid_text('','\n\n'.join(['一个研究焦点。']*4),daily=True)
    assert not analysis.valid_text('','今日确定了 $n^2+2$ 的界。',daily=True)
    assert not analysis.valid_text('Detailed text',SHORT,daily=True)

def test_input_budget_and_bounded_interest_samples(client,accounts,papers,batch):
    uid=accounts[0]['user']['id'];interests(uid)
    with connect() as db:
        for i in range(30):
            pid=db.execute("INSERT INTO papers(title,abstract,primary_category,created_at,ingested_date) VALUES(?,?,'cs.AI',?,?)",('Agent research '+str(i),'A study of agent memory and evaluation. '*80,now(),today())).lastrowid
            db.execute('INSERT INTO arxiv_batch_papers VALUES(?,?,?)',(batch,'new'+str(i),pid))
            db.execute('INSERT INTO paper_topics VALUES(?,?,.9)',(pid,3+i%2))
    profile,structured,_=analysis.profile_context(uid);materials=analysis.research_materials(profile,structured)
    assert materials['groups'][0]['matched_count']==33 and 1<=len(materials['groups'][0]['papers'])<=12
    assert all(t['paper_ids'] for t in materials['groups'][0]['focus_topics'])
    payload=analysis.model_payload(materials)
    assert analysis.input_tokens(prompts.get('trend_report'))+analysis.input_tokens(dumps(payload))<=settings().trend_input_tokens


def interest_topic(batch,name,count,category='cs.AI'):
    tid=execute('INSERT INTO topics(name_zh,name_en,category_keys,created_at) VALUES(?,?,?,?)',(name,name,dumps(['arxiv:'+category]),now()))
    ids=[]
    with connect() as db:
        for i in range(count):
            pid=db.execute('INSERT INTO papers(title,abstract,primary_category,created_at,ingested_date,classified) VALUES(?,?,?,?,?,1)',
                           (name+' '+str(i),'We characterize the central research problem.',category,now(),today())).lastrowid
            ids.append(pid)
            db.execute('INSERT INTO paper_topics VALUES(?,?,.9)',(pid,tid))
            db.execute('INSERT INTO arxiv_batch_papers VALUES(?,?,?)',(batch,'interest'+str(pid),pid))
    return tid,ids


def test_daily_interest_priority_beats_popularity_and_limits_to_three(client,accounts,papers,batch):
    uid=accounts[0]['user']['id']
    chosen=[interest_topic(batch,name,n) for name,n in [('首要方向',2),('第二方向',2),('第三方向',3),('第四方向',10),('其他热门方向',14)]]
    text='## 核心兴趣（长期）\n- [w:0.8] 第二方向\n- [w:0.6] 第三方向\n- [w:1.0] 首要方向\n- [w:0.4] 第四方向'
    put_profile(uid,text,{'category_selection':{'categories':['arxiv:cs.AI'],'topics':{}}},'manual')
    profile,structured,_=analysis.profile_context(uid)
    material=analysis.research_materials(profile,structured)
    daily=material['groups'][0]
    assert [t['name'] for t in daily['focus_topics']]==['首要方向','第二方向','第三方向']
    expected={pid for _,ids in chosen[:3] for pid in ids}
    assert {p['id'] for p in daily['papers']}<=expected
    assert all(set(t['paper_ids'])<=expected and len(t['paper_ids'])>=2 for t in daily['focus_topics'])


def test_selected_topics_follow_category_attention_without_written_interests(client,accounts,papers,batch):
    uid=accounts[0]['user']['id'];ai,ai_ids=interest_topic(batch,'人工智能关注主题',6);math,math_ids=interest_topic(batch,'数学关注主题',2,'math.CO')
    put_profile(uid,'',{'category_selection':{'categories':[],'topics':{'arxiv:cs.AI':[ai],'arxiv:math.CO':[math]},'weights':{'arxiv:cs.AI':.2,'arxiv:math.CO':1}}},'manual')
    profile,structured,_=analysis.profile_context(uid)
    daily=analysis.research_materials(profile,structured)['groups'][0]
    assert [t['name'] for t in daily['focus_topics']]==['数学关注主题','人工智能关注主题']
    assert set(daily['focus_topics'][0]['paper_ids'])==set(math_ids)


def test_category_only_focus_uses_attention_before_topic_counts(client,accounts,papers,batch):
    uid=accounts[0]['user']['id'];interest_topic(batch,'很多人工智能论文',9);interest_topic(batch,'数学重点',2,'math.CO')
    put_profile(uid,'',{'category_selection':{'categories':['arxiv:cs.AI','arxiv:math.CO'],'topics':{},'weights':{'arxiv:cs.AI':.2,'arxiv:math.CO':1}}},'manual')
    profile,structured,_=analysis.profile_context(uid)
    daily=analysis.research_materials(profile,structured)['groups'][0]
    assert [t['name'] for t in daily['focus_topics']][:2]==['数学重点','很多人工智能论文']
    assert len(daily['focus_topics'])<=3


@pytest.mark.asyncio
async def test_single_paper_interest_is_reported_without_unrelated_hot_topics(client,accounts,papers,batch,monkeypatch):
    uid=accounts[0]['user']['id'];interest_topic(batch,'用户关注方向',1);interest_topic(batch,'其他热门方向',8)
    put_profile(uid,'## 核心兴趣（长期）\n- [w:1] 用户关注方向',{'category_selection':{'categories':['arxiv:cs.AI'],'topics':{}}},'manual')
    profile,structured,_=analysis.profile_context(uid)
    material=analysis.research_materials(profile,structured)
    assert [t['name'] for t in material['groups'][0]['focus_topics']]==['用户关注方向']
    assert len(material['groups'][0]['papers'])==1
    calls=mocked(monkeypatch)
    assert await analysis.ensure_direction_trend(uid) and len(calls)==1
    assert analysis.trend_summary(uid)['items'][0]['status']=='ready'


def test_attention_change_invalidates_daily_cache_even_with_same_group_order(client,accounts,papers,batch):
    uid=accounts[0]['user']['id']
    put_profile(uid,'',{'category_selection':{'categories':['arxiv:cs.AI'],'topics':{},'weights':{'arxiv:cs.AI':.7}}},'manual')
    original=analysis.audience(uid)
    put_profile(uid,'',{'category_selection':{'categories':['arxiv:cs.AI'],'topics':{},'weights':{'arxiv:cs.AI':1}}},'manual')
    assert analysis.audience(uid)!=original


@pytest.mark.asyncio
async def test_daily_focus_metadata_rejects_topics_outside_the_users_interest(client,accounts,papers,batch,monkeypatch):
    uid=accounts[0]['user']['id'];interests(uid);mocked(monkeypatch)
    assert await analysis.ensure_direction_trend(uid)
    previous=analysis.cached(uid)['items_json']
    async def invalid(feature,messages,**kwargs):
        data=json.loads(messages[-1]['content'])
        return {'items':[{'direction':g['direction'],'summary':'' if g.get('kind')=='daily_overview' else FULL,
                         'short_summary':SHORT if g.get('kind')=='daily_overview' else '',
                         'paper_ids':g['paper_ids'][:8],'focus_directions':['不在兴趣范围的主题'] if g.get('kind')=='daily_overview' else [],
                         'insufficient':False} for g in data['groups']]}
    monkeypatch.setattr(analysis.models,'complete',invalid)
    assert not await analysis._generate(uid,force=True)
    assert '兴趣条目' in analysis.cached(uid)['error'] and analysis.cached(uid)['items_json']==previous

@pytest.mark.asyncio
async def test_prompt_change_inflight_preserves_old_cache_and_blocks_old_claim(client,accounts,papers,batch,monkeypatch):
    from app.api.prompts import save_instruction,PromptEdit
    uid=accounts[0]['user']['id'];interests(uid);mocked(monkeypatch);assert await analysis.ensure_direction_trend(uid)
    old=analysis.trend_summary(uid)['items'];started=asyncio.Event();release=asyncio.Event()
    async def complete(*args,**kwargs):
        started.set();await release.wait();raise ValueError('old failure')
    monkeypatch.setattr(analysis.models,'complete',complete)
    task=asyncio.create_task(analysis._generate(uid,force=True));await asyncio.wait_for(started.wait(),2)
    save_instruction('trend_report',PromptEdit(text=prompts.get('trend_report')+'\n准确说明适用范围。'))
    mocked(monkeypatch);assert await analysis._generate(uid,force=True)
    release.set();assert not await task
    assert analysis.cached(uid)['error'] is None and analysis.trend_summary(uid)['items']==old

@pytest.mark.asyncio
async def test_profile_change_inflight_does_not_publish(client,accounts,papers,batch,monkeypatch):
    uid=accounts[0]['user']['id'];interests(uid);started=asyncio.Event();release=asyncio.Event()
    async def complete(*args,**kwargs):
        started.set();await release.wait()
        return {'items':[{'direction':'人工智能','summary':FULL,'short_summary':SHORT,'paper_ids':papers[:2],'insufficient':False}]}
    monkeypatch.setattr(analysis.models,'complete',complete)
    task=asyncio.create_task(analysis.ensure_direction_trend(uid));await asyncio.wait_for(started.wait(),2)
    interests(uid,('arxiv:math.CO',));release.set();assert not await task and not analysis.trend_summary(uid)['items']

@pytest.mark.asyncio
async def test_account_deletion_cancels_generation_and_requests(client,accounts,papers,batch,monkeypatch):
    from app.user_management import delete_user
    uid=accounts[1]['user']['id'];interests(uid);analysis.request_update(uid);started=asyncio.Event();cancelled=asyncio.Event()
    async def complete(*args,**kwargs):
        started.set()
        try:await asyncio.Future()
        finally:cancelled.set()
    monkeypatch.setattr(analysis.models,'complete',complete)
    task=asyncio.create_task(analysis.ensure_direction_trend(uid));await asyncio.wait_for(started.wait(),2)
    await delete_user(uid,accounts[0]['user']);assert cancelled.is_set()
    with pytest.raises(asyncio.CancelledError):await task
    assert not one('SELECT * FROM users WHERE id=?',(uid,)) and not rows('SELECT * FROM arxiv_trend_requests WHERE user_id=?',(uid,))

@pytest.mark.asyncio
async def test_slow_analysis_does_not_block_http(client,accounts,papers,batch,monkeypatch):
    from app.main import app
    uid=accounts[0]['user']['id'];interests(uid);mocked(monkeypatch)
    started=threading.Event();release=threading.Event();original=analysis.research_materials
    def slow(*args,**kwargs):
        started.set();assert release.wait(3);return original(*args,**kwargs)
    monkeypatch.setattr(analysis,'research_materials',slow)
    task=asyncio.create_task(analysis.ensure_direction_trend(uid))
    try:
        assert await asyncio.to_thread(started.wait,2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as web:
            response=await asyncio.wait_for(web.get('/api/feed/today',headers=headers(accounts[0])),1)
        assert response.status_code==200 and response.json()['items'] and not task.done()
    finally:release.set();await task

@pytest.mark.asyncio
async def test_daily_task_never_creates_weekly_archive_or_notification(client,accounts,papers,batch,monkeypatch):
    for account in accounts:interests(account['user']['id'])
    calls=mocked(monkeypatch);assert await trend_report()==1 and len(calls)==1
    assert not rows('SELECT * FROM trend_reports') and not rows("SELECT * FROM notifications WHERE type='trend_report'")

@pytest.mark.asyncio
async def test_classification_prioritizes_latest_without_repeating_failures(client,papers,batch,monkeypatch):
    from app.pipeline import classify as classifier
    old=execute("INSERT INTO papers(title,abstract,created_at,ingested_date) VALUES('Historical','Abstract',?,?)",(now(),today()))
    order=[]
    async def service():pass
    async def classify(p):order.append(p['id']);execute('UPDATE papers SET classified=1 WHERE id=?',(p['id'],))
    monkeypatch.setattr(classifier,'check_service',service);monkeypatch.setattr(classifier,'classify_paper',classify)
    assert await classifier.classify(batch_id=batch)==3 and set(order)==set(papers)
    assert old not in order
    assert await classifier.classify()==1 and order[-1]==old

@pytest.mark.asyncio
async def test_scheduled_report_defers_while_pipeline_busy(client,accounts,monkeypatch):
    from app import scheduler
    interests(accounts[0]['user']['id'])
    monkeypatch.setattr(scheduler,'job_state',lambda:{'busy':True})
    async def fail(*args,**kwargs):raise AssertionError('busy pipeline must not start trends')
    monkeypatch.setattr(scheduler,'run_job',fail)
    assert await scheduler.run_scheduled('trend_report') is None
    assert rows('SELECT * FROM arxiv_trend_requests')

@pytest.mark.asyncio
async def test_daily_preparation_uses_stoppable_child_without_lock_deadlock(client,papers,batch,monkeypatch):
    from app import scheduler
    monkeypatch.setattr(scheduler,'_pipeline_lock',asyncio.Lock())
    for name in ('_job_tasks','_running_jobs','_stop_requests'):monkeypatch.setattr(scheduler,name,{})
    monkeypatch.setattr(scheduler,'_stopping',set())
    started=asyncio.Event();seen=[]
    async def sync(**kwargs):return daily.latest()
    async def classify(batch_id=None):
        seen.append(batch_id);started.set();await asyncio.Future()
    jobs=dict(scheduler.jobs);jobs['classify']=classify
    monkeypatch.setattr(daily,'sync_latest',sync);monkeypatch.setattr(scheduler,'jobs',jobs)
    task=asyncio.create_task(scheduler.run_job('trend_report'))
    await asyncio.wait_for(started.wait(),2)
    assert scheduler.job_state()['active']==['classify'] and seen==[batch]
    await scheduler.stop_jobs('classify')
    with pytest.raises(asyncio.CancelledError):await task
    assert not scheduler.job_state()['busy']
    assert one("SELECT error FROM source_status WHERE name='trend_report'")['error']==scheduler.STOP_MESSAGE

@pytest.mark.asyncio
async def test_disabled_classifier_is_not_started_by_daily_preparation(client,papers,batch,monkeypatch):
    from app import scheduler
    from app.pipeline_control import set_job_enabled
    monkeypatch.setattr(scheduler,'_pipeline_lock',asyncio.Lock())
    set_job_enabled('classify',False);called=[]
    async def sync(**kwargs):return daily.latest()
    async def report():called.append('report')
    async def fail(**kwargs):raise AssertionError('disabled classification must remain disabled')
    jobs=dict(scheduler.jobs);jobs.update(classify=fail,trend_report=report)
    monkeypatch.setattr(daily,'sync_latest',sync);monkeypatch.setattr(scheduler,'jobs',jobs)
    await asyncio.wait_for(scheduler.run_job('trend_report'),2)
    assert called==['report']

def test_removing_batch_member_invalidates_analysis_revision(client,papers,batch):
    before=analysis.latest_material()
    execute('DELETE FROM arxiv_batch_papers WHERE batch_id=? AND paper_id=?',(batch,papers[0]))
    assert analysis.latest_material()>before

def test_multilingual_interests_reuse_saved_queries_and_accented_names(client,accounts,papers,batch,monkeypatch):
    uid=accounts[0]['user']['id']
    profile=put_profile(uid,'## 核心兴趣（长期）\n- [w:0.9] Turan问题',{'category_selection':{'categories':['arxiv:cs.AI'],'topics':{}}},'manual')
    execute("UPDATE papers SET title='Turán density',abstract='We determine a Turán density threshold.' WHERE id=?",(papers[0],))
    profile,structured,_=analysis.profile_context(uid)
    material=analysis.research_materials(profile,structured)
    assert papers[0] in {p['id'] for g in material['groups'] if g.get('kind')!='daily_overview' for p in g['papers']}
    profile=dict(profile);profile['content']='## 核心兴趣（长期）\n- [w:0.9] 记忆机制'
    profile['embedding_parts']=dumps([{'text':'记忆机制','query':'记忆机制 — agent memory mechanisms'}])
    execute("UPDATE papers SET abstract='Agent memory mechanisms across tasks.' WHERE id=?",(papers[2],))
    material=analysis.research_materials(profile,structured)
    assert papers[2] in {p['id'] for g in material['groups'] if g.get('kind')!='daily_overview' for p in g['papers']}
