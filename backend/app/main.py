import asyncio
import os
import subprocess
import sys
import html
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.responses import JSONResponse
from .llm.secrets import SecretStorageError
from .config import settings
from .db import init_db, one
from .auth import router as auth_router, secret
from .api.content import router as content_router
from .api.profile import router as profile_router
from .api.chat import router as chat_router
from .api.admin import router as admin_router
from .api.model_config import router as model_router
from .api.source_categories import router as source_category_router
from .api.topic_proposals import router as proposal_router
from .api.site import router as site_router
from .api.prompts import router as prompt_router
from .api.skills import router as skill_router, personal_router as personal_skill_router
from . import site_settings
from .scheduler import make_scheduler, bootstrap, _manual_tasks
from .pipeline.read import _tasks
from .pipeline import reading_queue
from .logs import event, maintain as maintain_logs


@asynccontextmanager
async def lifespan(app):
    init_db(recover=settings().pipeline_mode=='inline')
    secret()
    from .paper_index import backfill
    index_stop=threading.Event()
    index_task=asyncio.create_task(asyncio.to_thread(backfill,index_stop))
    from .background_load import publish_loop
    load_task=asyncio.create_task(publish_loop())
    log_task=asyncio.create_task(maintain_logs())
    scheduler = make_scheduler()
    task, worker, interest_task, author_task, deletion_task = None, None, None, None, None
    if settings().pipeline_mode=='inline':
        from . import source_deletion_queue
        deletion_task=asyncio.create_task(source_deletion_queue.serve())
        from .pipeline import author_runs
        from . import scheduler as task_scheduler
        author_runs.recover()
        author_task=asyncio.create_task(task_scheduler.author_continuations())
        from .interest import profile_updates
        interest_task=asyncio.create_task(profile_updates.serve())
        reading_queue.recover()
        reading_queue.dispatch()
        if settings().scheduler_enabled: scheduler.start()
        if settings().bootstrap_enabled: task = asyncio.create_task(bootstrap())
    elif settings().pipeline_mode=='process':
        env = dict(os.environ)
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
        worker = subprocess.Popen([sys.executable,'-m','app.worker'],env=env,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    yield
    log_task.cancel()
    await asyncio.gather(log_task,return_exceptions=True)
    load_task.cancel()
    await asyncio.gather(load_task,return_exceptions=True)
    if scheduler.running: scheduler.shutdown(wait=False)
    if author_task:
        author_task.cancel()
        await asyncio.gather(author_task,return_exceptions=True)
        await task_scheduler.stop_jobs('author_impact',preserve_author=True)
    if deletion_task:
        deletion_task.cancel()
        await asyncio.gather(deletion_task,return_exceptions=True)
    pending = list(_manual_tasks)+list(_tasks.values())+([task] if task else [])+([interest_task] if interest_task else [])
    for running in pending: running.cancel()
    if pending: await asyncio.gather(*pending,return_exceptions=True)
    index_stop.set()
    await index_task
    if worker:
        worker.terminate()
        try: await asyncio.to_thread(worker.wait,10)
        except subprocess.TimeoutExpired: worker.kill()
    from .vector_search import close as close_vector_reader
    close_vector_reader()
    from .vector_store import close as close_vector_store
    close_vector_store()


app = FastAPI(title='刷论文',version='1.0.0',lifespan=lifespan)
from .background_load import WebLoadMiddleware
app.add_middleware(WebLoadMiddleware)


@app.middleware('http')
async def request_logs(request, call_next):
    try:
        response = await call_next(request)
    except Exception as error:
        event('system','接口执行异常',level='error',path=request.url.path,method=request.method,error_type=type(error).__name__)
        raise
    if request.url.path.startswith('/api/admin/') and request.method in ('POST','PUT','PATCH','DELETE'):
        actor = None
        authorization = request.headers.get('authorization','')
        if authorization.lower().startswith('bearer '):
            from .auth import decode
            try:
                actor = int(decode(authorization[7:], 'access')['sub'])
            except HTTPException:
                pass
        event('admin','管理员操作',user_id=actor,path=request.url.path,method=request.method,status=response.status_code)
    elif response.status_code>=500:
        event('system','接口返回错误',level='error',path=request.url.path,status=response.status_code)
    return response


@app.exception_handler(SecretStorageError)
async def secret_storage_error(request, error):
    return JSONResponse({'detail':str(error)}, status_code=503)
for router in [auth_router,content_router,profile_router,chat_router,admin_router,model_router,source_category_router,proposal_router,site_router,prompt_router,skill_router,personal_skill_router]:
    app.include_router(router)


@app.get('/api/health')
def health():
    return {'status':'ok','papers':one('SELECT COUNT(*) AS n FROM papers')['n']}


@app.get('/{path:path}',include_in_schema=False)
def frontend(path: str):
    if path.startswith('api/'):
        raise HTTPException(404,'接口不存在')
    root = settings().frontend_dir.resolve()
    entry=site_settings.entry_for('/'+path)
    if not entry and site_settings.is_retired('/'+path):
        raise HTTPException(404,'页面不存在')
    target = (root/path).resolve()
    if target.is_relative_to(root) and target.is_file():
        return FileResponse(target,headers={'Cache-Control':'no-cache'} if target.name in ('sw.js','index.html') else {})
    index = root/'index.html'
    if index.exists():
        if not entry and path not in site_settings.PUBLIC_PATHS:
            raise HTTPException(404,'页面不存在')
        config=site_settings.public_configuration()
        content=index.read_text(encoding='utf-8')
        import re
        content=re.sub(r'<title>.*?</title>',lambda _: '<title>'+html.escape(config['name'])+'</title>',content)
        content=re.sub(r'<meta name="description" content="[^"]*"\s*/?>',lambda _: '<meta name="description" content="'+html.escape(config['description'],quote=True)+'"/>',content)
        if config['favicon_url']:
            content=re.sub(r'<link rel="icon" href="[^"]*"\s*/?>',lambda _: '<link rel="icon" href="'+html.escape(config['favicon_url'],quote=True)+'"/>',content)
        if entry:content=content.replace('</head>','<meta name="app-admin-base" content="'+html.escape(entry,quote=True)+'"/></head>')
        return HTMLResponse(content,headers={'Cache-Control':'no-store'})
    raise HTTPException(404,'前端尚未构建，请执行 npm run build')
