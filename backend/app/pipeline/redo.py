"""Explicit, resumable reprocessing; existing results stay available until replacement succeeds."""
import asyncio
import json
from datetime import date
from fastapi import HTTPException
from pydantic import BaseModel, Field
from ..db import connect, rows, one, execute, dumps, set_paper_vector
from ..config import now, settings
from ..llm import runtime as models
from ..logs import event
from ..pipeline_control import check_cancelled
from ..interest.profile import CURRENT_PROFILE_IDS
from .. import prompts
from . import paper_retries

JOBS={'classify':['classification'],'tldr_gen':['brief'],
      'build_vectors':['embedding','profile_embedding'],'assess_quality':['quality'],'preread':['reading']}
SCHEMA='''
CREATE TABLE IF NOT EXISTS pipeline_redo_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,options TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,error TEXT);
CREATE TABLE IF NOT EXISTS pipeline_redo_items (
 id INTEGER PRIMARY KEY AUTOINCREMENT,run_id INTEGER NOT NULL REFERENCES pipeline_redo_runs(id) ON DELETE CASCADE,
 paper_id INTEGER REFERENCES papers(id) ON DELETE CASCADE,profile_id INTEGER REFERENCES interest_profile(id) ON DELETE CASCADE,
 component TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',error TEXT,
 CHECK((paper_id IS NULL)!=(profile_id IS NULL)),UNIQUE(run_id,paper_id,component),UNIQUE(run_id,profile_id,component));
CREATE INDEX IF NOT EXISTS idx_redo_pending ON pipeline_redo_items(run_id,status,id);
'''


def migrate_split_jobs(db):
    """Keep old switches, queued work and redo successes when splitting the combined job."""
    version='split-vector-quality-jobs-v1'
    if db.execute('SELECT 1 FROM app_migrations WHERE name=?',(version,)).fetchone():return
    saved=db.execute("SELECT value FROM app_settings WHERE name='pipeline_enabled'").fetchone()
    if saved:
        switches=json.loads(saved['value'])
        if 'embed_score' in switches:
            enabled=switches.pop('embed_score')
            for name in ('build_vectors','assess_quality'):switches.setdefault(name,enabled)
            db.execute("UPDATE app_settings SET value=?,updated_at=? WHERE name='pipeline_enabled'",(dumps(switches),now()))
    for name in ('build_vectors','assess_quality'):
        db.execute('''INSERT OR IGNORE INTO source_status(name,last_run,last_success,added,error,running)
            SELECT ?,last_run,last_success,0,error,0 FROM source_status WHERE name='embed_score' ''',(name,))
    db.execute("DELETE FROM source_status WHERE name='embed_score'")
    targets={}
    for row in db.execute("SELECT * FROM pipeline_redo_runs WHERE name='embed_score'").fetchall():
        # Historical versions no longer belong to an interest-vector redo.
        db.execute(f'DELETE FROM pipeline_redo_items WHERE run_id=? AND profile_id IS NOT NULL AND profile_id NOT IN ({CURRENT_PROFILE_IDS})',(row['id'],))
        options=json.loads(row['options'])
        present={r['component'] for r in db.execute('SELECT DISTINCT component FROM pipeline_redo_items WHERE run_id=?',(row['id'],))}
        if not present:present=set(options.get('components') or ['embedding','quality'])
        groups=[(name,[c for c in JOBS[name] if c in present]) for name in ('build_vectors','assess_quality')]
        groups=[(name,components) for name,components in groups if components]
        targets[row['id']]=[]
        for index,(name,components) in enumerate(groups):
            new_options={**options,'components':components}
            if name=='assess_quality':new_options.pop('embedding_identity',None)
            if index==0:
                ident=row['id']
                db.execute('UPDATE pipeline_redo_runs SET name=?,options=? WHERE id=?',(name,dumps(new_options),ident))
            else:
                ident=db.execute('''INSERT INTO pipeline_redo_runs(name,options,status,created_at,updated_at,error)
                    VALUES(?,?,?,?,?,?)''',(name,dumps(new_options),row['status'],row['created_at'],row['updated_at'],row['error'])).lastrowid
                for component in components:
                    db.execute('UPDATE pipeline_redo_items SET run_id=? WHERE run_id=? AND component=?',(ident,row['id'],component))
            targets[row['id']].append((name,ident))
        for name,ident in targets[row['id']]:
            counts=db.execute("SELECT COUNT(*) total,SUM(status='done') done,SUM(status='failed') failed FROM pipeline_redo_items WHERE run_id=?",(ident,)).fetchone()
            status='completed' if counts['total']==(counts['done'] or 0) else 'failed' if counts['failed'] else 'stopped' if row['status']=='running' else row['status']
            db.execute('UPDATE pipeline_redo_runs SET status=?,error=? WHERE id=?',(status,None if status=='completed' else row['error'],ident))
    for command in db.execute("SELECT * FROM pipeline_commands WHERE name='embed_score' AND status='queued'").fetchall():
        names=targets.get(command['redo_id']) if command['redo_id'] else None
        if names is None:
            # A model switch previously used this job only for staged vector rebuilding.
            rebuilding=db.execute("SELECT 1 FROM app_settings WHERE name='embedding_rebuild'").fetchone()
            names=[('build_vectors',None)] if rebuilding else [('build_vectors',None),('assess_quality',None)]
        for index,(name,ident) in enumerate(names):
            if index==0:db.execute('UPDATE pipeline_commands SET name=?,redo_id=? WHERE id=?',(name,ident,command['id']))
            else:db.execute('INSERT INTO pipeline_commands(action,name,created_at,redo_id) VALUES(?,?,?,?)',(command['action'],name,command['created_at'],ident))
    db.execute('INSERT INTO app_migrations VALUES(?,?)',(version,now()))


class RedoOptions(BaseModel):
    category_key: str = Field('',max_length=100)
    from_date: date | None = None
    to_date: date | None = None
    components: list[str] = Field(default_factory=list,max_length=2)
    paper_ids: list[int] = Field(default_factory=list,max_length=1000)


def selection(name, body):
    if name not in JOBS:raise HTTPException(404,'此任务不支持重做')
    if name=='assess_quality' and not prompts.skill_enabled('quality'):
        raise HTTPException(400,'质量评分技能已停用，请先在模型配置 → Skills 中启用')
    components=body.components or JOBS[name]
    if not set(components)<=set(JOBS[name]) or len(set(components))!=len(components):raise HTTPException(400,'重做项目无效')
    if body.from_date and body.to_date and body.from_date>body.to_date:raise HTTPException(400,'开始日期不能晚于结束日期')
    if components==['profile_embedding'] and (body.paper_ids or body.category_key or body.from_date or body.to_date):
        raise HTTPException(400,'兴趣向量不使用论文范围筛选')
    where=['1=1'];args=[]
    if body.paper_ids:
        ids=list(dict.fromkeys(body.paper_ids))
        if any(ident<=0 for ident in ids):raise HTTPException(400,'论文 ID 无效')
        where.append('p.id IN ('+','.join('?' for _ in ids)+')');args.extend(ids)
    if body.category_key:
        from ..source_catalog import registry
        if body.category_key not in {s['key'] for s in registry()}:raise HTTPException(400,'分类不存在')
        where.append('p.id IN (SELECT paper_id FROM paper_categories WHERE category_key=?)');args.append(body.category_key.casefold())
    for bound,op in ((body.from_date,'>='),(body.to_date,'<=')):
        if bound:where.append('COALESCE(p.published,p.ingested_date)'+op+'?');args.append(bound.isoformat())
    return components,' AND '.join(where),args


def preview(name, body):
    components,where,args=selection(name,body)
    paper_components=[c for c in components if c!='profile_embedding']
    count=one('SELECT COUNT(*) n FROM papers p WHERE '+where,args)['n'] if paper_components else 0
    profiles=one(f'SELECT COUNT(*) n FROM interest_profile WHERE id IN ({CURRENT_PROFILE_IDS})')['n'] if 'profile_embedding' in components else 0
    return {'papers':count,'profiles':profiles,'components':components,'operations':count*len(paper_components)+profiles}


def create_run(name, body):
    components,where,args=selection(name,body)
    options=body.model_dump(mode='json');options['components']=components
    if name=='build_vectors':options['embedding_identity']=models.embedding_identity(models.configuration())
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        ident=db.execute('INSERT INTO pipeline_redo_runs(name,options,created_at,updated_at) VALUES(?,?,?,?)',(name,dumps(options),now(),now())).lastrowid
        for component in components:
            if component=='profile_embedding':continue
            db.execute('INSERT INTO pipeline_redo_items(run_id,paper_id,component) SELECT ?,p.id,? FROM papers p WHERE '+where,[ident,component,*args])
        if 'profile_embedding' in components:
            db.execute(f"INSERT INTO pipeline_redo_items(run_id,profile_id,component) SELECT ?,id,'profile_embedding' FROM interest_profile WHERE id IN ({CURRENT_PROFILE_IDS})",(ident,))
        if not db.execute('SELECT id FROM pipeline_redo_items WHERE run_id=? LIMIT 1',(ident,)).fetchone():raise HTTPException(400,'所选范围没有论文或兴趣向量需要重做')
    return ident


def state(ident):
    run=one('SELECT * FROM pipeline_redo_runs WHERE id=?',(ident,))
    if not run:raise HTTPException(404,'重做记录不存在')
    counts=one("SELECT COUNT(*) total,SUM(status='done') completed,SUM(status='failed') failed FROM pipeline_redo_items WHERE run_id=?",(ident,))
    return {**run,'options':json.loads(run['options']),**{k:v or 0 for k,v in counts.items()}}


def latest_runs():
    return {r['name']:state(r['id']) for r in rows('SELECT id,name FROM pipeline_redo_runs WHERE id IN (SELECT MAX(id) FROM pipeline_redo_runs GROUP BY name)')}


def validate_resume(ident,name):
    run=state(ident)
    if run['name']!=name or name not in JOBS:raise HTTPException(400,'重做任务不匹配')
    if run['status'] in ('queued','running','completed'):raise HTTPException(409,'该重做任务无需继续或正在执行')
    identity=run['options'].get('embedding_identity')
    if identity and list(models.embedding_identity(models.configuration()))!=identity:raise HTTPException(409,'向量模型已变化，请使用当前模型新建重做任务')
    return run


async def process(item):
    component=item['component']
    if item['profile_id']:
        profile=one(f'SELECT * FROM interest_profile WHERE id=? AND id IN ({CURRENT_PROFILE_IDS})',(item['profile_id'],))
        if not profile:return
        from ..interest.profile import profile_embedding, embedding_inputs, save_current_embedding
        from ..llm.embedding_queue import background
        async with background():
            vector=await profile_embedding(profile['content'])
        if vector is None and embedding_inputs(profile['content']):raise ValueError('兴趣向量生成失败')
        if vector is not None and len(vector)!=settings().embedding_dim*4:raise ValueError('兴趣向量维度不匹配')
        check_cancelled()
        save_current_embedding(profile,vector)
        return
    paper=one('SELECT * FROM papers WHERE id=?',(item['paper_id'],))
    if not paper:return
    if component=='embedding':
        from .embed import embed_paper
        await embed_paper(paper)
    elif component=='quality':
        from .embed import score_paper
        await score_paper(paper)
    elif component=='classification':
        from .classify import classify_paper
        await classify_paper(paper)
    elif component=='reading':
        from .read import preread_paper
        await preread_paper(paper['id'])
    else:
        from .tldr import generate_brief
        await generate_brief(paper)


async def run(ident, name):
    record=state(ident)
    if record['name']!=name:raise ValueError('重做任务不匹配')
    if name=='assess_quality' and not prompts.skill_enabled('quality'):
        execute("UPDATE pipeline_redo_runs SET status='stopped',error='质量评分技能已停用，启用后可继续',updated_at=? WHERE id=?",(now(),ident))
        return 0
    identity=record['options'].get('embedding_identity')
    if identity and list(models.embedding_identity(models.configuration()))!=identity:
        execute("UPDATE pipeline_redo_runs SET status='failed',error='向量模型已变化，请新建重做任务',updated_at=? WHERE id=?",(now(),ident))
        raise ValueError('向量模型已变化，不能继续旧重做任务')
    execute("UPDATE pipeline_redo_runs SET status='running',error=NULL,updated_at=? WHERE id=?",(now(),ident))
    execute("UPDATE pipeline_redo_items SET status='pending',error=NULL WHERE run_id=? AND status!='done'",(ident,))
    feature=paper_retries.FEATURES[name]
    items=rows("SELECT * FROM pipeline_redo_items WHERE run_id=? AND status='pending' ORDER BY id",(ident,))
    active_component=None
    def progress():
        value=state(ident)
        stages=[]
        if name in ('build_vectors','assess_quality'):
            counts=rows("SELECT component,COUNT(*) total,SUM(status='done') completed,SUM(status='failed') failed FROM pipeline_redo_items WHERE run_id=? GROUP BY component",(ident,))
            for key,label in [('embedding','论文向量'),('profile_embedding','当前兴趣向量'),('quality','论文质量评估')]:
                stage=next((v for v in counts if v['component']==key),None)
                if stage:
                    stages.append({**stage,'key':key,'label':label,'pending':stage['total']-stage['completed']-stage['failed'],'unit':'份' if key=='profile_embedding' else '篇','model':models.selected('quality' if key=='quality' else 'embedding')['model']})
        execute("INSERT INTO source_status(name,progress) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET progress=excluded.progress",(name,dumps({'total':value['total'],'completed':value['completed'],'failed':value['failed'],'pending':value['total']-value['completed']-value['failed'],'model':models.selected(feature)['model'],'average_seconds':None,'current_paper':None,'estimated_remaining_seconds':None,'redo_id':ident,'unit':'项',**({'stages':stages,'stage':active_component} if stages else {})})))
    async def worker(pending):
        nonlocal active_component
        for item in pending:
            check_cancelled()
            active_component=item['component']
            execute("UPDATE pipeline_redo_items SET status='running' WHERE id=?",(item['id'],))
            progress()
            try:
                await process(item)
                execute("UPDATE pipeline_redo_items SET status='done',error=NULL WHERE id=?",(item['id'],))
            except asyncio.CancelledError:raise
            except Exception as error:
                execute("UPDATE pipeline_redo_items SET status='failed',error=? WHERE id=?",(models.safe_error(error)[:300],item['id']))
                event('task','论文重做失败，保留原结果',level='error',job=name,paper_id=item['paper_id'],component=item['component'],error_type=type(error).__name__)
            progress()
    tasks=[]
    cancelled=False
    try:
        progress()
        phases=JOBS[name]
        for component in phases:
            check_cancelled()
            pending=iter(item for item in items if item['component']==component)
            concurrency=models.concurrency('quality') if component=='quality' else models.embedding_parallelism(background=True) if component=='embedding' else 1 if component=='profile_embedding' else models.concurrency(feature)
            tasks=[asyncio.create_task(worker(pending)) for _ in range(max(1,concurrency))]
            await asyncio.gather(*tasks)
            check_cancelled()
    except asyncio.CancelledError:
        cancelled=True
        raise
    finally:
        for task in tasks:
            if not task.done():task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        execute("UPDATE pipeline_redo_items SET status='pending' WHERE run_id=? AND status='running'",(ident,))
        current=state(ident)
        status='stopped' if cancelled else 'failed' if current['failed'] else 'completed'
        execute('UPDATE pipeline_redo_runs SET status=?,updated_at=? WHERE id=?',(status,now(),ident))
        from .score import _ranking_cache, _cache_lock
        with _cache_lock:_ranking_cache.clear()
        progress()
    if current['failed']:raise RuntimeError(f"{current['failed']} 项重做失败；原结果已保留，可继续未完成部分")
    return current['completed']
