"""Detect and validate Codex catalog compatibility before adopting a new version."""
import asyncio
import json
import re
import httpx
from ..config import now
from .. import prompts
from ..db import connect,one,dumps
from . import codex,catalog

STABLE_RELEASE_URL='https://registry.npmjs.org/@openai/codex/latest'
VERSION_PATTERN=r'^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$'


def state_key(connection_id):return 'codex_compatibility:'+connection_id


def state(connection):
    record=one('SELECT value FROM app_settings WHERE name=?',(state_key(connection['id']),))
    value=json.loads(record['value']) if record else {}
    if value.get('signature')!=catalog.signature(connection):return {}
    return {k:v for k,v in value.items() if k!='signature'}


def store_state(db,connection,value,signature):
    db.execute('INSERT INTO app_settings(name,value,updated_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
               (state_key(connection['id']),dumps({**value,'signature':signature}),now()))


def version_tuple(value):
    if not isinstance(value,str) or not re.fullmatch(VERSION_PATTERN,value):raise ValueError('无效的 Codex 兼容版本')
    return tuple(int(v) for v in value.split('.'))


async def latest_release():
    # Read the official package's stable dist-tag; never install or execute it.
    async with httpx.AsyncClient(timeout=8) as client:
        response=await client.get(STABLE_RELEASE_URL)
        response.raise_for_status();data=response.json()
    version=data.get('version')
    if data.get('name')!='@openai/codex':raise ValueError('无效的官方版本响应')
    version_tuple(version)
    return version


async def validate(connection,models,old_models):
    from .runtime import bind
    old_ids={m['id'] for m in old_models}
    model=min(models,key=lambda m:(m['id'] in old_ids,m.get('priority',100),m['id']))
    efforts=model.get('reasoning_efforts',[])
    effort=next((e for e in ('none','minimal','low') if e in efforts),'auto')
    binding={**connection,'model':model['id'],'feature':'connection_test',
             'thinking':'auto','reasoning_effort':effort,'control_capabilities':model}
    parts=[]
    # Exercise the same SSE parser as research chat. No application tools are supplied.
    async with asyncio.timeout(35):
        with bind(binding):
            async for delta in codex.codex.stream([{'role':'system','content':prompts.get('codex_connection_test')},
                                                 {'role':'user','content':'OK'}],[]):
                if delta.content:parts.append(delta.content)
                if sum(map(len,parts))>1000:raise ValueError('连接验证回复过长')
    if not ''.join(parts).strip():raise ValueError('连接验证没有返回正文')
    return model['id']


def safe_error(error):
    status=getattr(error,'status_code',None) or getattr(getattr(error,'response',None),'status_code',None)
    if status:return f'（HTTP {status}）'
    if isinstance(error,TimeoutError) or isinstance(error,httpx.TimeoutException):return '（超时）'
    return ''


async def refresh(connection):
    old=catalog.saved(connection);previous=state(connection);revision=catalog.signature(connection)
    active=previous.get('active_version')
    configured=connection.get('codex_client_version') or codex.CLIENT_VERSION
    current=active or configured
    candidate=configured if not connection.get('codex_auto_update',True) else current
    info={**previous,'warning':None,'error':None}
    if connection.get('codex_auto_update',True):
        try:
            latest=await latest_release()
            info.update(latest_version=latest,checked_at=now())
            candidate=max((current,latest),key=version_tuple)
        except (httpx.HTTPError,ValueError,TypeError):
            info['warning']=f'未能查询官方稳定版本，继续使用兼容版本 {current}'
    stage='模型目录获取'
    try:
        version_tuple(candidate)
        models=await catalog.fetch(connection,client_version=candidate)
        validation={}
        if active!=candidate:
            stage='流式兼容验证'
            model=await validate(connection,models,old['models'])
            validation={'validation_model':model,'validated_at':now()}
        # Do not attach a response from an old login to a newly authorized account.
        if catalog.signature(connection)!=revision:raise ValueError('Codex 登录状态已变化')
        success_info={**info,**validation,'active_version':candidate,'error':None}
        stage='目录保存'
        saved=catalog.store(connection,models,compatibility=success_info,expected_signature=revision)
        message=f'已更新 {len(models)} 个模型 · 兼容版本 {candidate}'
        if info.get('warning'):message+='；'+info['warning']
        return {'ok':True,**saved,'message':message}
    except Exception as error:
        message=f'{stage}失败{safe_error(error)}，保留原兼容版本和模型目录'
        info['error']=message
        if catalog.signature(connection)==revision:
            with connect() as db:
                store_state(db,connection,info,revision)
                db.execute('UPDATE model_catalogs SET error=? WHERE connection_id=?',(message,connection['id']))
        return {'ok':False,**old,'compatibility':info,'error':message,'message':message}
