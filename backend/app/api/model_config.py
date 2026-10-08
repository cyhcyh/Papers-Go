import json
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..auth import admin_user
from ..config import now, settings
from ..db import connect, dumps, one
from ..llm import runtime
from ..llm.secrets import encrypt_configuration, decrypt_configuration
from ..llm.provider import cloud
from ..llm.controls import discover
from ..llm import catalog,vector_rebuild,codex,codex_updates
import uuid

router = APIRouter(prefix='/api/admin/models', dependencies=[Depends(admin_user)])


class Connection(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=r'^[a-zA-Z0-9_-]+$')
    name: str = Field(min_length=1, max_length=100)
    kind: Literal['ollama', 'cloud', 'codex']
    base_url: str = Field(min_length=1, max_length=1000)
    api_key: str = Field('', max_length=4000)
    clear_key: bool = False
    oauth_client_id: str = Field(codex.CLIENT_ID,min_length=1,max_length=200)
    credential_revision: str = Field('',max_length=80)
    codex_auto_update: bool = True
    codex_client_version: str = Field(codex.CLIENT_VERSION,max_length=20,pattern=codex_updates.VERSION_PATTERN)

    @field_validator('base_url')
    @classmethod
    def check_url(cls, value):
        parsed = urlsplit(value.strip())
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('请输入不含账号、查询参数的 HTTP/HTTPS 服务地址')
        return value.strip().rstrip('/')


class Selection(BaseModel):
    connection_id: str
    model: str = Field(min_length=1, max_length=200)
    thinking: Literal['auto','on','off'] = 'auto'
    reasoning_effort: str = Field('auto',max_length=40,pattern=r'^[a-zA-Z0-9_-]+$')

    @field_validator('model')
    @classmethod
    def trim_model(cls, value):
        if not value.strip():
            raise ValueError('请输入模型名称')
        return value.strip()


class Route(BaseModel):
    primary: Selection
    fallback: Selection | None = None


class Configuration(BaseModel):
    connections: list[Connection] = Field(min_length=1, max_length=21)
    routes: dict[str, Route]
    embedding_dim: int = Field(1024, ge=1, le=8192)
    rebuild_vectors: bool = False
    brief_cloud_concurrency: int = Field(4,ge=1,le=8)
    quality_cloud_concurrency: int = Field(2,ge=1,le=8)
    classify_cloud_concurrency: int = Field(4,ge=1,le=16)
    reading_cloud_concurrency: int = Field(2,ge=1,le=8)
    reading_local_concurrency: int = Field(1,ge=1,le=4)
    embedding_cloud_concurrency: int = Field(4,ge=1,le=16)
    embedding_local_concurrency: int = Field(1,ge=1,le=4)


def private_connection(body, previous):
    value = body.model_dump(exclude={'clear_key'})
    old = next((c for c in previous['connections'] if c['id']==body.id), {})
    if old.get('embedding_retained'):
        return dict(old)
    value['api_key'] = '' if body.clear_key or body.kind!='cloud' else body.api_key or (old.get('api_key','') if old.get('kind')==body.kind else '')
    value['credential_revision']=old.get('credential_revision','') if value['api_key']==old.get('api_key','') else body.credential_revision if body.credential_revision and body.credential_revision!=old.get('credential_revision') else uuid.uuid4().hex
    if body.kind=='codex' and body.base_url not in codex.ALLOWED_BASES:
        raise HTTPException(400,'Codex 授权只能用于受支持的 OpenAI 服务地址')
    return value


@router.get('')
def get_configuration():
    return runtime.public_configuration()


@router.put('')
async def save_configuration(body: Configuration):
    pending=vector_rebuild.pending()
    previous = runtime.configuration()
    connections = [private_connection(c, previous) for c in body.connections]
    if sum(not c.get('embedding_retained') for c in connections)>20:
        raise HTTPException(400,'最多可配置 20 个模型连接')
    ids = {c['id'] for c in connections}
    if len(ids)!=len(connections):
        raise HTTPException(400, '连接标识不能重复')
    if set(body.routes)!=set(runtime.FEATURES):
        raise HTTPException(400, '请为每个功能配置模型')
    for name, route in body.routes.items():
        for choice in (route.primary, route.fallback):
            if choice and choice.connection_id not in ids:
                raise HTTPException(400, f'{runtime.FEATURES[name][0]}选择的连接不存在')
            if choice and name!='embedding' and any(c['id']==choice.connection_id and c.get('embedding_retained') for c in connections):
                raise HTTPException(400,'保留的旧向量连接仅供当前向量空间使用，请选择模型连接')
    if body.routes['embedding'].fallback:
        raise HTTPException(400, '向量生成必须统一使用一个模型，不能设置备用模型')
    config = {'connections': connections, 'routes': {key:r.model_dump() for key,r in body.routes.items()}, 'embedding_dim': body.embedding_dim, 'brief_cloud_concurrency':body.brief_cloud_concurrency,
              'quality_cloud_concurrency':body.quality_cloud_concurrency,'classify_cloud_concurrency':body.classify_cloud_concurrency,
              'reading_cloud_concurrency':body.reading_cloud_concurrency,'reading_local_concurrency':body.reading_local_concurrency,
              'embedding_cloud_concurrency':body.embedding_cloud_concurrency,'embedding_local_concurrency':body.embedding_local_concurrency}
    vector_rebuild.remove_unused_retained(config)
    discovered = {}
    for feature, route in config['routes'].items():
        for choice in (route['primary'],route['fallback']):
            if not choice:
                continue
            if feature=='embedding':
                if choice['thinking']!='auto' or choice['reasoning_effort']!='auto':
                    raise HTTPException(400,'向量模型不使用思考与推理强度设置')
                binding=runtime.resolve(choice,config)
                if binding['kind']=='codex':raise HTTPException(400,'Codex 连接不提供 embedding 模型，请选择本地或云端向量模型')
                choice['model']=catalog.canonical_name(binding,choice['model'])
                binding['model']=choice['model']
                info=catalog.model_info(binding,binding['model']) or catalog.embedding_info(binding['model'],binding)
                if info['type']=='chat':raise HTTPException(400,'向量生成需要选择 embedding 模型')
                if info.get('embedding_dimensions') and body.embedding_dim not in info['embedding_dimensions']:
                    raise HTTPException(400,'所选向量模型不支持该维度')
                choice['embedding_metadata']=info
                continue
            binding = runtime.resolve(choice,config)
            choice['model']=catalog.canonical_name(binding,choice['model'])
            binding['model']=choice['model']
            identity = (binding['kind'],binding['base_url'],binding['model'])
            if identity not in discovered:
                discovered[identity] = await discover(binding)
            caps = discovered[identity]
            if caps.get('type')=='embedding':raise HTTPException(400,f'{runtime.FEATURES[feature][0]}需要文本生成模型')
            if choice['thinking'] not in caps['thinking_modes']:
                raise HTTPException(400,f'{runtime.FEATURES[feature][0]}所选模型不支持该思考模式')
            if choice['reasoning_effort']!='auto' and (choice['thinking']=='off' or choice['reasoning_effort'] not in caps['reasoning_efforts']):
                raise HTTPException(400,f'{runtime.FEATURES[feature][0]}所选模型不支持该推理强度')
            choice['control_capabilities'] = caps
    embedding_changed = runtime.embedding_identity(previous)!=runtime.embedding_identity(config)
    if pending and embedding_changed:raise HTTPException(409,'已有向量重建配置，请先完成或取消重建，再切换向量模型；其他功能可继续修改')
    live=config
    if embedding_changed:
        from ..pipeline_control import public_state
        from ..pipeline.read import _tasks
        from ..agent.chat import _active_sessions
        if runtime.active_snapshots or public_state()['busy'] or _tasks or one("SELECT 1 FROM reading_jobs WHERE status='running'") or _active_sessions:
            raise HTTPException(409, '请先停止模型任务、精读和对话，再切换向量模型')
        if settings().pipeline_mode!='inline' and not public_state()['worker_available']:
            raise HTTPException(503,'后台工作进程尚未就绪，请稍后切换向量模型')
        if not body.rebuild_vectors:
            raise HTTPException(409, '切换向量模型或维度需要勾选重建向量')
        from ..pipeline_control import job_enabled,queue_command
        if not job_enabled('build_vectors'):raise HTTPException(409,'请先启用建立向量流水线任务，再安排向量重建')
        try:live=vector_rebuild.with_embedding(config,previous)
        except ValueError as error:raise HTTPException(409,str(error)) from error
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        latest=db.execute("SELECT value FROM app_settings WHERE name='models'").fetchone()
        current=decrypt_configuration(json.loads(latest['value'])) if latest else previous
        if runtime.embedding_identity(current)!=runtime.embedding_identity(previous):
            raise HTTPException(409,'向量配置刚刚完成切换，请刷新页面后再保存')
        record=db.execute("SELECT value FROM app_settings WHERE name='embedding_rebuild'").fetchone()
        if record:
            if embedding_changed:raise HTTPException(409,'已有向量重建配置，请先完成或取消重建')
            value=json.loads(record['value']);value['config']=decrypt_configuration(value['config'])
            try:vector_rebuild.check_connection(config,value,current)
            except ValueError as error:raise HTTPException(409,str(error)) from error
        if embedding_changed:vector_rebuild.set_pending(config,db=db)
        db.execute("INSERT INTO app_settings VALUES('models',?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (dumps(encrypt_configuration(live)), now()))
        live_ids={c['id'] for c in live['connections']}
        for old in previous['connections']:
            if old['id'] not in live_ids:
                db.execute('DELETE FROM codex_auth WHERE connection_id=?',(old['id'],))
                db.execute('DELETE FROM model_catalogs WHERE connection_id=?',(old['id'],))
                db.execute('DELETE FROM model_capability_overrides WHERE connection_id=?',(old['id'],))
                db.execute('DELETE FROM app_settings WHERE name=?',(codex_updates.state_key(old['id']),))
    settings().embedding_dim = live['embedding_dim']
    if embedding_changed:
        try:
            if settings().pipeline_mode=='inline':
                from ..scheduler import start_manual
                start_manual('build_vectors')
            else:queue_command('start','build_vectors')
        except Exception:
            vector_rebuild.update('failed','其他配置已保存，但重建任务尚未排入队列，请继续重建')
            raise
    return {**runtime.public_configuration(live),'vectors_reset':False,'rebuild_queued':embedding_changed,
            'pending_vectors':one('SELECT COUNT(*) n FROM papers' if embedding_changed else 'SELECT COUNT(*) n FROM papers WHERE embedding IS NULL')['n']}



class CapabilityRequest(BaseModel):
    connection: Connection
    model: str = Field(min_length=1,max_length=200)


@router.post('/capabilities')
async def model_capabilities(body: CapabilityRequest):
    return await discover({**private_connection(body.connection,runtime.configuration()),'model':body.model})


@router.post('/connection-test')
async def test_connection(body: Connection):
    connection = private_connection(body, runtime.configuration())
    try:
        if body.kind=='ollama':
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.get(body.base_url+'/api/tags')
                response.raise_for_status()
                models = [model['name'] for model in response.json()['models']]
        elif body.kind=='codex':
            await codex.credentials(body.id)
            return {'ok':True,'models':[],'message':'Codex 授权有效，点击更新可用模型读取目录'}
        else:
            if not connection['api_key']:
                return {'ok':False, 'models':[], 'message':'请填写 API Key'}
            with runtime.bind({**connection, 'model':''}):
                async with cloud.client() as client:
                    # A connection check only lists models; it does not generate text.
                    page = await client.models.list(timeout=8)
                    models = [model.id for model in page.data]
        return {'ok':True, 'models':sorted(set(models)), 'message':'连接成功'}
    except Exception as error:
        status = getattr(error, 'status_code', None) or getattr(getattr(error, 'response', None), 'status_code', None)
        return {'ok':False, 'models':[], 'message':f'连接或模型列表获取失败{f"（HTTP {status}）" if status else ""}，请检查地址和 Key，也可手动填写模型名称。'}


@router.post('/refresh')
async def refresh_catalog(body: Connection):
    previous=runtime.configuration();connection=private_connection(body,previous)
    if connection['kind']=='codex':return await codex_updates.refresh(connection)
    old=catalog.saved(connection)
    try:
        models=await catalog.fetch(connection)
        saved=catalog.store(connection,models)
        return {'ok':True,**saved,'message':f'已更新 {len(models)} 个模型'}
    except Exception as error:
        status=getattr(error,'status_code',None) or getattr(getattr(error,'response',None),'status_code',None)
        message='模型目录更新失败，保留上次结果'+(f'（HTTP {status}）' if status else '')
        if isinstance(error,ValueError) and str(error) in ('请填写 API Key','服务未返回可用模型目录'):message+='：'+str(error)
        if old['updated_at']:
            with connect() as db:db.execute('UPDATE model_catalogs SET error=? WHERE connection_id=?',(message,body.id))
        return {'ok':False,**old,'error':message,'message':message}


@router.post('/codex/login')
async def codex_login(body: Connection):
    connection=private_connection(body,runtime.configuration())
    if body.kind!='codex':raise HTTPException(400,'请选择 Codex 连接')
    try:return await codex.start_login(connection['id'],connection['oauth_client_id'])
    except Exception as error:raise HTTPException(502,'Codex 登录授权请求失败，请检查网络后重试') from error


@router.post('/codex/{connection_id}/poll')
async def codex_poll(connection_id: str):
    try:return await codex.poll_login(connection_id)
    except Exception as error:raise HTTPException(502,'Codex 授权验证失败，请稍后重试') from error


@router.post('/codex/{connection_id}/logout')
def codex_logout(connection_id: str):return codex.logout(connection_id)


@router.post('/embedding-rebuild/resume')
def resume_rebuild():
    if not vector_rebuild.pending():raise HTTPException(404,'没有待完成的向量重建')
    from ..pipeline_control import queue_command,job_enabled,public_state
    if public_state()['busy']:raise HTTPException(409,'当前任务仍在运行')
    if not job_enabled('build_vectors'):raise HTTPException(409,'请先启用建立向量任务')
    vector_rebuild.update('queued')
    try:
        if settings().pipeline_mode=='inline':
            from ..scheduler import start_manual
            start_manual('build_vectors')
        else:queue_command('start','build_vectors')
    except Exception:
        vector_rebuild.update('failed','任务暂未排入队列，请稍后继续重建')
        raise
    return {'ok':True}


@router.post('/embedding-rebuild/cancel')
def cancel_rebuild():
    from ..pipeline_control import public_state
    if public_state()['busy']:raise HTTPException(409,'请先停止正在运行的流水线任务')
    with connect() as db:
        db.execute("DELETE FROM pipeline_commands WHERE action='start' AND name='build_vectors' AND status='queued' AND redo_id IS NULL")
    vector_rebuild.cancel()
    return runtime.public_configuration()


class CapabilityOverride(BaseModel):
    connection:Connection
    model:str=Field(min_length=1,max_length=200)
    reset:bool=False
    thinking_modes:list[Literal['auto','on','off']]=Field(default_factory=lambda:['auto'])
    reasoning_efforts:list[str]=Field(default_factory=list,max_length=12)
    protocol:Literal['openai','ollama','bailian','deepseek','responses']='openai'
    type:Literal['unknown','chat','embedding']='unknown'


@router.post('/capability-override')
def capability_override(body:CapabilityOverride):
    import re
    connection=private_connection(body.connection,runtime.configuration())
    if not catalog.model_info(connection,body.model):raise HTTPException(404,'请先更新该连接的模型目录')
    if any(not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}',v) for v in body.reasoning_efforts):raise HTTPException(400,'推理档位必须使用服务接口中的英文值')
    if connection['kind']=='ollama' and body.protocol!='ollama' or connection['kind']=='codex' and body.protocol!='responses':raise HTTPException(400,'协议与连接类型不一致')
    if connection['kind']=='cloud' and body.protocol not in ('openai','bailian','deepseek'):raise HTTPException(400,'云端 API 连接请选择对应的兼容协议')
    value=None if body.reset else {'thinking_modes':list(dict.fromkeys(['auto']+body.thinking_modes)),'reasoning_efforts':list(dict.fromkeys(body.reasoning_efforts)),'protocol':body.protocol,'type':body.type}
    return catalog.override(connection,body.model,value)
