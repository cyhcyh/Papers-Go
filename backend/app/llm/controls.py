"""Provider-specific thinking controls, without exposing connection credentials."""
from urllib.parse import urlparse
import httpx


def capabilities(binding):
    name = binding['model'].lower().rsplit('/', 1)[-1]
    host = (urlparse(binding['base_url']).hostname or '').lower()
    modes, levels, protocol = ['auto'], [], None
    if binding.get('kind','cloud') == 'ollama':
        protocol = 'ollama'
        if name.startswith('gpt-oss'):
            modes, levels = ['auto', 'on'], ['low', 'medium', 'high']
        elif name.startswith(('qwen3', 'deepseek-r1')):
            modes = ['auto', 'on', 'off']
    elif host.endswith('.maas.aliyuncs.com') or host == 'dashscope.aliyuncs.com':
        protocol = 'bailian'
        if name.startswith(('deepseek-v4', 'deepseek-v3.1', 'deepseek-v3.2')):
            modes = ['auto', 'on', 'off']
            if name.startswith('deepseek-v4'):
                levels = ['high', 'max']
                if any(part in name for part in ('v4.1-flash', 'flash-0731', 'pro-0813')):
                    levels.insert(0, 'low')
        elif name.startswith(('deepseek-r1', 'qwq')) or 'thinking' in name:
            modes = ['auto', 'on']
        elif 'instruct' in name or name in ('deepseek-v3', 'qwen-max'):
            modes = ['auto', 'off']
        elif name.startswith(('qwen-plus', 'qwen-flash', 'qwen-turbo', 'qwen3')):
            modes = ['auto', 'on', 'off']
    elif host == 'api.deepseek.com':
        protocol = 'deepseek'
        if name.startswith(('deepseek-v4', 'deepseek-flash', 'deepseek-pro', 'deepseek-chat')):
            modes, levels = ['auto', 'on', 'off'], ['low', 'high', 'max']
        elif name == 'deepseek-reasoner':
            modes = ['auto', 'on']
    return {'thinking_modes': modes, 'reasoning_efforts': levels, 'protocol': protocol,
            'note': '该模型的思考控制尚未确认，使用模型默认行为。' if modes == ['auto'] else
                    '该模型不支持关闭思考。' if 'off' not in modes else
                    '该模型不支持开启思考。' if 'on' not in modes else ''}


async def discover(binding):
    if binding.get('id'):
        from .catalog import model_info
        cached=model_info(binding,binding['model'])
        if cached:return cached
    result = capabilities(binding)
    result['capability_source']='official' if result['thinking_modes']!=['auto'] else 'unknown'
    if binding['kind']=='codex':
        result.update(protocol='responses',note='请先更新此连接的模型目录，以读取模型支持的推理档位。')
    if binding['kind'] == 'ollama':
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.post(binding['base_url'].rstrip('/')+'/api/show', json={'model':binding['model']})
                response.raise_for_status()
                data = response.json()
                info = data.get('thinking')
                finetune = str(data.get('model_info',{}).get('general.finetune','')).lower()
                if finetune=='thinking':
                    result['thinking_modes'] = ['auto','on']
                    result['reasoning_only'] = True
                    result['note'] = '该权重为思考专用版，思考过程会隐藏，不能关闭思考。'
                elif finetune=='instruct':
                    result['thinking_modes'] = ['auto','off']
                    result['note'] = '该权重为非思考版。'
            if isinstance(info, dict) and isinstance(info.get('values'), list):
                values = info['values']
                levels = [v for v in values if isinstance(v,str) and v not in ('auto','')]
                result['thinking_modes'] = ['auto'] + (['on'] if True in values or levels else []) + (['off'] if False in values else [])
                result['reasoning_efforts'] = levels
                result['capability_source']='api'
                result['note'] = '该模型不支持关闭思考。' if 'off' not in result['thinking_modes'] else '该模型不支持开启思考。' if 'on' not in result['thinking_modes'] else ''
        except (httpx.HTTPError, ValueError):
            pass
    return result


def request_options(binding):
    if not binding:
        return {}
    caps = binding.get('control_capabilities') or capabilities(binding)
    mode = binding.get('thinking', 'off' if binding.get('feature')=='classify' else 'auto')
    effort = binding.get('reasoning_effort', 'auto')
    options = {}
    if mode != 'auto' and mode in caps['thinking_modes']:
        if caps['protocol'] == 'bailian' and 'on' in caps['thinking_modes'] and 'off' in caps['thinking_modes']:
            options['extra_body'] = {'enable_thinking':mode=='on'}
        elif caps['protocol'] == 'deepseek' and 'off' in caps['thinking_modes']:
            options['extra_body'] = {'thinking':{'type':'enabled' if mode=='on' else 'disabled'}}
        elif caps['protocol'] == 'openai' and mode=='off' and 'none' in caps['reasoning_efforts']:
            options['reasoning_effort']='none'
        elif caps['protocol'] == 'ollama':
            options['extra_body'] = {'think':mode=='on'}
    if mode=='on' and effort=='auto' and caps['protocol']=='openai':
        effort=next((v for v in caps['reasoning_efforts'] if v!='none'),'auto')
    if mode != 'off' and effort in caps['reasoning_efforts']:
        options['reasoning_effort'] = effort
        if caps['protocol'] == 'ollama':
            options = {'extra_body':{'think':effort},'reasoning_effort':effort}
    return options


def native_think(binding):
    mode = binding.get('thinking', 'off') if binding else 'off'
    effort = binding.get('reasoning_effort','auto') if binding else 'auto'
    if mode=='off':
        return False
    if effort!='auto':
        return effort
    return True if mode=='on' else None
