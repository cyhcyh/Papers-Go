"""Small real-provider verification. Run inside Docker; completed labels are retained."""
import argparse
import asyncio
from collections import Counter
from contextvars import ContextVar
import json
from pathlib import Path
import statistics
import time

from app.config import now, settings
from app.db import one, rows
from app.llm import runtime
from app.llm.provider import cloud, completion_options
from app.pipeline.classify import classify_paper
from app.taxonomy import ARXIV

paper_context = ContextVar('benchmark_paper', default=None)


async def benchmark(limit, paper_ids=None):
    with runtime.model_snapshot():
        binding = runtime.selected('classify')
        if binding['kind'] != 'cloud':
            raise RuntimeError('Select a cloud classifier before this verification')
        # Include prior problem examples, then balance pending papers across source categories.
        ids = paper_ids or [152,196,5272,5273]
        anchors = rows('SELECT * FROM papers WHERE id IN ('+','.join('?' for _ in ids)+') ORDER BY id',ids)
        chosen = anchors[:limit]
        selected_ids = {p['id'] for p in chosen}
        buckets = [rows('SELECT * FROM papers WHERE classified=0 AND primary_category=? ORDER BY id LIMIT ?', (code,limit))
                   for code, _, _ in ARXIV]
        while len(chosen)<limit and any(buckets):
            for bucket in buckets:
                if not bucket or len(chosen)>=limit:
                    continue
                p = bucket.pop(0)
                if p['id'] not in selected_ids:
                    chosen.append(p)
                    selected_ids.add(p['id'])
        if len(chosen)<limit:
            for p in rows('SELECT * FROM papers WHERE classified=0 ORDER BY id LIMIT ?', (limit*2,)):
                if len(chosen)>=limit:
                    break
                if p['id'] not in selected_ids:
                    chosen.append(p)
                    selected_ids.add(p['id'])

        usage = {}
        original_record = cloud.record_usage
        def record(model, value):
            original_record(model, value)
            if value:
                details = getattr(value,'completion_tokens_details',None)
                usage[paper_context.get()] = {'input_tokens':value.prompt_tokens,
                    'output_tokens':value.completion_tokens,
                    'reasoning_tokens':getattr(details,'reasoning_tokens',None)}
        cloud.record_usage = record
        semaphore = asyncio.Semaphore(settings().classify_cloud_concurrency)
        results = []
        started = time.perf_counter()
        async def process(p):
            async with semaphore:
                token = paper_context.set(p['id'])
                begin = time.perf_counter()
                item = {'id':p['id'],'title':p['title'],'abstract':p['abstract'],
                        'category':p['primary_category'],'anchor':p['id'] in {a['id'] for a in anchors}}
                try:
                    await classify_paper(p)
                    item['topics'] = rows('SELECT t.name_en,t.name_zh,pt.confidence FROM paper_topics pt JOIN topics t ON t.id=pt.topic_id WHERE pt.paper_id=?', (p['id'],))
                    item['ok'] = True
                except Exception as error:
                    item.update(ok=False,error_type=type(error).__name__)
                finally:
                    item['seconds'] = round(time.perf_counter()-begin,3)
                    item['usage'] = usage.get(p['id'])
                    results.append(item)
                    paper_context.reset(token)
                    if len(results)%10==0 or len(results)==len(chosen):
                        print(json.dumps({'completed':len(results),'total':len(chosen),
                            'failed':sum(not r['ok'] for r in results),'elapsed_seconds':round(time.perf_counter()-started,1)}),flush=True)
        try:
            await asyncio.gather(*(process(p) for p in chosen))
        finally:
            cloud.record_usage = original_record
        elapsed = time.perf_counter()-started
        times = [r['seconds'] for r in results]
        reported_reasoning = [r['usage']['reasoning_tokens'] for r in results if r['usage'] and r['usage']['reasoning_tokens'] is not None]
        summary = {'created_at':now(),'model':binding['model'],
            'request_options':completion_options({**binding,'feature':'classify'},binding['model'],binding['base_url']),
            'concurrency':settings().classify_cloud_concurrency,'papers':len(results),
            'completed':sum(r['ok'] for r in results),'failed':sum(not r['ok'] for r in results),
            'elapsed_seconds':round(elapsed,2),'average_seconds':round(statistics.mean(times),3) if times else None,
            'median_seconds':round(statistics.median(times),3) if times else None,
            'p95_seconds':sorted(times)[max(0,int(len(times)*.95)-1)] if times else None,
            'papers_per_minute':round(len(results)/elapsed*60,2),
            'average_output_tokens':round(statistics.mean(r['usage']['output_tokens'] for r in results if r['usage']),1) if usage else None,
            'reported_reasoning_count':len(reported_reasoning),'reported_reasoning_tokens':sum(reported_reasoning),
            'categories':dict(Counter(p['category'] for p in results)),
            'remaining_papers':one('SELECT COUNT(*) n FROM papers WHERE classified=0')['n']}
        path = settings().data_dir/'benchmarks'/('classification-deepseek-nonthinking-'+time.strftime('%Y%m%d-%H%M%S')+'.json')
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps({'summary':summary,'results':sorted(results,key=lambda p:p['id'])},ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'summary':summary,'report':str(path)},ensure_ascii=True),flush=True)
        for item in results:
            if item['anchor']:
                print(json.dumps({'id':item['id'],'title':item['title'],'topics':item.get('topics'),'seconds':item['seconds']},ensure_ascii=True),flush=True)


parser = argparse.ArgumentParser()
parser.add_argument('--limit',type=int,default=100)
parser.add_argument('--ids',help='Comma-separated verification example IDs')
args = parser.parse_args()
if not 1<=args.limit<=100:
    parser.error('limit must be between 1 and 100')
asyncio.run(benchmark(args.limit,[int(v) for v in args.ids.split(',')] if args.ids else None))
