"""Real-provider evaluation in a temporary database; production labels stay untouched."""
import argparse
import asyncio
import json
from pathlib import Path
import random
import statistics
import tempfile
import time

from app.config import settings,now
from app.db import init_db,connect
from app.llm import runtime
from app.pipeline.topic_decision import predict_topic
from app.topic_semantics import semantic_candidates,ensure_index

DEV_IDS=[5,12,17,19,20,23,24,32,35,37,38,39,54,66,76,78,87,109,112,130,133,145,164,165,169,171,172,180,182,188,191,192,202,211,218,252,264,272,275,281,282,285,289,308,323,334,339,346,352,362,367,369,371,376,381]


async def evaluate(args):
    sample=json.loads(Path(args.sample).read_text())
    config=runtime.configuration()
    original_dir=settings().data_dir
    with tempfile.TemporaryDirectory(prefix='topic-validation-') as directory:
        settings().data_dir=Path(directory)
        settings().embedding_dim=config['embedding_dim']
        init_db(recover=False)
        with runtime.model_snapshot(config):
            if args.cache:
                import shutil
                shutil.copyfile(args.cache,Path(directory)/'topic_candidates.sqlite3')
            await ensure_index()
            with connect() as db:
                for s in sample['sources']:
                    columns=list(s)
                    db.execute('INSERT OR REPLACE INTO source_categories('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',[s[k] for k in columns])
            selected=[p for p in sample['papers'] if p['id'] in (DEV_IDS if args.mode=='dev' else sample['heldout_ids'])]
            results=[];start=time.perf_counter();semaphore=asyncio.Semaphore(4)
            async def process(p):
                async with semaphore:
                    before=time.perf_counter()
                    row={'id':p['id'],'title':p['title'],'abstract':p['abstract'],'category':p['primary_category'],'venue':p['venue']}
                    try:
                        retrieval_start=time.perf_counter();options=await semantic_candidates(p)
                        row['retrieval_ms']=round((time.perf_counter()-retrieval_start)*1000,2)
                        row['candidates']=[{'key':e['key'],'label':e['label']} for e in options]
                        if not options:raise ValueError('no candidates')
                        row['decision']=await predict_topic(p,options)
                        row['topic']=next((e['label'] for e in options if e['key']==row['decision']['standard_key']),None)
                        row['path']=next((e['path'] for e in options if e['key']==row['decision']['standard_key']),None)
                        row['ok']=True
                    except Exception as error:row.update(ok=False,error=runtime.safe_error(error)[:500])
                    row['seconds']=round(time.perf_counter()-before,3);results.append(row)
                    if len(results)%10==0 or len(results)==len(selected):
                        print(json.dumps({'mode':args.mode,'completed':len(results),'total':len(selected),'failed':sum(not r['ok'] for r in results),'seconds':round(time.perf_counter()-start,1)}),flush=True)
            await asyncio.gather(*(process(p) for p in selected))
            times=[r['seconds'] for r in results]
            summary={'model':runtime.selected('classify')['model'],'thinking':runtime.selected('classify').get('thinking'),
                     'papers':len(results),'failed':sum(not r['ok'] for r in results),'review':sum(r.get('decision',{}).get('needs_review',False) for r in results),
                     'average_seconds':round(statistics.mean(times),3) if times else None,'elapsed_seconds':round(time.perf_counter()-start,2),
                     'retrieval_median_ms':round(statistics.median(r.get('retrieval_ms',0) for r in results),2) if results else None}
            Path(args.output).write_text(json.dumps({'created_at':now(),'mode':args.mode,'summary':summary,'results':sorted(results,key=lambda r:r['id'])},ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(summary),flush=True)
        settings().data_dir=original_dir


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--sample',required=True);parser.add_argument('--output',required=True);parser.add_argument('--cache');parser.add_argument('--mode',choices=['dev','heldout'],default='dev')
    asyncio.run(evaluate(parser.parse_args()))
