"""Compare old/new code against isolated copies; never call a model or modify live data."""
import argparse
import json
import os
from pathlib import Path
import shutil
import statistics
import sys
import threading
import time


parser=argparse.ArgumentParser()
parser.add_argument('--root',required=True)
parser.add_argument('--mode',choices=['old','new'],required=True)
parser.add_argument('--source',default='/data/backups/before-paper-lifecycle-20261005-154950.sqlite3')
parser.add_argument('--code',default='/tmp/vector-isolation-code')
args=parser.parse_args()
root=Path(args.root).resolve()
if root.parent!=Path('/data') or not root.name.startswith('vector-isolation-eval-'):
    raise SystemExit('Only an isolated evaluation directory is permitted')
directory=root/args.mode
directory.mkdir(parents=True,exist_ok=False)
shutil.copyfile(args.source,directory/'papers.sqlite3')
os.environ.update(DATA_DIR=str(directory),SCHEDULER_ENABLED='false',BOOTSTRAP_ENABLED='false',PIPELINE_MODE='external')
if args.mode=='new':sys.path.insert(0,args.code)
from app.config import settings,now
from app.db import connect,init_db,one
from app.pipeline import score
from app import vector_search

init_db(recover=False)
with connect() as db:
    user=db.execute('SELECT user_id FROM interest_profile WHERE embedding IS NOT NULL ORDER BY id DESC LIMIT 1').fetchone()
    uid=user[0] if user else None
    category=db.execute('SELECT category_key,COUNT(*) n FROM paper_categories GROUP BY category_key ORDER BY n DESC LIMIT 1').fetchone()[0]
results={'mode':args.mode,'papers':one('SELECT COUNT(*) n FROM papers')['n'],'category':category}
if args.mode=='new':
    from app import vector_store
    started=time.perf_counter();vector_store.ensure()
    results['initial_copy_seconds']=round(time.perf_counter()-started,3)
    print(json.dumps({'phase':'copied','seconds':results['initial_copy_seconds']}),flush=True)
    started=time.perf_counter()
    before=one("SELECT value FROM app_settings WHERE name='paper_revision'")['value']
    while vector_store.legacy_cleanup_batch():pass
    results['legacy_cleanup_seconds']=round(time.perf_counter()-started,3)
    assert one("SELECT value FROM app_settings WHERE name='paper_revision'")['value']==before
    print(json.dumps({'phase':'normalized','seconds':results['legacy_cleanup_seconds']}),flush=True)


def reset():
    score._ranking_cache.clear();score._context_cache.clear();vector_search.close()


def measure(name,function):
    timings=[];sample=None
    for _ in range(3):
        reset();started=time.perf_counter();sample=function();timings.append((time.perf_counter()-started)*1000)
    warmed=[]
    for _ in range(5):
        started=time.perf_counter();function();warmed.append((time.perf_counter()-started)*1000)
    results[name]={'cold_ms':round(statistics.median(timings),2),'warm_ms':round(statistics.median(warmed),2),
                   'items':[[p['id'],p['score']] for p in sample['items']]}
    print(json.dumps({'phase':name,**{k:v for k,v in results[name].items() if k!='items'}}),flush=True)


measure('guest_feed',lambda:score.feed(None,'today',0,20))
measure('user_feed',lambda:score.feed(uid,'today',0,20))
clause='p.id IN (SELECT paper_id FROM paper_categories WHERE category_key=?)'
measure('large_browse',lambda:score.browse_page(uid,clause,[category],'score',0,20))

if args.mode=='new':
    from app.llm import vector_rebuild as rebuild,runtime
    target=runtime.configuration()
    target['routes']['embedding']['primary']['model']='isolated-performance-replacement'
    rebuild.set_pending(target)
    name=rebuild._prepare_storage(target)
    live=vector_store.active()['name']
    with vector_store.writer(name) as output,vector_store.reader(live) as source:
        cursor=source.execute('SELECT * FROM paper_vectors ORDER BY paper_id')
        while batch:=cursor.fetchmany(1024):
            vector_store.stage(output,[tuple(row) for row in batch]);output.commit()
    with vector_store.writer(name) as output,connect() as db:
        output.executemany('INSERT INTO profile_vectors VALUES(?,?,?)',
                          [tuple(row) for row in db.execute('SELECT id,content,embedding FROM interest_profile WHERE id IN (SELECT MAX(id) FROM interest_profile GROUP BY user_id)')])
    done_index=threading.Event();index_reads=[];index_errors=[]
    def read_during_index():
        while not done_index.is_set():
            begin=time.perf_counter()
            try:
                assert score.feed(uid,'today',0,20)['items']
                index_reads.append((time.perf_counter()-begin)*1000)
            except Exception as error:index_errors.append(type(error).__name__)
            time.sleep(.1)
    index_thread=threading.Thread(target=read_during_index);index_thread.start()
    started=time.perf_counter();total=rebuild._prepare_index(target['embedding_dim']);last=completed=0
    try:
        while True:
            last,completed,count=rebuild._index_batch(last,completed,total)
            if not count:break
            time.sleep(.02)
        assert vector_store.active()['name']==live
    finally:
        done_index.set();index_thread.join(60)
    assert not index_errors,index_errors
    results['index_seconds']=round(time.perf_counter()-started,3)
    results['index_feed_median_ms']=round(statistics.median(index_reads),2)
    results['index_feed_max_ms']=round(max(index_reads),2)
    started=time.perf_counter();assert rebuild._apply_index(target,completed)
    results['final_switch_seconds']=round(time.perf_counter()-started,3)
    assert vector_store.active()['name']==name
    print(json.dumps({'phase':'rebuilt','index_seconds':results['index_seconds'],'final_switch_seconds':results['final_switch_seconds']}),flush=True)

from app.pipeline.expire import purge_batch
stamp=now()
with connect() as db:
    selected=[r[0] for r in db.execute('SELECT id FROM papers ORDER BY id LIMIT 1000')]
    db.executemany("UPDATE papers SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",[(i,) for i in selected])
    before_users=db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    before_chats=db.execute('SELECT COUNT(*) FROM chat_sessions').fetchone()[0]
reset();score.feed(uid,'today',0,20)
done=threading.Event();latencies=[];batch_times=[]
def reads():
    while not done.is_set():
        started=time.perf_counter();score.feed(uid,'today',0,20);latencies.append((time.perf_counter()-started)*1000)
        time.sleep(.05)
thread=threading.Thread(target=reads);thread.start();started=time.perf_counter();purged=0
try:
    while purged<1000:
        begin=time.perf_counter();count=purge_batch(stamp);batch_times.append((time.perf_counter()-begin)*1000)
        if not count:break
        purged+=count
        if args.mode=='new':vector_store.cleanup_batch()
        time.sleep(.05)
finally:
    results['expiry_seconds']=round(time.perf_counter()-started,3);done.set();thread.join(60)
results['purged']=purged
results['expiry_max_batch_ms']=round(max(batch_times),2)
results['expiry_feed_median_ms']=round(statistics.median(latencies),2) if latencies else None
results['expiry_feed_max_ms']=round(max(latencies),2) if latencies else None
assert one('SELECT COUNT(*) n FROM users')['n']==before_users
assert one('SELECT COUNT(*) n FROM chat_sessions')['n']==before_chats
with connect() as db:assert not db.execute('PRAGMA foreign_key_check').fetchall()
(root/f'{args.mode}.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in results.items() if k not in ('guest_feed','user_feed','large_browse')}),flush=True)
vector_search.close()
if args.mode=='new':vector_store.close()
