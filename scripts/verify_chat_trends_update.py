"""Local deployment checks. Never prints tokens, keys or private configuration."""
import asyncio
import json
import sys
import time

import httpx
import jwt

from app.auth import secret
from app.config import settings
from app.db import one, rows
from app.llm import runtime
from app.pipeline.direction_trends import ensure_direction_trend, trend_summary


def admin_client():
    user=one('SELECT id FROM users WHERE is_admin=1 AND disabled=0 ORDER BY id LIMIT 1')
    issued=int(time.time())
    token=jwt.encode({'sub':str(user['id']),'type':'access','iat':issued,'jti':'chat-trends-validation','exp':issued+600},secret(),algorithm='HS256')
    return user['id'],httpx.Client(base_url='http://127.0.0.1:8000/api',headers={'Authorization':'Bearer '+token},trust_env=False,timeout=60)


async def main():
    uid,client=admin_client()
    if sys.argv[1]=='configure':
        loaded=client.get('/admin/models')
        loaded.raise_for_status()
        config=loaded.json()
        classifier=config['routes']['classify']['primary']
        binding=runtime.resolve(classifier)
        if binding['kind']!='cloud' or not binding.get('api_key'):
            raise RuntimeError('The approved cloud classifier connection is not configured')
        config['routes']['trend_report']={'primary':{'connection_id':classifier['connection_id'],'model':classifier['model'],'thinking':'off','reasoning_effort':'auto'},'fallback':None}
        response=client.put('/admin/models',json=config)
        if response.status_code!=200:
            print({'status':response.status_code,'detail':response.json().get('detail')})
            return
        route=response.json()['routes']['trend_report']['primary']
        saved=json.loads(one("SELECT value FROM app_settings WHERE name='models'")['value'])
        print({'trend_model':route['model'],'thinking':route['thinking'],'keys_encrypted':not any(c.get('api_key') for c in saved['connections']) and any(c.get('api_key_encrypted') for c in saved['connections'])})
    elif sys.argv[1]=='refresh':
        for user in (uid,None):
            started=time.monotonic()
            ok=await ensure_direction_trend(user,force=True)
            summary=trend_summary(user)
            print(json.dumps({'audience':'admin' if user else 'guest','generated':ok,'seconds':round(time.monotonic()-started,2),'summary':summary['summary'],'evidence_count':len(summary['papers']),'historical_count':sum(bool(p.get('historical')) for p in summary['papers']),'error':one('SELECT error FROM direction_trends WHERE audience=?',('user:'+str(user) if user else 'guest',))['error']},ensure_ascii=False),flush=True)
    else:
        print({'health':client.get('/health').json(),'historical_cache':rows('SELECT scope_key,length(papers) bytes,error FROM trend_reference_cache'),'trend_route':{k:runtime.selected('trend_report')[k] for k in ('model','thinking','reasoning_effort')}})
    client.close()


asyncio.run(main())
