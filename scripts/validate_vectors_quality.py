"""Small real-model audit; all vectors and scores are written to a temporary database."""
import argparse
import asyncio
import json
import math
from pathlib import Path
import statistics
import tempfile
import time
from app.config import settings,now,today
from app.db import init_db,connect,rows
from app.llm import runtime
from app.pipeline.embed import score_paper
from app.pipeline.score import cosine
from validate_topic_quality import DEV_IDS

QUERIES=[('图着色和图的重着色',[5273,5571]),('Hopf 代数与 Magnus 展开的经典极限',[19]),
         ('球面上的正权数值求积公式与节点数量界',[38]),('利用图像消除机器翻译的歧义',[369]),
         ('从放射影像报告中抽取实体和结构化标签',[32]),('长音频语音识别的专业术语修正',[133]),
         ('高斯过程的大规模精确推断',[164]),('通过提示注入降低语言模型的任务效果',[180]),
         ('未知机器人形态的逆运动学',[172]),('用户端可定制的推荐系统',[367])]


async def evaluate(args):
    sample=json.loads(Path(args.sample).read_text());by={p['id']:p for p in sample['papers']}
    config=runtime.configuration();original=settings().data_dir
    with tempfile.TemporaryDirectory(prefix='vectors-quality-') as directory:
        settings().data_dir=Path(directory);settings().embedding_dim=config['embedding_dim'];init_db(recover=False)
        with runtime.model_snapshot(config):
            papers=[by[i] for i in sorted(set(DEV_IDS+[5273,5571])) if i in by]
            vectors=[];times=[]
            for start in range(0,len(papers),16):
                before=time.perf_counter();batch=papers[start:start+16]
                vectors.extend(await runtime.embed([p['title']+'\n'+p['abstract'] for p in batch]))
                times.append(time.perf_counter()-before)
                print(json.dumps({'stage':'vectors','completed':len(vectors),'total':len(papers)}),flush=True)
            query_vectors=await runtime.embed([q for q,_ in QUERIES]);queries=[]
            for (query,expected),vector in zip(QUERIES,query_vectors):
                ranked=sorted([(cosine(vector,v),p['id'],p['title']) for p,v in zip(papers,vectors)],reverse=True)
                rank=next((i+1 for i,item in enumerate(ranked) if item[1] in expected),None)
                queries.append({'query':query,'expected':expected,'rank':rank,'top5':[{'id':ident,'title':title,'cosine':round(score,4)} for score,ident,title in ranked[:5]]})
            norms=[math.sqrt(sum(x*x for x in v)) for v in vectors]
            vector_summary={'model':runtime.selected('embedding')['model'],'papers':len(papers),'queries':len(queries),
                            'top1_hits':sum(q['rank']==1 for q in queries),'top3_hits':sum(q['rank']<=3 for q in queries if q['rank']),
                            'finite':all(all(math.isfinite(x) for x in v) for v in vectors),'dimensions':sorted({len(v) for v in vectors}),
                            'norm_range':[round(min(norms),4),round(max(norms),4)],'batch_seconds':[round(t,2) for t in times]}
            quality=[]
            for ident in [19,38,78,109,130,165,180,362]:
                p=by[ident].copy();p['pdf_url']=None
                # Audit the existing abstract/cached-text input without starting bulk PDF downloads.
                with connect() as db:
                    db.execute('INSERT INTO papers(id,title,abstract,primary_category,venue_rank,hf_upvotes,github_stars,fulltext,created_at,ingested_date) VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (p['id'],p['title'],p['abstract'],p['primary_category'],p['venue_rank'],p['hf_upvotes'],p['github_stars'],p['fulltext'],now(),today()))
                before=time.perf_counter()
                item={'id':ident,'title':p['title'],'abstract':p['abstract'],'cached_fulltext':bool(p['fulltext'])}
                try:
                    await score_paper(p)
                    item.update(rows('SELECT skeleton,base_quality_score,quality_score FROM papers WHERE id=?',(ident,))[0]);item['skeleton']=json.loads(item['skeleton']);item['ok']=True
                except Exception as error:item.update(ok=False,error=runtime.safe_error(error)[:300])
                item['seconds']=round(time.perf_counter()-before,3);quality.append(item)
                print(json.dumps({'stage':'quality','completed':len(quality),'total':8,'seconds':item['seconds']}),flush=True)
            report={'vector_summary':vector_summary,'queries':queries,'quality_model':runtime.selected('quality')['model'],
                    'quality_thinking':runtime.selected('quality').get('thinking'),'quality':quality}
            Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(vector_summary),flush=True)
    settings().data_dir=original


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--sample',required=True);p.add_argument('--output',required=True);asyncio.run(evaluate(p.parse_args()))
