import asyncio
import inspect
import logging
import time
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from .config import settings, now
from .db import execute, one, connect
from .pipeline.fetch import fetch_arxiv, fetch_conf
from .pipeline.community import fetch_community
from .pipeline.author_impact import author_impact
from .pipeline.classify import classify
from .pipeline.embed import build_vectors, assess_quality
from .pipeline.tldr import tldr_gen
from .pipeline.read import preread
from .pipeline.trends import trend_stats, trend_report
from .pipeline.alerts import alert_eval
from .pipeline.metrics import metrics, audit
from .pipeline.expire import expire_papers
from .interest.reflect import reflect
from .llm.ollama import ollama
from .llm import runtime as models
from .logs import event
from .pipeline_control import job_enabled, cancellation_scope
from .task_settings import INDEPENDENT_JOBS, schedules as task_schedules, trigger as schedule_trigger
from .task_readiness import DEPENDENCIES, ready_for_automatic

logger = logging.getLogger(__name__)
jobs = {'fetch_arxiv':fetch_arxiv,'fetch_conf':fetch_conf,'fetch_community':fetch_community,
        'classify':classify,'build_vectors':build_vectors,'assess_quality':assess_quality,'tldr_gen':tldr_gen,'preread':preread,
        'trend_stats':trend_stats,'alert_eval':alert_eval,'metrics':metrics,'reflect':reflect,
        'trend_report':trend_report,'audit':audit,'author_impact':author_impact,'paper_expiry':expire_papers}
_pipeline_lock = asyncio.Lock()
_manual_tasks = set()
_manual_names = {}
_job_tasks = {}
_running_jobs = {}
_pipeline_tasks = set()
_stopping = set()
_stop_requests = {}
_restarting = set()
STOP_MESSAGE = '已手动停止'


def job_state():
    tasks = set(_manual_tasks) | _pipeline_tasks | {task for group in _job_tasks.values() for task in group}
    active = sorted(_running_jobs)
    queued = sorted(name for name, group in _job_tasks.items() if any(task is not _running_jobs.get(name) for task in group))
    stopping_names = sorted(name for name, group in _job_tasks.items() if any(task in _stopping and not task.done() for task in group))
    return {'busy':any(not task.done() for task in tasks), 'active':active, 'queued':queued,
            'pipeline':bool(_pipeline_tasks) or 'pipeline' in _manual_names.values(),
            'stopping':any(not task.done() for task in _stopping), 'stopping_names':stopping_names,
            'stopping_pipeline':any(task in _stopping and not task.done() for task in _pipeline_tasks)}


async def stop_jobs(name='pipeline', *, preserve_author=False):
    cancelled_continuation=False
    if name in ('pipeline','author_impact') and not preserve_author:
        from .pipeline import author_runs
        cancelled_continuation=author_runs.cancel()
    if name in ('pipeline','build_vectors'):
        from .llm import vector_rebuild
        if (rebuild:=vector_rebuild.pending()) and rebuild['status']=='queued':vector_rebuild.update('stopped')
    if name == 'pipeline':
        targets = set(_manual_tasks) | _pipeline_tasks | {task for group in _job_tasks.values() for task in group}
    else:
        targets = set(_job_tasks.get(name, ())) | {task for task, label in _manual_names.items() if label == name}
    targets = {task for task in targets if not task.done()}
    labels = {label for label, group in _job_tasks.items() if group & targets}
    labels.update(label for task, label in _manual_names.items() if task in targets and label!='pipeline')
    for label in labels - set(_running_jobs):
        execute('INSERT INTO source_status(name,running,error) VALUES(?,0,?) ON CONFLICT(name) DO UPDATE SET running=0,error=excluded.error',(label,STOP_MESSAGE))
    for task in targets - _stopping:
        if preserve_author and task in _job_tasks.get('author_impact', ()):
            _restarting.add(task)
        if requested:=_stop_requests.get(task):requested.set()
        _stopping.add(task)
        task.add_done_callback(_stopping.discard)
        task.cancel()
    if targets:
        await asyncio.wait(targets, timeout=5)
    return {'requested':bool(targets) or cancelled_continuation, **job_state()}


def _forget_job(name, task):
    group = _job_tasks.get(name)
    if group is not None:
        group.discard(task)
        if not group:
            _job_tasks.pop(name,None)


@models.model_task
async def run_job(name, fetch_limit=None, redo_id=None, force_conf=False, author_generation=None, daily_batch_id=None, retry_missing_authors=False):
    if not ready_for_automatic([name],label=name):
        if redo_id:execute("UPDATE pipeline_redo_runs SET status='stopped',updated_at=? WHERE id=?",(now(),redo_id))
        return
    task = asyncio.current_task()
    requested=asyncio.Event()
    _stop_requests[task]=requested
    _job_tasks.setdefault(name,set()).add(task)
    try:
        with cancellation_scope(requested):
            if name=='trend_report' and job_enabled(name):
                # Preparation runs before the report acquires the pipeline lock.
                # Its classification is a normal stoppable child job, never a
                # recursive acquisition of the same lock or a historical rescan.
                from .pipeline.arxiv_daily import sync_latest, latest
                execute("INSERT INTO source_status(name,last_run,error) VALUES('trend_report',?,NULL) ON CONFLICT(name) DO UPDATE SET last_run=excluded.last_run,error=NULL",(now(),))
                try:
                    async with _pipeline_lock:await sync_latest()
                except asyncio.CancelledError:raise
                except Exception as error:
                    execute("INSERT INTO source_status(name,error) VALUES('trend_report',?) ON CONFLICT(name) DO UPDATE SET error=excluded.error",(models.safe_error(error)[:500],))
                    event('trend','公告同步失败，保留上一期',job=name,level='error',error=models.safe_error(error)[:500])
                    return
                batch=latest()
                if batch and job_enabled('classify') and one('SELECT 1 FROM arxiv_batch_papers b JOIN papers p ON p.id=b.paper_id WHERE b.batch_id=? AND p.classified=0 LIMIT 1',(batch['id'],)):
                    child=asyncio.create_task(run_job('classify',daily_batch_id=batch['id']))
                    try:await child
                    finally:
                        if not child.done():child.cancel()
                        await asyncio.gather(child,return_exceptions=True)
            await _run_job(name, fetch_limit, task, redo_id, force_conf, author_generation, daily_batch_id, retry_missing_authors)
    except asyncio.CancelledError:
        if name=='trend_report':
            # Stopping its preparation child must not leave an automatic request
            # that immediately restarts classification in the idle worker loop.
            execute("INSERT INTO source_status(name,running,error) VALUES('trend_report',0,?) ON CONFLICT(name) DO UPDATE SET running=0,error=excluded.error",(STOP_MESSAGE,))
        raise
    finally:
        _stop_requests.pop(task,None)
        _restarting.discard(task)
        _forget_job(name, task)
        if redo_id:
            execute("UPDATE pipeline_redo_runs SET status='stopped',updated_at=? WHERE id=? AND status IN ('queued','running')", (now(),redo_id))


async def _run_job(name, fetch_limit, task, redo_id=None, force_conf=False, author_generation=None, daily_batch_id=None, retry_missing_authors=False):
    async with _pipeline_lock:
        if not job_enabled(name):
            if redo_id:execute("UPDATE pipeline_redo_runs SET status='stopped' WHERE id=?",(redo_id,))
            event('task','任务已禁用，跳过执行',job=name)
            return
        if name=='author_impact' and author_generation is not None:
            from .pipeline import author_runs
            saved=author_runs.read()
            if not saved or saved['generation']!=author_generation or saved['phase']!='running':return
        _running_jobs[name] = task
        execute('INSERT INTO source_status(name,last_run,running,error) VALUES(?,?,1,NULL) ON CONFLICT(name) DO UPDATE SET last_run=excluded.last_run,running=1,error=NULL',(name,now()))
        execute("UPDATE source_status SET progress='{}' WHERE name=?", (name,))
        if name=='fetch_arxiv':
            execute('UPDATE source_status SET added=0 WHERE name=?',(name,))
        started = time.perf_counter()
        event('task','任务开始',job=name)
        author_token=None
        try:
            if name=='author_impact':
                from .pipeline import author_runs
                if retry_missing_authors:
                    execute('DELETE FROM author_query_failures')
                author_token=author_generation if author_generation is not None else author_runs.begin()
            function = jobs[name]
            if redo_id:
                from .pipeline.redo import run
                result = await run(redo_id,name)
            elif name=='classify' and daily_batch_id is not None:
                result=await function(batch_id=daily_batch_id)
            elif name == 'fetch_arxiv' and fetch_limit is not None:
                result = await function(limit=fetch_limit)
            elif name == 'fetch_conf' and 'force' in inspect.signature(function).parameters:
                result = await function(force=force_conf)
            else:
                if inspect.iscoroutinefunction(function):
                    result = await function()
                else:
                    # A synchronous SQLite transaction finishes before the task releases its lock.
                    worker = asyncio.create_task(asyncio.to_thread(function))
                    try:
                        result = await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        try:
                            await worker
                        except Exception:
                            pass
                        raise
            if author_token is not None:
                continuation=author_runs.finish(author_token)
                execute("UPDATE source_status SET last_success=CASE WHEN ? THEN ? ELSE last_success END,added=?,running=0 WHERE name=?",
                        (continuation['phase']=='complete',now(),continuation['processed'],name))
            else:
                execute('UPDATE source_status SET last_success=?,added=?,running=0 WHERE name=?',(now(),result if isinstance(result,int) else 0,name))
            event('task','任务完成',job=name,seconds=round(time.perf_counter()-started,2),result=result)
        except asyncio.CancelledError:
            restarting=author_token is not None and task in _restarting
            if author_token is not None:
                if restarting:author_runs.recover()
                else:author_runs.cancel()
            execute('UPDATE source_status SET running=0,error=? WHERE name=?',(None if restarting else STOP_MESSAGE if task in _stopping else '任务被中断',name))
            event('task','任务已停止',job=name,seconds=round(time.perf_counter()-started,2))
            raise
        except Exception as error:
            # Source failures remain visible; other scheduled stages continue.
            execute('UPDATE source_status SET error=?,running=0 WHERE name=?',(models.safe_error(error)[:500],name))
            if author_token is not None:
                author_runs.finish(author_token,error=models.safe_error(error)[:500])
            logger.warning('%s failed: %s',name,type(error).__name__)
            event('task','任务失败',level='error',job=name,error=models.safe_error(error),seconds=round(time.perf_counter()-started,2))
        finally:
            _running_jobs.pop(name,None)


async def _stages(names, fetch_limit=None, force_conf=False):
    children = []
    for name in names:
        arguments={}
        if name=='fetch_arxiv' and fetch_limit is not None:arguments['fetch_limit']=fetch_limit
        if name=='fetch_conf' and force_conf:arguments['force_conf']=True
        task = asyncio.create_task(run_job(name,**arguments))
        _job_tasks.setdefault(name,set()).add(task)
        task.add_done_callback(lambda done, label=name:_forget_job(label,done))
        children.append(task)
    try:
        for child in children:
            try:
                await asyncio.shield(child)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                # Stopping one stage leaves its siblings queued in the pipeline.
    finally:
        for child in children:
            if not child.done():
                if child not in _stopping:
                    child.cancel()
        await asyncio.gather(*children, return_exceptions=True)


@models.model_task
async def pipeline(fetch_limit=None, force_conf=False):
    names=[name for name in jobs if name not in INDEPENDENT_JOBS]
    if not ready_for_automatic(names,label='pipeline'):return
    task = asyncio.current_task()
    _pipeline_tasks.add(task)
    try:
        await _stages(names, fetch_limit, force_conf)
    finally:
        _pipeline_tasks.discard(task)


def start_manual(name, redo_id=None, *, author_generation=None):
    arguments={'author_generation':author_generation} if author_generation is not None else {}
    if name=='author_impact' and author_generation is None:
        arguments['retry_missing_authors']=True
    task = asyncio.create_task(pipeline(force_conf=True) if name=='pipeline' else run_job(name,redo_id=redo_id,force_conf=True,**arguments))
    _manual_tasks.add(task)
    _manual_names[task] = name
    task.add_done_callback(_manual_tasks.discard)
    task.add_done_callback(lambda done:_manual_names.pop(done,None))
    return task


async def continue_author_if_idle():
    # Pending continuations never reserve the pipeline lock or block manual jobs.
    if job_state()['busy']:
        return None
    from .pipeline import author_runs
    generation=await asyncio.to_thread(author_runs.claim)
    return start_manual('author_impact',author_generation=generation) if generation is not None else None


async def author_continuations():
    while True:
        await asyncio.sleep(5)
        try:
            await continue_author_if_idle()
        except Exception as error:
            logger.warning('author continuation failed: %s',type(error).__name__)


@models.model_task
async def bootstrap():
    task = asyncio.current_task()
    _pipeline_tasks.add(task)
    try:
        await _bootstrap()
    finally:
        _pipeline_tasks.discard(task)


async def _bootstrap():
    def resume_enabled(name):
        state=one('SELECT error FROM source_status WHERE name=?',(name,))
        return job_enabled(name) and (not state or state['error']!=STOP_MESSAGE)
    planned=[name for name in jobs if name not in INDEPENDENT_JOBS and resume_enabled(name)]
    if not ready_for_automatic(planned,label='pipeline'):return
    if resume_enabled('fetch_arxiv') and not one('SELECT id FROM papers LIMIT 1'):
        await _stages(['fetch_arxiv'], fetch_limit=settings().fetch_limit)
    if not one('SELECT id FROM papers WHERE classified=0 OR embedding IS NULL OR scored=0 OR brief_json IS NULL LIMIT 1'):
        return
    # Model downloads run in the Ollama container; the web app remains usable.
    stages = [name for name in ('classify','build_vectors','assess_quality','tldr_gen','preread','trend_stats','alert_eval','metrics') if resume_enabled(name)]
    if not stages: return
    for _ in range(360):
        config = models.configuration()
        features = set()
        for name in stages:
            features.update(DEPENDENCIES.get(name,()))
        bindings = [models.resolve(config['routes'][name]['primary'],config) for name in features]
        endpoints = {b['base_url']:b for b in bindings if b['kind']=='ollama'}
        ready = True
        for binding in endpoints.values():
            with models.bind(binding):
                ready = ready and (await ollama.status())['ready']
        if ready:
            await _stages(stages)
            return
        await asyncio.sleep(30)


async def run_scheduled(name):
    names=[stage for stage in jobs if stage not in INDEPENDENT_JOBS] if name=='pipeline' else [name]
    if not ready_for_automatic(names,label=name):return
    # Manual executions and previous scheduled runs share the same deduplication.
    if name=='trend_report' and job_state()['busy']:
        event('trend','流水线忙，延后自动趋势更新',job=name)
        # The worker will consume this small request when it next becomes idle.
        with connect() as db:
            db.execute("INSERT OR IGNORE INTO arxiv_trend_requests SELECT p.user_id,? FROM interest_profile p JOIN users u ON u.id=p.user_id WHERE u.disabled=0 AND p.created_at>=date('now','-14 days') GROUP BY p.user_id",(now(),))
        return
    if name == 'pipeline':
        duplicate = _pipeline_tasks or 'pipeline' in _manual_names.values() or any(
            _job_tasks.get(stage) or stage in _manual_names.values()
            for stage in jobs if stage not in INDEPENDENT_JOBS)
    else:
        duplicate = _job_tasks.get(name) or name in _manual_names.values()
    if duplicate:
        event('task', '已有同类任务，跳过重复定时触发', job=name)
        return
    if name == 'pipeline':
        await pipeline()
    else:
        await run_job(name)


def apply_schedules(scheduler, values):
    previous = getattr(scheduler, '_task_schedules', {})
    for name, value in values.items():
        if previous.get(name) == value:
            continue
        existing = scheduler.get_job(name)
        if not value['enabled']:
            if existing:
                scheduler.remove_job(name)
        elif existing:
            scheduler.reschedule_job(name, trigger=schedule_trigger(value))
        else:
            scheduler.add_job(run_scheduled, schedule_trigger(value), args=[name], id=name)
    scheduler._task_schedules = values


async def refresh_schedules(scheduler):
    # One indexed scalar read every ten seconds; parse only after a configuration change.
    found = await asyncio.to_thread(one, "SELECT updated_at FROM app_settings WHERE name='task_center'")
    revision = found['updated_at'] if found else ''
    if revision != scheduler._task_revision:
        values, revision = await asyncio.to_thread(task_schedules)
        apply_schedules(scheduler, values)
        scheduler._task_revision = revision


def make_scheduler():
    scheduler = AsyncIOScheduler(timezone=settings().tz, job_defaults={'coalesce':True, 'max_instances':1, 'misfire_grace_time':3600})
    values, scheduler._task_revision = task_schedules()
    apply_schedules(scheduler, values)
    scheduler.add_job(refresh_schedules, 'interval', seconds=10, args=[scheduler], id='_task_settings_refresh')
    return scheduler
