from .local_slot import local_slot
import asyncio
import json
import copy
import uuid
from types import SimpleNamespace
import httpx
from ..config import settings
from .runtime import current_binding, configuration, resolve
from .controls import native_think
from .thinking import clean_answer
from .thinking import AnswerStream
from .controls import discover

_model_lock = asyncio.Lock()


class Ollama:
    async def stream(self, messages, tools, *, json_mode=False):
        binding = current_binding()
        native = copy.deepcopy(messages)
        names = {call['id']:call['function']['name'] for message in native for call in message.get('tool_calls', [])}
        for message in native:
            message['content'] = message.get('content') or ''
            if message.get('reasoning_content'):
                message['thinking'] = message.pop('reasoning_content')
            for call in message.get('tool_calls', []):
                arguments = call['function']['arguments']
                if isinstance(arguments, str):
                    call['function']['arguments'] = json.loads(arguments)
            if message['role']=='tool':
                message['tool_name'] = names.get(message.pop('tool_call_id', ''), '')
        think = native_think(binding)
        caps = binding.get('control_capabilities') or await discover(binding)
        reasoning_only = caps.get('reasoning_only',False)
        if reasoning_only:
            think = True
        if binding['model'].split(':', 1)[0]=='qwen3' and think is False:
            for message in reversed(native):
                if message['role']=='user':
                    message['content'] += '\n/no_think'
                    break
        payload = {'model':binding['model'],'messages':native,'stream':True,'think':think,
                   'options':{'temperature':.3,'num_ctx':16384}}
        reading=binding.get('feature','').startswith('reading_')
        if reading:payload['options']['num_predict']=binding['output_tokens']
        if json_mode:payload['format']=binding.get('json_schema') or 'json'
        if tools:
            payload['tools'] = tools
        index = 0
        visible = AnswerStream(initial_hidden=reasoning_only)
        finished=False
        async with _model_lock, local_slot(), httpx.AsyncClient(timeout=180) as client:
            async with client.stream('POST',binding['base_url'].rstrip('/')+'/api/chat',json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get('error'):
                        raise ValueError(chunk['error'])
                    message = chunk.get('message', {})
                    if 'thinking' in message:
                        visible.hidden = False
                        visible.buffer = ''
                    calls = []
                    for call in message.get('tool_calls', []):
                        function = call['function']
                        calls.append(SimpleNamespace(index=index,id=call.get('id') or uuid.uuid4().hex,
                            function=SimpleNamespace(name=function['name'],arguments=dumps_arguments(function.get('arguments', {})))))
                        index += 1
                    content = visible.feed(message.get('content') or '')
                    if chunk.get('done'):
                        finished=True
                        if reading and chunk.get('done_reason')=='length':
                            raise ValueError('精读输出达到长度上限，请调整思考模式后重试')
                        content += visible.finish()
                    yield SimpleNamespace(content=content,reasoning_content=message.get('thinking'),tool_calls=calls)
                if reading and not finished:
                    raise ValueError('本地模型连接中断，精读尚未完成，请重试')

    async def chat(self, prompt, json_mode=True):
        cfg = settings()
        binding = current_binding()
        model = binding['model'] if binding else cfg.ollama_model
        qwen3 = model.split(':', 1)[0] == 'qwen3'
        think = native_think(binding)
        options={'temperature':.1,'num_ctx':8192}
        if binding and binding.get('feature','').startswith('reading_'):
            from .reading_limits import reading_budget
            options.update(num_ctx=16384,num_predict=reading_budget(binding,'L3' if binding['feature']=='reading_l3' else 'L2')[1])
        if qwen3 and think is False:
            # The installed Qwen3 template opens <think> even with think=False.
            prompt += '\n/no_think'
        async with _model_lock, local_slot(), httpx.AsyncClient(timeout=180) as client:
            response = await client.post((binding['base_url'] if binding else cfg.ollama_base_url).rstrip('/') + '/api/chat', json={
                'model': model, 'messages': [{'role': 'user', 'content': prompt}],
                'stream': False, 'format': (binding.get('json_schema') if binding else None) or ('json' if json_mode else ''),
                'options': options, 'think': think})
            response.raise_for_status()
            content = response.json()['message']['content']
            content = clean_answer(content)
            return json.loads(content) if json_mode else content

    async def embed(self, texts):
        binding = current_binding()
        async with _model_lock, local_slot(), httpx.AsyncClient(timeout=180) as client:
            response = await client.post((binding['base_url'] if binding else settings().ollama_base_url).rstrip('/') + '/api/embed',
                json={'model': binding['model'] if binding else settings().embedding_model, 'input': texts, 'truncate': True})
            response.raise_for_status()
            vectors = response.json()['embeddings']
            from ..db import validate_vectors
            validate_vectors(vectors, len(texts))
            return vectors

    async def status(self):
        try:
            config = configuration()
            binding = current_binding()
            endpoint = binding['base_url'] if binding else next((c['base_url'] for c in config['connections'] if c['kind']=='ollama'), settings().ollama_base_url)
            required = {resolve(r['primary'],config)['model'] for name,r in config['routes'].items() if name in ('classify','quality','brief','embedding') and resolve(r['primary'],config)['kind']=='ollama' and resolve(r['primary'],config)['base_url']==endpoint}
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(endpoint.rstrip('/') + '/api/tags')
                response.raise_for_status()
                names = [m['name'] for m in response.json()['models']]
                def has(model):
                    return model in names or model + ':latest' in names
                return {'available': True, 'models': names,
                        'ready': all(has(model) for model in required)}
        except (httpx.HTTPError, ValueError, KeyError):
            return {'available': False, 'ready': False, 'models': []}


ollama = Ollama()


def dumps_arguments(value):
    return value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
