"""Persisted model connections and per-feature routing, frozen for each running task."""
import copy
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from ..config import settings
from ..db import one
from .controls import capabilities
from ..logs import event
from ..pipeline_control import check_cancelled

FEATURES = {
    'classify': ('主题分类', 'JSON'),
    'quality': ('质量与新颖性评估', 'JSON'),
    'brief': ('中文标题与论文速读', 'JSON'),
    'embedding': ('向量生成（论文、兴趣、全文检索统一）', 'embedding'),
    'chat': ('智能助手', '流式回复与工具调用'),
    'reading_l2': ('论文精读', 'JSON'),
    'reading_l3': ('深度解析', 'JSON 与长文阅读'),
    'interest_init': ('兴趣描述整理', 'JSON'),
    'reflect': ('每周画像反思', 'JSON'),
    'trend_report': ('方向趋势简述', 'JSON'),
    'audit': ('分类抽样评估', 'JSON'),
}
_snapshot = ContextVar('model_snapshot', default=None)
_binding = ContextVar('model_binding', default=None)
active_snapshots = set()


def defaults():
    cfg = settings()
    local = lambda model: {'connection_id': 'local', 'model': model, 'thinking':'off' if 'off' in capabilities({'kind':'ollama','model':model,'base_url':cfg.ollama_base_url})['thinking_modes'] else 'auto', 'reasoning_effort':'auto'}
    cloud = lambda model: {'connection_id': 'cloud', 'model': model, 'thinking':'auto', 'reasoning_effort':'auto'}
    routes = {name: {'primary': cloud(cfg.llm_model_precise), 'fallback': None} for name in FEATURES}
    for name in ('classify', 'quality', 'brief'):
        routes[name]['primary'] = local(cfg.ollama_model)
    routes['trend_report']['primary'] = local(cfg.ollama_model)
    routes['embedding']['primary'] = local(cfg.embedding_model)
    routes['embedding']['primary']['thinking'] = 'auto'
    routes['chat']['primary'] = cloud(cfg.llm_model_chat)
    routes['audit']['primary'] = cloud(cfg.llm_model_fast)
    routes['brief']['fallback'] = cloud(cfg.llm_model_fast)
    for choice in (routes['brief']['primary'],routes['brief']['fallback']):
        connection={'kind':'cloud' if choice['connection_id']=='cloud' else 'ollama','base_url':cfg.llm_base_url if choice['connection_id']=='cloud' else cfg.ollama_base_url}
        if connection['kind']=='cloud' and 'off' in capabilities({**connection,**choice})['thinking_modes']:
            choice['thinking']='off'
    return {'connections': [
        {'id': 'local', 'name': '本地 Ollama', 'kind': 'ollama', 'base_url': cfg.ollama_base_url, 'api_key': ''},
        {'id': 'cloud', 'name': '默认云端', 'kind': 'cloud', 'base_url': cfg.llm_base_url, 'api_key': cfg.llm_api_key},
    ], 'routes': routes, 'embedding_dim': cfg.embedding_dim, 'brief_cloud_concurrency':cfg.brief_cloud_concurrency,
        'quality_cloud_concurrency':cfg.quality_cloud_concurrency,'classify_cloud_concurrency':cfg.classify_cloud_concurrency,
        'reading_cloud_concurrency':cfg.reading_cloud_concurrency,'reading_local_concurrency':cfg.reading_local_concurrency,
        'embedding_cloud_concurrency':cfg.embedding_cloud_concurrency,'embedding_local_concurrency':cfg.embedding_local_concurrency}


def configuration():
    frozen = _snapshot.get()
    if frozen is not None:
        return frozen
    saved = one("SELECT value FROM app_settings WHERE name='models'")
    if saved:
        import json
        from .secrets import decrypt_configuration
        config = decrypt_configuration(json.loads(saved['value']))
        config.setdefault('brief_cloud_concurrency',settings().brief_cloud_concurrency)
        config.setdefault('quality_cloud_concurrency',settings().quality_cloud_concurrency)
        config.setdefault('classify_cloud_concurrency',settings().classify_cloud_concurrency)
        config.setdefault('reading_cloud_concurrency',settings().reading_cloud_concurrency)
        config.setdefault('reading_local_concurrency',settings().reading_local_concurrency)
        config.setdefault('embedding_cloud_concurrency',settings().embedding_cloud_concurrency)
        config.setdefault('embedding_local_concurrency',settings().embedding_local_concurrency)
        for feature, route in config['routes'].items():
            for choice in (route['primary'], route.get('fallback')):
                if choice is not None:
                    connection = next(c for c in config['connections'] if c['id']==choice['connection_id'])
                    mode = 'off' if feature!='embedding' and (feature in ('classify','chat') or connection['kind']=='ollama') else 'auto'
                    caps = capabilities({**connection,**choice})
                    choice.setdefault('thinking',mode if mode in caps['thinking_modes'] else 'auto')
                    choice.setdefault('reasoning_effort','auto')
        return config
    return defaults()


def public_configuration(config=None, *, include_rebuild=True):
    config = copy.deepcopy(config or configuration())
    for connection in config['connections']:
        connection['configured'] = connection['kind'] == 'ollama' or bool(connection.pop('api_key', ''))
        connection.pop('api_key', None)
        from .catalog import saved
        connection['catalog']=saved(connection)
        if connection['kind']=='codex':
            from .codex import status,CLIENT_VERSION
            connection.setdefault('codex_auto_update',True)
            connection.setdefault('codex_client_version',CLIENT_VERSION)
            connection['auth']=status(connection['id'])
            connection['configured']=connection['auth']['logged_in']
    config['features'] = [{'id': key, 'name': name, 'requirement': requirement} for key, (name, requirement) in FEATURES.items()]
    if include_rebuild:
        from .vector_rebuild import state
        config['pending_rebuild']=state()
    return config


@contextmanager
def model_snapshot(config=None, *, replace=False):
    if _snapshot.get() is not None and not replace:
        yield _snapshot.get()
        return
    frozen = copy.deepcopy(config or configuration())
    if not replace:settings().embedding_dim = frozen['embedding_dim']
    marker = object()
    token = _snapshot.set(frozen)
    active_snapshots.add(marker)
    try:
        from .. import prompts
        with prompts.snapshot():
            yield frozen
    finally:
        active_snapshots.discard(marker)
        _snapshot.reset(token)


def model_task(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        with model_snapshot():
            return await function(*args, **kwargs)
    return wrapped


def resolve(selection, config=None):
    config = config or configuration()
    connection = next(c for c in config['connections'] if c['id'] == selection['connection_id'])
    return {**connection, **selection, 'embedding_dim': config['embedding_dim']}


def selected(feature):
    return resolve(configuration()['routes'][feature]['primary'])


def configured(feature):
    binding = selected(feature)
    if binding['kind']=='codex':
        from .codex import status
        return status(binding['id'])['logged_in']
    return binding['kind'] == 'ollama' or bool(binding.get('api_key'))


def vector_dimension():
    return configuration()['embedding_dim']


def embedding_parallelism(binding=None,config=None,*,background=False):
    from .embedding_queue import limits
    config=config or configuration();binding=binding or resolve(config['routes']['embedding']['primary'],config)
    total,batch=limits(binding,config)
    return batch if background else total


def concurrency(feature):
    if selected(feature)['kind']=='ollama':
        return 1
    if feature=='quality':
        return configuration().get('quality_cloud_concurrency',settings().quality_cloud_concurrency)
    return configuration().get('brief_cloud_concurrency',settings().brief_cloud_concurrency) if feature=='brief' else configuration().get('classify_cloud_concurrency',settings().classify_cloud_concurrency)


def current_binding():
    return _binding.get()


@contextmanager
def bind(binding):
    token = _binding.set(binding)
    try:
        yield
    finally:
        _binding.reset(token)


async def complete(feature, messages, json_mode=False, cache_seconds=0, purpose='precise', validate=None, schema=None):
    from .provider import cloud
    from .ollama import ollama
    with model_snapshot():
        route = configuration()['routes'][feature]
        choices = [route['primary']] + ([route['fallback']] if route.get('fallback') else [])
        for index, choice in enumerate(choices):
            check_cancelled()
            binding = resolve(choice)
            binding['feature'] = feature
            if schema:
                binding['json_schema'] = schema
            with bind(binding):
                started = time.perf_counter()
                try:
                    if binding['kind'] == 'ollama':
                        prompt = '\n\n'.join(message['content'] for message in messages)
                        result = await ollama.chat(prompt, json_mode=json_mode)
                    elif binding['kind']=='codex':
                        from .codex import codex
                        result=await codex.complete(messages,json_mode=json_mode)
                    else:
                        result = await cloud.complete(messages, purpose=purpose, json_mode=json_mode, cache_seconds=cache_seconds)
                    check_cancelled()
                    value = validate(result) if validate else result
                    event('model','模型调用完成',job=feature,model=binding['model'],kind_model=binding['kind'],thinking=binding.get('thinking'),reasoning_effort=binding.get('reasoning_effort'),seconds=round(time.perf_counter()-started,2))
                    return value
                except Exception as error:
                    event('model','模型调用失败',level='error',job=feature,model=binding['model'],error=safe_error(error),seconds=round(time.perf_counter()-started,2))
                    if index == len(choices)-1:
                        raise
                    backup = resolve(choices[index+1])
                    if backup['kind']=='cloud' and not backup.get('api_key'):
                        raise


async def embed(texts):
    from .provider import cloud
    from .ollama import ollama
    with model_snapshot(), bind({**selected('embedding'),'feature':'embedding'}):
        started = time.perf_counter()
        binding = current_binding()
        try:
            from .catalog import embedding_info
            from .embedding_queue import permit
            info=binding.get('embedding_metadata') or embedding_info(binding['model'],binding)
            vectors=[]
            for start in range(0,len(texts),info['embedding_batch_size']):
                check_cancelled()
                batch=texts[start:start+info['embedding_batch_size']]
                async with permit(binding,configuration()):
                    vectors.extend(await (ollama.embed(batch) if binding['kind']=='ollama' else cloud.embed(batch)))
                check_cancelled()
            from ..db import validate_vectors
            validate_vectors(vectors, len(texts))
            event('model','向量生成完成',job='embedding',model=binding['model'],items=len(texts),seconds=round(time.perf_counter()-started,2))
            return vectors
        except Exception as error:
            event('model','向量生成失败',level='error',job='embedding',model=binding['model'],error=safe_error(error),seconds=round(time.perf_counter()-started,2))
            raise


async def stream(messages, tools, *, feature='chat', json_mode=False, schema=None):
    from .provider import cloud
    from .ollama import ollama
    with model_snapshot():
        route = configuration()['routes'][feature]
        choices = [route['primary']] + ([route['fallback']] if route.get('fallback') else [])
        for index, choice in enumerate(choices):
            check_cancelled()
            emitted = False
            started = time.perf_counter()
            binding={**resolve(choice), 'feature':feature}
            if feature.startswith('reading_'):
                from .reading_limits import reading_budget, token_upper_bound
                context,output=reading_budget(binding,'L3' if feature=='reading_l3' else 'L2')
                binding.update(output_tokens=output,json_schema=schema)
            with bind(binding):
                try:
                    if feature.startswith('reading_') and sum(token_upper_bound(m.get('content') or '') for m in messages)+output+512>context:
                        raise ValueError('所选模型上下文不足以容纳精读材料，请选择更长上下文的模型')
                    options={'json_mode':True} if json_mode else {}
                    if current_binding()['kind']=='ollama':
                        async for delta in ollama.stream(messages, tools, **options):
                            check_cancelled()
                            emitted = emitted or bool(delta.content or delta.tool_calls or feature!='chat' and getattr(delta,'reasoning_content',None))
                            yield delta
                    elif binding['kind']=='codex':
                        from .codex import codex
                        async for delta in codex.stream(messages,tools,**options):
                            check_cancelled()
                            emitted=emitted or bool(delta.content or delta.tool_calls or feature!='chat' and getattr(delta,'reasoning_content',None))
                            yield delta
                    else:
                        async for delta in cloud.stream(messages, tools, **options):
                            check_cancelled()
                            emitted = emitted or bool(delta.content or delta.tool_calls or feature!='chat' and getattr(delta,'reasoning_content',None))
                            yield delta
                    check_cancelled()
                    event('model','对话响应完成' if feature=='chat' else '精读流式响应完成',job=feature,model=current_binding()['model'],thinking=current_binding().get('thinking'),seconds=round(time.perf_counter()-started,2))
                    return
                except Exception as error:
                    event('model','对话响应失败' if feature=='chat' else '精读流式响应失败',level='error',job=feature,model=current_binding()['model'],error=safe_error(error),seconds=round(time.perf_counter()-started,2))
                    # After any text/tool call, changing model could repeat output or actions.
                    if emitted or index==len(choices)-1:
                        raise
                    backup = resolve(choices[index+1])
                    if backup['kind']=='cloud' and not backup.get('api_key'):
                        raise


def embedding_identity(config):
    binding = resolve(config['routes']['embedding']['primary'], config)
    from .catalog import canonical_name
    return (binding['kind'], binding['base_url'].rstrip('/'), canonical_name(binding,binding['model']), config['embedding_dim'])


def safe_error(error):
    """Model providers may echo credentials in an error response."""
    status = getattr(error,'status_code',None) or getattr(getattr(error,'response',None),'status_code',None)
    if status:
        return f'服务请求失败（HTTP {status}），请检查地址、认证和模型配置'
    message = str(error)
    try:
        for connection in configuration()['connections']:
            key = connection.get('api_key')
            if key:
                message = message.replace(key,'[已隐藏]')
    except Exception:
        pass
    return message
