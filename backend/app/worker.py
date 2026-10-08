"""Run the scheduler and pipeline independently from FastAPI."""
import asyncio
import os
import signal
from datetime import datetime, timedelta
from contextlib import contextmanager

from .config import settings, now
from .db import init_db, connect, execute, rows
from .logs import event
from . import scheduler
from .pipeline_control import publish_state, publish_heartbeat, job_enabled
from .pipeline import reading_queue,fulltext_cache


def ensure_reading_consumer(task=None):
    if task is not None and not task.done():
        return task
    if task is not None:
        error=None if task.cancelled() else task.exception()
        event('reading','精读队列消费循环已退出，重新启动',level='error',job='reading_queue',
              error_type=type(error).__name__ if error else 'CancelledOrStopped')
    return asyncio.create_task(reading_queue.serve())


@contextmanager
def single_worker():
    path = settings().data_dir / 'pipeline-worker.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if os.name=='nt':
            import msvcrt
            handle.seek(0); handle.write(b'0'); handle.flush(); handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


async def heartbeat_loop():
    while True:
        await asyncio.to_thread(publish_heartbeat,{**scheduler.job_state(),'pid':os.getpid()})
        await asyncio.sleep(1)


async def serve():
    os.environ['PIPELINE_WORKER']='1'
    init_db(recover=False)
    from .pipeline import author_runs
    author_runs.recover()
    author_task=asyncio.create_task(scheduler.author_continuations())
    from .llm import vector_rebuild
    if (rebuild:=vector_rebuild.pending()) and (rebuild['status'] in ('running','indexing','applying') or rebuild['status']=='queued' and not rows("SELECT id FROM pipeline_commands WHERE status='queued' AND action='start' AND name IN ('pipeline','build_vectors')")):
        vector_rebuild.update('stopped','工作进程重启，可继续重建')
    fulltext_cache.recover_pins()
    cache_task=asyncio.create_task(fulltext_cache.maintain())
    from . import vector_store
    vector_task=asyncio.create_task(vector_store.maintain())
    reading_task=ensure_reading_consumer()
    from .interest import profile_updates
    interest_task=asyncio.create_task(profile_updates.serve())
    from . import source_deletion_queue
    deletion_task=asyncio.create_task(source_deletion_queue.serve())
    execute('UPDATE source_status SET running=0')
    execute("UPDATE pipeline_commands SET status='interrupted' WHERE status='processing'")
    execute("UPDATE pipeline_redo_runs SET status='stopped' WHERE status='running' OR status='queued' AND id NOT IN (SELECT redo_id FROM pipeline_commands WHERE status='queued' AND redo_id IS NOT NULL)")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(signum, stop.set)
        except NotImplementedError: signal.signal(signum, lambda *_:loop.call_soon_threadsafe(stop.set))
    timer = scheduler.make_scheduler()
    if settings().scheduler_enabled: timer.start()
    bootstrap = asyncio.create_task(scheduler.bootstrap()) if settings().bootstrap_enabled else None
    event('system','流水线工作进程已启动',pid=os.getpid())
    heartbeat_task=asyncio.create_task(heartbeat_loop())
    next_trend_check=0.0
    try:
        while not stop.is_set():
            reading_task=ensure_reading_consumer(reading_task)
            if loop.time()>=next_trend_check:
                next_trend_check=loop.time()+10
                if not scheduler.job_state()['busy'] and job_enabled('trend_report') and not rows("SELECT id FROM pipeline_commands WHERE status='queued' LIMIT 1") and rows('SELECT r.user_id FROM arxiv_trend_requests r JOIN users u ON u.id=r.user_id WHERE u.disabled=0 LIMIT 1'):
                    state=rows("SELECT error,last_run FROM source_status WHERE name='trend_report'")
                    recent_failure=bool(state and state[0]['error'] and state[0]['last_run'] and datetime.fromisoformat(now())-datetime.fromisoformat(state[0]['last_run'])<timedelta(minutes=5))
                    if (not state or state[0]['error']!=scheduler.STOP_MESSAGE) and not recent_failure:
                        scheduler.start_manual('trend_report')
            # Publish before and after dispatch so the UI sees both queued and running states.
            await asyncio.to_thread(publish_state, {**scheduler.job_state(), 'pid':os.getpid()})
            commands = await asyncio.to_thread(rows, "SELECT * FROM pipeline_commands WHERE status='queued' ORDER BY id LIMIT 20")
            for command in commands:
                execute("UPDATE pipeline_commands SET status='processing' WHERE id=?", (command['id'],))
                try:
                    if command['action']=='stop':
                        await scheduler.stop_jobs(command['name'])
                    elif command['name']=='pipeline' or job_enabled(command['name']):
                        scheduler.start_manual(command['name'],redo_id=command['redo_id'])
                        await asyncio.sleep(0)
                    else:
                        if command['redo_id']:execute("UPDATE pipeline_redo_runs SET status='stopped' WHERE id=?",(command['redo_id'],))
                        event('task','排队任务已禁用，跳过执行',job=command['name'])
                    execute("UPDATE pipeline_commands SET status='done' WHERE id=?", (command['id'],))
                except Exception as error:
                    execute("UPDATE pipeline_commands SET status='failed',error=? WHERE id=?", (type(error).__name__,command['id']))
                    event('task','后台指令执行失败',level='error',job=command['name'],error_type=type(error).__name__)
            await asyncio.to_thread(publish_state, {**scheduler.job_state(), 'pid':os.getpid()})
            try: await asyncio.wait_for(stop.wait(), 1)
            except TimeoutError: pass
    finally:
        if timer.running: timer.shutdown(wait=False)
        author_task.cancel()
        await asyncio.gather(author_task,return_exceptions=True)
        await scheduler.stop_jobs('pipeline',preserve_author=True)
        if bootstrap:
            bootstrap.cancel()
            await asyncio.gather(bootstrap,return_exceptions=True)
        reading_task.cancel()
        await asyncio.gather(reading_task,return_exceptions=True)
        deletion_task.cancel()
        await asyncio.gather(deletion_task,return_exceptions=True)
        interest_task.cancel()
        await asyncio.gather(interest_task,return_exceptions=True)
        cache_task.cancel()
        await asyncio.gather(cache_task,return_exceptions=True)
        vector_task.cancel()
        await asyncio.gather(vector_task,return_exceptions=True)
        vector_store.close()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task,return_exceptions=True)
        await asyncio.to_thread(publish_state, {**scheduler.job_state(), 'pid':None})


if __name__=='__main__':
    with single_worker():
        asyncio.run(serve())
