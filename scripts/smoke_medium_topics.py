"""Real-provider smoke test against an isolated DATA_DIR, without relabeling papers."""
import asyncio
import json
import time
from app.db import init_db,one
from app.standard_topics import catalog
from app.topic_semantics import ensure_index,semantic_candidates
from app.pipeline.topic_decision import predict_topic
from app.config import settings

async def main():
    init_db(recover=False)
    start=time.perf_counter();count=await ensure_index()
    print(json.dumps({'index_entries':count,'index_seconds':round(time.perf_counter()-start,2)}),flush=True)
    results=[]
    for ident in (1666,5286,5551,3494,3996):
        paper=one('SELECT * FROM papers WHERE id=?',(ident,))
        before=one('SELECT COALESCE(MAX(id),0) id FROM llm_usage')['id'];start=time.perf_counter()
        try:
            options=await semantic_candidates(paper);value=await predict_topic(paper,options)
            chosen=catalog().get(value['standard_key'],{})
            record={'paper_id':ident,'title':paper['title'],'seconds':round(time.perf_counter()-start,2),
                    'calls':value['attempts'],'name':chosen.get('label'),'decision':value}
        except Exception as error:record={'paper_id':ident,'error':str(error),'seconds':round(time.perf_counter()-start,2)}
        used=one("SELECT SUM(input_tokens+output_tokens) tokens FROM llm_usage WHERE id>? AND feature='classify'",(before,))
        record['tokens']=used['tokens'];results.append(record)
        print(json.dumps({k:v for k,v in record.items() if k!='decision'},ensure_ascii=False),flush=True)
    (settings().data_dir/'real-provider-smoke.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    if any('error' in r for r in results):raise RuntimeError('Smoke test failed; see isolated report')

if __name__=='__main__':asyncio.run(main())
