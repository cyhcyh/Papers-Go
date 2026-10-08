"""Persisted per-connection catalogs; capability provenance is explicit."""
import asyncio
import json
from urllib.parse import urlparse
import httpx
from ..db import connect, one, dumps
from ..config import now
from .controls import capabilities, discover

SCHEMA = '''CREATE TABLE IF NOT EXISTS model_catalogs (
 connection_id TEXT PRIMARY KEY, signature TEXT NOT NULL, models TEXT NOT NULL,
 updated_at TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS model_capability_overrides (connection_id TEXT NOT NULL,signature TEXT NOT NULL,model TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(connection_id,model));
'''


def signature(connection):
    revision=connection.get('credential_revision','')
    if connection['kind']=='codex':
        row=one('SELECT generation FROM codex_auth WHERE connection_id=?',(connection['id'],))
        revision=row['generation'] if row else ''
    return dumps([connection['kind'], connection['base_url'].rstrip('/'), revision])


def saved(connection):
    row = one('SELECT * FROM model_catalogs WHERE connection_id=?', (connection['id'],))
    extra={}
    if connection['kind']=='codex':
        from .codex_updates import state
        extra['compatibility']=state(connection)
    if row and row['signature']==signature(connection):
        return {'models':apply_overrides(connection,json.loads(row['models'])), 'updated_at':row['updated_at'], 'error':row['error'],**extra}
    return {'models':[], 'updated_at':None, 'error':None,**extra}


def embedding_info(model, connection):
    name=model.lower().rsplit('/',1)[-1]
    host=(urlparse(connection['base_url']).hostname or '').lower()
    info={'type':'unknown', 'embedding_dimensions':[], 'embedding_batch_size':32, 'dimensions_parameter':False}
    if 'embedding' in name or name.startswith(('bge-m3','nomic-embed','mxbai-embed','all-minilm')):
        info['type']='embedding'
    if ('aliyuncs.com' in host) and name.startswith(('text-embedding-', 'qwen3.7-text-embedding')):
        info.update(type='embedding', dimensions_parameter=name not in ('text-embedding-v1','text-embedding-v2'), capability_source='official')
        if name=='text-embedding-v4':info.update(embedding_dimensions=[64,128,256,512,768,1024,1536,2048],embedding_batch_size=10)
        elif name=='text-embedding-v3':info.update(embedding_dimensions=[64,128,256,512,768,1024],embedding_batch_size=10)
        elif name in ('text-embedding-v1','text-embedding-v2'):info.update(embedding_dimensions=[1536],embedding_batch_size=25)
        elif name=='qwen3.7-text-embedding':info.update(embedding_dimensions=[256,512,768,1024,1536,2048,2560],embedding_batch_size=20)
        elif name=='qwen3.7-text-embedding-flash':info.update(embedding_dimensions=[256,512,768,1024],embedding_batch_size=20)
    elif host=='api.openai.com' and name.startswith('text-embedding-3-'):
        info.update(type='embedding', dimensions_parameter=True, capability_source='official')
    return info


def metadata(entry, connection):
    ident=entry.get('id') or entry.get('slug') or entry.get('name')
    if not isinstance(ident,str) or not ident or entry.get('visibility') in ('hide','none') or entry.get('show_in_picker') is False:return None
    base={**capabilities({**connection,'model':ident}),**embedding_info(ident,connection)}
    base.setdefault('capability_source','official' if base.get('protocol') and base['thinking_modes']!=['auto'] else 'unknown')
    fields=entry.get('capabilities') or {}
    efforts=entry.get('supported_reasoning_levels') or entry.get('supported_reasoning_efforts') or entry.get('supportedReasoningEfforts') or []
    if not efforts and isinstance(fields.get('effort'),dict):
        efforts=[k for k,v in fields['effort'].items() if isinstance(v,dict) and v.get('supported')]
    levels=[v if isinstance(v,str) else v.get('reasoning_effort',v.get('reasoningEffort',v.get('effort'))) for v in efforts if isinstance(v,(str,dict))]
    levels=[v for v in levels if isinstance(v,str) and v!='auto']
    if levels:
        base.update(reasoning_efforts=list(dict.fromkeys(levels)),thinking_modes=['auto','on']+(['off'] if 'none' in levels else []),capability_source='api')
        base['protocol']=base.get('protocol') or 'responses' if connection['kind']=='codex' else base.get('protocol') or 'openai'
    thinking=entry.get('thinking') or fields.get('thinking')
    if isinstance(thinking,dict) and isinstance(thinking.get('values'),list):
        values=thinking['values'];named=[v for v in values if isinstance(v,str)]
        base.update(thinking_modes=['auto']+(['on'] if True in values or named else [])+(['off'] if False in values else []),reasoning_efforts=named,capability_source='api')
    elif isinstance(thinking,dict) and 'supported' in thinking:
        base.update(thinking_modes=['auto','on']+(['off'] if thinking.get('can_disable') or thinking.get('toggleable') else []) if thinking['supported'] else ['auto','off'],capability_source='api')
    model_type=entry.get('model_type') or entry.get('type')
    if model_type in ('embedding','chat','text-generation'):base['type']='embedding' if model_type=='embedding' else 'chat'
    if connection['kind']=='codex':base.update(type='chat',protocol='responses')
    if base['type']=='embedding':base.update(thinking_modes=['auto'],reasoning_efforts=[],note='向量模型不使用思考与推理强度设置。')
    elif base['capability_source']=='api':
        base['note']='该模型不支持关闭思考。' if 'off' not in base['thinking_modes'] else ''
    return {'id':ident,'name':entry.get('display_name') or entry.get('displayName') or ident,**base,
            'context_window':entry.get('context_window') or entry.get('max_input_tokens'),
            'priority':entry.get('priority') if isinstance(entry.get('priority'),int) else 100,
            'default_reasoning_effort':entry.get('default_reasoning_level') or entry.get('default_reasoning_effort') or entry.get('defaultReasoningEffort')}


async def fetch(connection, *, client_version=None):
    if connection['kind']=='codex':
        from .codex import credentials, request_headers, CLIENT_VERSION
        auth=await credentials(connection['id'])
        async with httpx.AsyncClient(timeout=20) as client:
            response=await client.get(connection['base_url']+'/models',params={'client_version':client_version or connection.get('codex_client_version') or CLIENT_VERSION},headers={**request_headers(auth),'Accept':'application/json'})
            response.raise_for_status();data=response.json()
    elif connection['kind']=='ollama':
        async with httpx.AsyncClient(timeout=10) as client:
            response=await client.get(connection['base_url']+'/api/tags');response.raise_for_status();data=response.json()
    else:
        if not connection.get('api_key'):raise ValueError('请填写 API Key')
        # Retain SDK model listing for custom compatible clients, including pagination.
        from .provider import cloud
        from .runtime import bind
        with bind({**connection,'model':''}):
            async with cloud.client() as client:
                page=await client.models.list(timeout=15)
                entries=[]
                while True:
                    entries.extend(m.model_dump() for m in page.data)
                    if not getattr(page,'has_next_page',lambda:False)():break
                    page=await page.get_next_page()
        data={'data':entries}
    entries=data.get('models',data.get('data',[]))
    models=[value for e in entries if isinstance(e,dict) and (value:=metadata(e,connection))]
    if connection['kind']=='ollama':
        semaphore=asyncio.Semaphore(4)
        async def enrich(item):
            async with semaphore:
                caps=await discover({**{k:v for k,v in connection.items() if k!='id'},'model':item['id']})
                item.update(caps)
                item['capability_source']=caps.get('capability_source',item['capability_source'])
        await asyncio.gather(*(enrich(item) for item in models))
    if not models:raise ValueError('服务未返回可用模型目录')
    return sorted({m['id']:m for m in models}.values(),key=lambda m:m['id'])


def store(connection, models, *, compatibility=None, expected_signature=None):
    stamp=now();revision=signature(connection)
    if expected_signature is not None and revision!=expected_signature:raise ValueError('Codex 登录状态已变化')
    with connect() as db:
        db.execute('INSERT INTO model_catalogs VALUES(?,?,?,?,NULL) ON CONFLICT(connection_id) DO UPDATE SET signature=excluded.signature,models=excluded.models,updated_at=excluded.updated_at,error=NULL',
                   (connection['id'],revision,dumps(models),stamp))
        if compatibility is not None:
            from .codex_updates import store_state
            store_state(db,connection,compatibility,revision)
    extra={'compatibility':compatibility} if compatibility is not None else {}
    return {'models':apply_overrides(connection,models),'updated_at':stamp,'error':None,**extra}


def canonical_name(connection, model):
    if connection['kind']=='ollama' and ':' not in model.rsplit('/',1)[-1]:return model+':latest'
    return model


def model_info(connection, model):
    return next((item for item in saved(connection)['models'] if canonical_name(connection,item['id'])==canonical_name(connection,model)),None)


def apply_overrides(connection,models):
    with connect() as db:overrides={r['model']:dict(r) for r in db.execute('SELECT * FROM model_capability_overrides WHERE connection_id=?',(connection['id'],))}
    revision=signature(connection)
    return [{**item,**json.loads(row['value']),'capability_source':'manual','note':'管理员根据服务规格补充的能力。'} if (row:=overrides.get(item['id'])) and row['signature']==revision else item for item in models]


def override(connection,model,value):
    with connect() as db:
        if value is None:db.execute('DELETE FROM model_capability_overrides WHERE connection_id=? AND model=?',(connection['id'],model))
        else:db.execute('INSERT OR REPLACE INTO model_capability_overrides VALUES(?,?,?,?)',(connection['id'],signature(connection),model,dumps(value)))
    return saved(connection)
