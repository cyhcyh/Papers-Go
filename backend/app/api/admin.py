import json
import secrets
import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from ..auth import admin_user
from ..config import settings, now, today
from ..db import rows, one, execute, connect, dumps
from ..llm.ollama import ollama
from ..scheduler import jobs, start_manual, job_state, stop_jobs, STOP_MESSAGE
from ..catalog import directory, validate_category_keys
from ..standard_topics import catalog as standards, search as search_standards, scope_keys, approve, clean_profile_references, save_area, DISCIPLINES, equivalent_name
from ..logs import event
from ..disciplines import Discipline
from ..pipeline_control import enabled_jobs, job_enabled, set_job_enabled, public_state, queue_command
from ..task_readiness import missing_configuration, require_configuration

router = APIRouter(prefix='/api/admin',dependencies=[Depends(admin_user)])
from .task_center import router as task_center_router
router.include_router(task_center_router)


@router.get('/logs')
def logs(kind: str = '', level: str = '', job: str = '', query: str = Query('', max_length=100),
         since: date | None = None, until: date | None = None,
         offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
    from ..site_settings import configuration
    site = configuration()
    policy = {'retention_days':site['log_retention_days'], 'max_entries':site['log_max_entries']}
    conditions = ['created_at>=?']
    args = [(datetime.now(timezone.utc)-timedelta(days=policy['retention_days'])).isoformat()]
    if since:
        conditions.append('created_at>=?'); args.append(since.isoformat())
    if until:
        conditions.append('created_at<?'); args.append((until+timedelta(days=1)).isoformat())
    for key, value in [('kind',kind), ('level',level), ('job',job)]:
        if value:
            conditions.append(key+'=?'); args.append(value)
    if query:
        conditions.append('(message LIKE ? OR model LIKE ? OR detail LIKE ?)')
        args += ['%'+query+'%']*3
    where = ' WHERE '+' AND '.join(conditions) if conditions else ''
    total = one('SELECT COUNT(*) n FROM app_logs'+where, args)['n']
    result = rows('SELECT * FROM app_logs'+where+' ORDER BY id DESC LIMIT ? OFFSET ?', [*args,limit,offset])
    return {'items':[{**r,'detail':json.loads(r['detail'])} for r in result], 'total':total, 'policy':policy}


@router.get('/topics')
def topics():
    cutoff = (date.fromisoformat(today())-timedelta(days=7)).isoformat()
    result = rows('SELECT t.*,COUNT(pt.paper_id) AS paper_count FROM topics t LEFT JOIN paper_topics pt ON pt.topic_id=t.id GROUP BY t.id')
    interactions = rows("SELECT pt.topic_id,COUNT(*) AS shown,SUM(CASE WHEN i.action='like' THEN 1 ELSE 0 END) AS liked FROM interactions i JOIN paper_topics pt ON pt.paper_id=i.paper_id WHERE i.created_at>=? AND i.action IN ('like','save','skip') AND NOT EXISTS(SELECT 1 FROM interactions u WHERE u.target_id=i.id AND u.action='undo') GROUP BY pt.topic_id",(cutoff,))
    rates = {i['topic_id']:i['liked']/i['shown'] for i in interactions}
    pending={r['topic_id']:r['n'] for r in rows('SELECT topic_id,COUNT(*) n FROM topic_pending_papers GROUP BY topic_id')}
    return [{**t,'category_keys':json.loads(t['category_keys'] or '[]'),'like_rate':rates.get(t['id'],0),'pending_count':pending.get(t['id'],0)} for t in result]


@router.get('/standards')
def standard_directory(query: str=Query('',max_length=150),system: str='',parent: str|None=None):
    return search_standards(query,system,parent,500)


@router.get('/topics/{topic_id}/pending')
def pending_papers(topic_id: int):
    return rows('SELECT p.id,p.title,p.primary_category,p.venue,l.confidence,c.reason,c.evidence FROM topic_pending_papers l JOIN papers p ON p.id=l.paper_id LEFT JOIN paper_classifications c ON c.paper_id=p.id WHERE l.topic_id=? ORDER BY l.paper_id LIMIT 30',(topic_id,))


@router.get('/topics/{topic_id}/proposals')
def topic_proposals(topic_id:int):
    return rows('SELECT p.note,p.created_at,u.username FROM topic_proposals p JOIN users u ON u.id=p.user_id WHERE p.topic_id=? ORDER BY p.id',(topic_id,))


@router.get('/categories')
def categories():
    return directory(include_proposed=True)


class Topic(BaseModel):
    name_zh: str = Field(min_length=1,max_length=100)
    name_en: str = Field(min_length=1,max_length=200)
    parent_id: int | None = None
    status: Literal['active','proposed','merged','disabled','legacy'] = 'active'
    category_keys: list[str] | None = None
    standard_key: str | None = None
    discipline: Discipline | None = None
    description: str | None = Field(None,min_length=1,max_length=1200)


def topic_fields(body,previous=None,*,directory=None,source_keys=None):
    key=previous['standard_key'] if previous else body.standard_key
    if directory is None:directory=standards()
    known=directory.get(key)
    if not known and not key:
        known=next((e for e in directory.values() if equivalent_name(body.name_en,e['label'])),None)
        key=known['key'] if known else None
    if key and key not in directory:raise HTTPException(400,'研究方向 ID 不存在')
    discipline=body.discipline or (previous['discipline'] if previous else None) or (known['discipline'] if known else None)
    description=body.description if body.description is not None else (previous['description'] if previous else '') or (known['description'] if known else '')
    if discipline not in DISCIPLINES or not description.strip():raise HTTPException(400,'请填写学科和研究方向描述')
    keys=body.category_keys if body.category_keys is not None else json.loads(previous['category_keys'] or '[]') if previous else []
    if source_keys is None:keys=validate_category_keys(keys,include_inactive=True)
    else:
        if not set(keys)<=source_keys:raise HTTPException(400,'分类不存在或尚未开放')
        keys=list(dict.fromkeys(keys))
    if not keys:raise HTTPException(400,'请选择至少一个所属主类')
    if not body.name_en.strip() or not body.name_zh.strip():raise HTTPException(400,'名称不能为空')
    return key,discipline,description.strip(),keys


def validate_flat_topic(parent_id):
    if parent_id is not None:raise HTTPException(400,'主题直接属于 arXiv 分类或顶会，不支持子主题')


def topic_conflict(db,name,key,ignore=None):
    for t in db.execute('SELECT id,name_en,standard_key FROM topics'):
        if t['id']!=ignore and ((key and t['standard_key']==key) or equivalent_name(name,t['name_en'])):
            raise HTTPException(409,'已有同名主题，请编辑、批准或恢复已有主题')


@router.post('/topics')
def create_topic(body: Topic):
    key,discipline,description,keys=topic_fields(body)
    validate_flat_topic(body.parent_id)
    try:
        with connect() as db:
            db.execute('BEGIN IMMEDIATE');topic_conflict(db,body.name_en,key)
            ident=db.execute("INSERT INTO topics(name_zh,name_en,parent_id,status,created_by,created_at,category_keys,standard_key,discipline,description) VALUES(?,?,NULL,?,'admin',?,?,?,?,?)",
                             (body.name_zh.strip(),body.name_en.strip(),body.status,now(),dumps(keys),key,discipline,description)).lastrowid
            if body.status=='active':approve(db,ident)
            elif key:save_area(db,key,discipline,body.name_en.strip(),description)
    except ValueError as error:raise HTTPException(400,str(error)) from error
    event('topic','管理员新增主题',topic_id=ident)
    return {'id':ident}


@router.patch('/topics/{topic_id}')
def edit_topic(topic_id: int,body: Topic):
    previous=one('SELECT * FROM topics WHERE id=?',(topic_id,))
    if not previous:raise HTTPException(404,'主题不存在')
    key,discipline,description,keys=topic_fields(body,previous)
    validate_flat_topic(body.parent_id)
    try:
        with connect() as db:
            db.execute('BEGIN IMMEDIATE');topic_conflict(db,body.name_en,key,topic_id)
            if previous['status']=='active' and body.status!='active':raise HTTPException(400,'请使用停用操作同步移除论文标签')
            db.execute('UPDATE topics SET name_zh=?,name_en=?,parent_id=NULL,status=?,category_keys=?,standard_key=?,discipline=?,description=? WHERE id=?',
                       (body.name_zh.strip(),body.name_en.strip(),body.status,dumps(keys),key,discipline,description,topic_id))
            attached=approve(db,topic_id) if body.status=='active' else 0
            if body.status!='active' and key:
                area=save_area(db,key,discipline,body.name_en.strip(),description)
                db.execute('UPDATE topics SET standard_code=?,standard_system=?,standard_path=? WHERE id=?',(key,discipline,area['path'],topic_id))
    except ValueError as error:raise HTTPException(400,str(error)) from error
    event('topic','管理员批准或更新主题',topic_id=topic_id,status=body.status,assigned_papers=attached)
    return {'ok':True}


def remove_topic_records(db,ids,permanent=False):
    db.execute('CREATE TEMP TABLE removed_topics(id INTEGER PRIMARY KEY)')
    db.executemany('INSERT INTO removed_topics VALUES(?)',[(i,) for i in ids])
    db.execute('CREATE TEMP TABLE affected_topic_papers(id INTEGER PRIMARY KEY)')
    db.execute('INSERT INTO affected_topic_papers SELECT paper_id FROM paper_topics WHERE topic_id IN (SELECT id FROM removed_topics) UNION SELECT paper_id FROM topic_pending_papers WHERE topic_id IN (SELECT id FROM removed_topics)')
    affected=db.execute('SELECT COUNT(*) FROM affected_topic_papers').fetchone()[0]
    db.execute("UPDATE papers SET classified=0,classification_state='pending' WHERE id IN (SELECT id FROM affected_topic_papers)")
    for table in ('paper_topics','topic_pending_papers','topic_daily_stats'):
        db.execute('DELETE FROM '+table+' WHERE topic_id IN (SELECT id FROM removed_topics)')
    if permanent:
        db.execute('UPDATE topics SET parent_id=NULL WHERE parent_id IN (SELECT id FROM removed_topics)')
        # The disabled-topic row holds its ban; permanent deletion releases it.
        db.execute('DELETE FROM topics WHERE id IN (SELECT id FROM removed_topics)')
    else:db.execute("UPDATE topics SET status='disabled',parent_id=NULL WHERE id IN (SELECT id FROM removed_topics)")
    clean_profile_references(db,set(ids))
    db.execute("UPDATE direction_trends SET attempted_at=NULL,evidence='[]',summary='',items_json='[]' WHERE audience!='guest'")
    return affected


@router.delete('/topics/{topic_id}')
def delete_topic(topic_id: int, permanent: bool = False):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        topic = db.execute('SELECT * FROM topics WHERE id=?',(topic_id,)).fetchone()
        if not topic: raise HTTPException(404,'主题不存在')
        if permanent and topic['status'] not in ('disabled','legacy','merged'):
            raise HTTPException(400,'请先停用或拒绝该主题，再彻底删除')
        affected=remove_topic_records(db,[topic_id],permanent)
    event('topic','主题已彻底删除，可重新提议' if permanent else '主题已停用，论文标签已移除',topic_id=topic_id,standard_key=topic['standard_key'],affected_papers=affected)
    return {'ok':True,'affected_papers':affected,'deleted':permanent}


class Merge(BaseModel):
    target_id: int


class TopicBatch(BaseModel):
    ids: list[int] = Field(min_length=1)
    action: Literal['approve','reject','disable','restore','delete']


@router.post('/topics/batch')
def batch_topics(body: TopicBatch):
    from ..source_catalog import supported_keys
    completed,failed = [],[]
    approving=body.action in ('approve','restore')
    directory=standards() if approving else None
    source_keys=supported_keys(False) if approving else None
    attached=affected=0
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TEMP TABLE selected_topics(id INTEGER PRIMARY KEY)')
        ids=list(dict.fromkeys(body.ids))
        db.executemany('INSERT INTO selected_topics VALUES(?)',[(i,) for i in ids])
        found={t['id']:dict(t) for t in db.execute('SELECT t.* FROM topics t JOIN selected_topics s ON s.id=t.id')}
        for ident in ids:
            topic=found.get(ident)
            db.execute('SAVEPOINT topic_item')
            try:
                if not topic:raise HTTPException(404,'主题不存在')
                allowed=(body.action in ('approve','reject') and topic['status']=='proposed' or
                         body.action=='disable' and topic['status']=='active' or
                         body.action=='restore' and topic['status']=='disabled' or
                         body.action=='delete' and topic['status'] in ('disabled','legacy','merged'))
                if not allowed:raise HTTPException(400,'主题当前状态不支持此操作')
                if approving:
                    item=Topic(name_zh=topic['name_zh'],name_en=topic['name_en'],status='active',
                               discipline=topic['discipline'],description=topic['description'] or None,
                               category_keys=json.loads(topic['category_keys'] or '[]'))
                    key,discipline,description,keys=topic_fields(item,topic,directory=directory,source_keys=source_keys)
                    topic_conflict(db,item.name_en,key,ident)
                    db.execute('UPDATE topics SET name_zh=?,name_en=?,parent_id=NULL,discipline=?,description=?,category_keys=? WHERE id=?',
                               (item.name_zh.strip(),item.name_en.strip(),discipline,description,dumps(keys),ident))
                    attached+=approve(db,ident)
                completed.append(ident)
            except (HTTPException,ValueError) as error:
                db.execute('ROLLBACK TO topic_item')
                failed.append({'id':ident,'error':error.detail if isinstance(error,HTTPException) else str(error)})
            finally:db.execute('RELEASE topic_item')
        if completed and not approving:affected=remove_topic_records(db,completed,body.action=='delete')
    event('topic','批量管理主题',action=body.action,topic_ids=completed,completed=len(completed),failed=len(failed),affected_papers=affected,assigned_papers=attached)
    return {'completed':completed,'failed':failed}


@router.post('/topics/{topic_id}/merge')
def merge_topic(topic_id: int,body: Merge):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        source = db.execute('SELECT * FROM topics WHERE id=?',(topic_id,)).fetchone()
        target = db.execute("SELECT * FROM topics WHERE id=? AND status='active'",(body.target_id,)).fetchone()
        if not source or source['status']=='merged' or not target or topic_id==body.target_id: raise HTTPException(400,'请选择有效的不同主题')
        if target['standard_key'] not in standards(): raise HTTPException(400,'目标必须为已批准的主题')
        links=db.execute('SELECT paper_id,confidence FROM paper_topics WHERE topic_id=? UNION ALL SELECT paper_id,confidence FROM topic_pending_papers WHERE topic_id=?',(topic_id,topic_id)).fetchall()
        entry=standards()[target['standard_key']]
        for link in links:
            paper=dict(db.execute('SELECT * FROM papers WHERE id=?',(link['paper_id'],)).fetchone())
            from ..taxonomy import paper_keys
            if not scope_keys(entry,paper_keys(paper)): raise HTTPException(400,'关联论文不属于已开放分类')
            db.execute('DELETE FROM paper_topics WHERE paper_id=?',(link['paper_id'],))
            db.execute('INSERT INTO paper_topics VALUES(?,?,?)',(link['paper_id'],body.target_id,link['confidence']))
            db.execute("UPDATE papers SET classified=1,classification_state='ready' WHERE id=?",(link['paper_id'],))
        db.execute('DELETE FROM paper_topics WHERE topic_id=?',(topic_id,))
        db.execute('DELETE FROM topic_pending_papers WHERE topic_id=?',(topic_id,))
        keys = json.loads(source['category_keys'] or '[]') + json.loads(target['category_keys'] or '[]')
        keys = scope_keys(entry,set(keys))
        db.execute('UPDATE topics SET category_keys=? WHERE id=?', (dumps(keys), body.target_id))
        db.execute("UPDATE topics SET status='merged' WHERE id=?",(topic_id,))
        for p in db.execute('SELECT * FROM interest_profile WHERE id IN(SELECT MAX(id) FROM interest_profile GROUP BY user_id)').fetchall():
            structured = json.loads(p['structured'])
            ids = structured.get('topic_ids',[])
            partial = structured.get('category_selection', {}).get('topics', {})
            if topic_id in ids or any(topic_id in scoped for scoped in partial.values()):
                structured['topic_ids']=list({body.target_id if t==topic_id else t for t in ids})
                for key, scoped in partial.items():
                    partial[key] = sorted({body.target_id if t == topic_id else t for t in scoped})
                from ..interest.profile import put_profile
                put_profile(p['user_id'],p['content'],structured,'topic_merge',p['embedding'],db)
    from ..pipeline.trends import trend_stats
    trend_stats()
    event('topic','主题已合并',topic_id=topic_id,target_id=body.target_id,affected_papers=len(links))
    return {'ok':True}


@router.get('/users')
def users():
    return rows('SELECT id,username,is_admin,disabled,created_at FROM users ORDER BY id')


from ..user_management import UserUpdate, edit_user as update_account, delete_user as remove_account, deletion_impact
from ..user_management import UserBatch, batch_impact, disable_users, delete_users


@router.post('/users/batch/impact')
def users_impact(body: UserBatch,admin=Depends(admin_user)):
    return batch_impact(body.ids,admin)


@router.post('/users/batch/disable')
async def users_disable(body: UserBatch,admin=Depends(admin_user)):
    return await disable_users(body.ids,admin)


class UsersDelete(UserBatch):
    confirm_text: str


@router.delete('/users/batch')
async def users_delete(body: UsersDelete,admin=Depends(admin_user)):
    if body.confirm_text!=f'删除 {len(set(body.ids))} 个用户':raise HTTPException(400,'请输入正确的删除确认文字')
    return await delete_users(body.ids,admin)


@router.patch('/users/{user_id}')
def edit_user(user_id: int,body: UserUpdate,admin=Depends(admin_user)):
    return update_account(user_id,body,admin)


@router.get('/users/{user_id}/impact')
def user_impact(user_id: int):
    return deletion_impact(user_id)


@router.delete('/users/{user_id}')
async def delete_user(user_id: int,admin=Depends(admin_user)):
    return await remove_account(user_id,admin)


@router.post('/invites')
def invite():
    code = secrets.token_urlsafe(12)
    execute('INSERT INTO invite_codes(code,created_at) VALUES(?,?)',(code,now()))
    return {'code':code}


@router.get('/metrics/daily')
def daily_metrics(days: int=Query(30,ge=1,le=365),user_id: int | None=None):
    cutoff = (date.fromisoformat(today())-timedelta(days=days-1)).isoformat()
    condition = ' AND user_id=?' if user_id else ''
    return rows('SELECT date,SUM(shown) AS shown,SUM(skipped) AS skipped,SUM(liked) AS liked,SUM(saved) AS saved,SUM(expanded) AS expanded,1.0*SUM(liked)/MAX(1,SUM(considered)) AS like_rate,1.0*SUM(saved)/MAX(1,SUM(considered)) AS save_rate,SUM(quick_skip_rate*considered)/MAX(1,SUM(considered)) AS quick_skip_rate,1.0*SUM(expanded)/MAX(1,SUM(considered)) AS expand_rate FROM daily_metrics WHERE date>=?'+condition+' GROUP BY date ORDER BY date',(cutoff,user_id) if user_id else (cutoff,))


@router.get('/sources')
async def sources():
    if settings().pipeline_mode != 'inline':
        return await asyncio.to_thread(source_snapshot)
    return source_snapshot()


def source_snapshot():
    found = {s['name']:s for s in rows('SELECT * FROM source_status')}
    state = public_state()
    switches = enabled_jobs()
    from ..llm import runtime as models
    model_config=models.configuration()
    from ..pipeline.redo import latest_runs
    redo=latest_runs()
    from ..pipeline import author_runs
    author_continuation=author_runs.snapshot()
    from ..pipeline import paper_retries
    retries=paper_retries.snapshot()
    return [{**found.get(name,{'name':name,'last_run':None,'last_success':None,'added':0,'error':None,'running':0}),
             **({'continuation':author_continuation} if name=='author_impact' else {}),
             'progress':json.loads(found.get(name,{}).get('progress') or '{}'),
             'missing_configuration':missing_configuration([name],model_config,switches),
             'retries':retries.get(name),'redo':redo.get(name),'enabled':switches.get(name,True), 'queued':name in state['queued'], 'stopped':found.get(name,{}).get('error')==STOP_MESSAGE} for name in jobs]


from ..pipeline.redo import RedoOptions, preview as redo_preview, create_run, state as redo_state, validate_resume


@router.post('/jobs/{name}/redo/preview')
def preview_redo(name: str, body: RedoOptions):
    return redo_preview(name,body)


def enqueue_redo(name, ident):
    if settings().pipeline_mode=='inline':
        if job_state()['busy']:raise HTTPException(409,'当前有任务运行，请先停止或稍后再试')
        start_manual(name,redo_id=ident)
    else:queue_command('redo',name,ident)


@router.post('/jobs/{name}/retry-failed')
async def retry_failed(name: str):
    from ..pipeline import paper_retries
    from .. import prompts
    if name not in paper_retries.FEATURES:raise HTTPException(404,'此任务不支持重试失败项')
    if not job_enabled(name):raise HTTPException(409,'任务已禁用，请先启用')
    require_configuration(name)
    if name=='assess_quality' and not prompts.skill_enabled('quality'):
        raise HTTPException(409,'质量评分技能已停用，请先启用')
    def arrange():
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Model migrations must finish in the staging index, never overwrite
            # active vectors with another model's output through a normal redo.
            from ..llm import vector_rebuild
            rebuilding=name=='build_vectors' and vector_rebuild.pending()
            ident=paper_retries.create_retry_run(db,name)
            if settings().pipeline_mode=='inline':
                if job_state()['busy']:raise HTTPException(409,'当前有任务运行，请先停止或稍后再试')
                start_manual(name,**({} if rebuilding else {'redo_id':ident}))
            else:queue_command('start' if rebuilding else 'redo',name,None if rebuilding else ident,db=db)
            if rebuilding:
                db.execute('DELETE FROM pipeline_redo_runs WHERE id=?',(ident,))
        return {'queued':True}
    value=arrange() if settings().pipeline_mode=='inline' else await asyncio.to_thread(arrange)
    event('task','管理员安排重试失败论文',job=name)
    return value


@router.post('/jobs/{name}/redo')
async def new_redo(name: str,body: RedoOptions):
    if not job_enabled(name):raise HTTPException(409,'任务已禁用，请先启用')
    require_configuration(name)
    if public_state()['busy']:raise HTTPException(409,'当前有任务运行，请先停止或稍后再试')
    ident=create_run(name,body)
    try:enqueue_redo(name,ident)
    except Exception:
        execute("UPDATE pipeline_redo_runs SET status='stopped' WHERE id=?",(ident,))
        raise
    return redo_state(ident)


@router.post('/jobs/{name}/redo/{ident}/resume')
async def resume_redo(name: str,ident: int):
    if not job_enabled(name):raise HTTPException(409,'任务已禁用，请先启用')
    require_configuration(name)
    validate_resume(ident,name)
    if public_state()['busy']:raise HTTPException(409,'当前有任务运行，请先停止或稍后再试')
    execute("UPDATE pipeline_redo_runs SET status='queued',updated_at=? WHERE id=?",(now(),ident))
    try:enqueue_redo(name,ident)
    except Exception:
        execute("UPDATE pipeline_redo_runs SET status='stopped' WHERE id=?",(ident,))
        raise
    return redo_state(ident)


@router.get('/jobs')
async def task_state():
    return public_state()


class JobSwitch(BaseModel):
    enabled: bool


@router.patch('/jobs/{name}')
def configure_job(name: str,body: JobSwitch):
    if name not in jobs: raise HTTPException(404,'任务不存在')
    set_job_enabled(name,body.enabled)
    event('task','管理员启用任务' if body.enabled else '管理员禁用任务',job=name)
    return {'name':name,'enabled':body.enabled}


@router.post('/jobs/{name}/stop')
async def stop(name: str):
    if name not in jobs and name!='pipeline': raise HTTPException(404,'任务不存在')
    if settings().pipeline_mode=='inline': return await stop_jobs(name)
    state = public_state()
    from ..pipeline import author_runs
    author_pending=author_runs.read() if name in ('pipeline','author_impact') else None
    if not state['busy'] and not (author_pending and author_pending['phase'] in author_runs.WAITING):
        return {'requested':False,**state}
    ident = await asyncio.to_thread(queue_command,'stop',name)
    for _ in range(30):
        await asyncio.sleep(.1)
        command = one('SELECT status FROM pipeline_commands WHERE id=?',(ident,))
        if command['status'] in ('done','failed'): break
    return {'requested':True,**public_state()}


@router.post('/jobs/{name}')
async def run(name: str):
    if name not in jobs and name!='pipeline': raise HTTPException(404,'任务不存在')
    if name!='pipeline' and not job_enabled(name): raise HTTPException(409,'任务已禁用，请先启用')
    require_configuration(name)
    if settings().pipeline_mode=='inline':
        if job_state()['busy'] or any(s['running'] for s in rows('SELECT running FROM source_status')): raise HTTPException(409,'当前有任务运行，请先停止或稍后再试')
        start_manual(name)
    else: await asyncio.to_thread(queue_command,'start',name)
    return {'queued':True}


@router.get('/llm-status')
async def llm_status():
    from ..llm import runtime as model_runtime
    config=model_runtime.configuration()
    choice=config['routes']['chat']['primary']
    connection=next((c for c in config['connections'] if c['id']==choice['connection_id']),{})
    return {'base_url':connection.get('base_url',''),'configured':model_runtime.configured('chat'),
        'models':{key:config['routes'][feature]['primary']['model'] for key,feature in (('chat','chat'),('precise','reading_l2'),('fast','audit'))},
        'ollama':await ollama.status() if any(c['kind']=='ollama' for c in config['connections']) else {'available':False,'ready':False,'models':[]},
        'usage':rows('SELECT substr(created_at,1,10) AS date,model,SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens FROM llm_usage WHERE billing_source="api" AND created_at>=? GROUP BY date,model',(today(),)),
        'audit':one("SELECT detail,created_at FROM audit_log WHERE action='classification.audit' ORDER BY id DESC LIMIT 1")}
