from ..llm import runtime as models
import asyncio
import time
import httpx
from ..db import rows, execute, connect, dumps, one
from ..config import now, settings
from ..taxonomy import paper_keys
from ..standard_topics import queue_or_assign, queue_new_topic, catalog
from ..topic_semantics import semantic_candidates,ensure_index
from ..logs import event
from .topic_decision import predict_topic
from ..pipeline_control import check_cancelled


async def check_service():
    config = models.configuration()
    route = config['routes']['classify']
    failures = []
    for choice in [route['primary']] + ([route['fallback']] if route.get('fallback') else []):
        binding = models.resolve(choice,config)
        if binding['kind']=='codex':
            from ..llm.codex import credentials
            try:
                await credentials(binding['id']);return
            except ValueError as error:
                failures.append(str(error));continue
        if binding['kind']=='cloud':
            if binding.get('api_key'):
                return
            failures.append('分类云端模型尚未配置 API Key')
            continue
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(binding['base_url'].rstrip('/')+'/api/tags')
                response.raise_for_status()
                names = {item['name'] for item in response.json()['models']}
                if binding['model'] not in names and binding['model']+':latest' not in names:
                    failures.append('Ollama 尚未安装分类模型 '+binding['model'])
                    continue
                return
        except (httpx.HTTPError, ValueError, KeyError):
            failures.append('Ollama 分类服务不可用，请启动服务后重试')
    raise RuntimeError('；'.join(failures))


async def _classify_paper(paper):
    blocked={t['standard_key'] for t in rows("SELECT standard_key FROM topics WHERE status IN ('disabled','merged')")}
    options=await semantic_candidates(paper,blocked)
    if not options and not paper_keys(paper):
        with connect() as db:
            db.execute('DELETE FROM paper_topics WHERE paper_id=?',(paper['id'],))
            db.execute('DELETE FROM topic_pending_papers WHERE paper_id=?',(paper['id'],))
            db.execute("UPDATE papers SET classified=1,classification_state='unmatched' WHERE id=?",(paper['id'],))
        event('topic','论文不在已开放主题范围内',job='classify',paper_id=paper['id'],state='unmatched')
        return
    result=await predict_topic(paper,options)
    check_cancelled()
    key=result['standard_key'];confidence=result['confidence']
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        previous=[dict(r) for r in db.execute('SELECT t.id,t.name_zh,t.standard_key,l.confidence FROM paper_topics l JOIN topics t ON t.id=l.topic_id WHERE l.paper_id=? UNION ALL SELECT t.id,t.name_zh,t.standard_key,l.confidence FROM topic_pending_papers l JOIN topics t ON t.id=l.topic_id WHERE l.paper_id=?',(paper['id'],paper['id']))]
        db.execute('DELETE FROM paper_topics WHERE paper_id=?',(paper['id'],))
        db.execute('DELETE FROM topic_pending_papers WHERE paper_id=?',(paper['id'],))
        if key:
            ident,state=queue_or_assign(db,paper,catalog()[key],confidence,result.get('name_zh'))
        else:
            ident,state=queue_new_topic(db,paper,result['new_topic'],confidence)
        reason=result['reason']
        db.execute('INSERT INTO paper_classifications(paper_id,standard_key,confidence,reason,evidence,status,error,attempts,previous_result,method_version,updated_at) VALUES(?,?,?,?,?,?,NULL,?,?,\'medium-v1\',?) ON CONFLICT(paper_id) DO UPDATE SET standard_key=excluded.standard_key,confidence=excluded.confidence,reason=excluded.reason,evidence=excluded.evidence,status=excluded.status,error=NULL,attempts=excluded.attempts,previous_result=excluded.previous_result,method_version=\'medium-v1\',updated_at=excluded.updated_at',
                   (paper['id'],key,confidence,reason,result['evidence'],state,result['attempts'],dumps(previous),now()))
    event('topic','论文主题待批准' if state=='awaiting_approval' else '论文主题已分类',
          job='classify',paper_id=paper['id'],topic_id=ident,standard_key=key,confidence=confidence,state=state)


async def classify_paper(paper):
    try:
        return await _classify_paper(paper)
    except Exception as error:
        detail=models.safe_error(error)[:500]
        execute("INSERT INTO paper_classifications(paper_id,status,error,attempts,updated_at) VALUES(?,'failed',?,?,?) ON CONFLICT(paper_id) DO UPDATE SET status='failed',error=excluded.error,attempts=excluded.attempts,updated_at=excluded.updated_at",
                (paper['id'],detail,getattr(error,'attempts',1),now()))
        event('topic','论文分类失败，保留原结果',level='error',job='classify',paper_id=paper['id'],paper_title=paper['title'][:160],stage=getattr(error,'stage','classification'),error_type=type(error).__name__,error=detail)
        raise


@models.model_task
async def classify(limit=None, batch_id=None):
    from .arxiv_daily import latest
    latest_batch=latest()
    priority=batch_id or (latest_batch['id'] if latest_batch else None)
    membership='id IN (SELECT paper_id FROM arxiv_batch_papers WHERE batch_id=?)'
    total=one('SELECT COUNT(*) n FROM papers WHERE classified=0'+(' AND '+membership if batch_id else ''),[batch_id] if batch_id else [])['n']
    if limit is not None:
        total = min(total,limit)
    start = time.perf_counter()
    completed,failed,last_id,claimed = 0,0,0,0
    duration = 0.0
    binding = models.selected('classify')
    model = binding['model']
    concurrency = models.concurrency('classify')
    in_flight = {}
    def progress():
        elapsed = time.perf_counter()-start
        attempted = completed+failed
        paper = next(reversed(in_flight.values()), None)
        value = {'total':total,'completed':completed,'failed':failed,'pending':total-completed,
                 'current_paper':{'id':paper['id'],'title':paper['title'][:160]} if paper else None,
                 'model':model,'concurrency':concurrency,'in_flight':len(in_flight),
                 'elapsed_seconds':round(elapsed,1),'average_seconds':round(duration/attempted,1) if attempted else None,
                 'papers_per_minute':round(attempted/elapsed*60,1) if attempted and elapsed else None,
                 'estimated_remaining_seconds':round(elapsed/attempted*(total-attempted)) if attempted else None}
        execute("INSERT INTO source_status(name,progress) VALUES('classify',?) ON CONFLICT(name) DO UPDATE SET progress=excluded.progress", (dumps(value),))
    progress()
    if not total:
        return 0
    await check_service()
    await ensure_index()
    errors = []
    batch = iter(())
    phase=0 if priority else 1
    def next_paper():
        nonlocal batch, last_id, claimed, phase
        if claimed >= total:
            return None
        paper = next(batch, None)
        if paper is None:
            condition=(' AND '+membership if phase==0 else ' AND NOT '+membership) if priority else ''
            found=rows('SELECT * FROM papers WHERE classified=0 AND id>?'+condition+' ORDER BY id LIMIT 100',[last_id,*([priority] if priority else [])])
            if not found and phase==0 and batch_id is None:
                phase=1;last_id=0
                found=rows('SELECT * FROM papers WHERE classified=0 AND id>? AND NOT '+membership+' ORDER BY id LIMIT 100',[last_id,priority])
            if not found:return None
            last_id = found[-1]['id']
            batch = iter(found)
            paper = next(batch)
        claimed += 1
        return paper

    async def worker():
        nonlocal completed, failed, duration
        while True:
            check_cancelled()
            p=next_paper()
            if p is None:break
            in_flight[p['id']] = p
            progress()
            started = time.perf_counter()
            attempted = False
            try:
                await classify_paper(p)
                completed += 1
                attempted = True
                check_cancelled()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failed += 1
                attempted = True
                errors.append(type(error).__name__)
                event('task','分类子任务失败',level='error',job='classify',paper_id=p['id'],paper_title=p['title'][:160],stage='classification',error_type=type(error).__name__,error=models.safe_error(error)[:500])
                await check_service()
            finally:
                if attempted:
                    duration += time.perf_counter()-started
                in_flight.pop(p['id'], None)
                progress()

    workers = [asyncio.create_task(worker()) for _ in range(min(concurrency,total))]
    try:
        await asyncio.gather(*workers)
    finally:
        for task in workers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        progress()
    if errors:
        raise RuntimeError(f'{failed} 篇分类失败，将在下次重试；{errors[0]}')
    return completed
