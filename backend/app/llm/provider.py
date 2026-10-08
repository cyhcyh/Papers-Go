import hashlib
import json
import re
import time
from openai import AsyncOpenAI
from ..config import settings, now
from ..db import one, execute, dumps
from .runtime import current_binding
from .controls import request_options
from .thinking import clean_answer


class LLMUnavailable(RuntimeError):
    pass


def completion_options(binding, model, endpoint):
    options = request_options({**binding,'model':model,'base_url':endpoint}) if binding else {}
    if binding and binding.get('feature')=='brief':
        options['max_tokens']=2048 if binding.get('thinking')=='off' else 16384
    if binding and binding.get('feature')=='quality':
        options['max_tokens']=2048 if binding.get('thinking')=='off' else 16384
    if binding and binding.get('feature')=='classify':
        thinking = binding.get('thinking','off')
        options['max_tokens'] = 4096 if thinking!='off' else 512
    if binding and binding.get('feature','').startswith('reading_'):
        from .reading_limits import reading_budget
        options['max_tokens']=reading_budget(binding,'L3' if binding['feature']=='reading_l3' else 'L2')[1]
    return options


def parse_json_answer(content):
    # Accept one surrounding JSON fence; never extract snippets or repair invalid JSON.
    fenced = re.fullmatch(r'```(?:json)?[ \t]*\r?\n(.*?)\r?\n```', content, re.I | re.S)
    return json.loads(fenced.group(1) if fenced else content)


class CloudLLM:
    """Only this module speaks the OpenAI-compatible protocol."""
    def client(self):
        cfg = settings()
        binding = current_binding()
        key = ('ollama' if binding['kind']=='ollama' else binding.get('api_key')) if binding else cfg.llm_api_key
        if not key:
            raise LLMUnavailable('请在后台模型配置中填写所选云端连接的 API Key')
        base_url = binding['base_url'].rstrip('/') + ('/v1' if binding['kind']=='ollama' else '') if binding else cfg.llm_base_url
        reading=binding and binding.get('feature','').startswith('reading_')
        return AsyncOpenAI(base_url=base_url, api_key=key,
                           timeout=cfg.llm_timeout, max_retries=0 if reading else 2)

    def model(self, purpose):
        return current_binding()['model'] if current_binding() else getattr(settings(), 'llm_model_' + purpose)

    def record_usage(self, model, usage):
        binding=current_binding()
        if not binding or binding['kind']=='cloud':
            execute('INSERT INTO llm_usage(model,input_tokens,output_tokens,created_at,feature,connection_id,usage_known) VALUES(?,?,?,?,?,?,?)',
                    (model, getattr(usage,'prompt_tokens',getattr(usage,'total_tokens',0)) or 0, getattr(usage,'completion_tokens',0) or 0, now(),
                     binding.get('feature') if binding else None,binding.get('id') if binding else None,int(usage is not None)))

    async def complete(self, messages, purpose='precise', json_mode=False, cache_seconds=0):
        model = self.model(purpose)
        endpoint = current_binding()['base_url'] if current_binding() else settings().llm_base_url
        options = completion_options(current_binding(), model, endpoint)
        schema = (current_binding() or {}).get('json_schema')
        if json_mode and schema:
            # Compatible providers need not support response_format=json_schema.
            # Send the contract as instructions and still validate locally.
            messages = [*messages, {'role':'user','content':'程序要求的输出 JSON Schema（仅输出符合该结构的对象）：\n'+dumps(schema)}]
        key = hashlib.sha256(dumps([endpoint, model, messages, json_mode, options]).encode()).hexdigest()
        if cache_seconds:
            hit = one('SELECT content FROM llm_cache WHERE key=? AND expires_at>?', (key, int(time.time())))
            if hit:
                return json.loads(hit['content']) if json_mode else hit['content']
        kwargs = {'model': model, 'messages': messages, 'temperature': .3, **options}
        if json_mode:
            kwargs['response_format'] = {'type': 'json_object'}
        async with self.client() as client:
            result = await client.chat.completions.create(**kwargs)
        self.record_usage(model, result.usage)
        if result.choices[0].finish_reason == 'length':
            raise ValueError('模型输出达到长度上限，请重试或检查模型配置')
        content = clean_answer(result.choices[0].message.content or '')
        parsed = parse_json_answer(content) if json_mode else content
        if cache_seconds:
            if json_mode:content=dumps(parsed)
            execute('INSERT OR REPLACE INTO llm_cache VALUES(?,?,?)', (key, content, int(time.time()) + cache_seconds))
            execute('DELETE FROM llm_cache WHERE expires_at<?', (int(time.time()),))
        return parsed

    async def stream(self, messages, tools, *, json_mode=False):
        model = self.model('chat')
        binding=current_binding()
        reading=binding and binding.get('feature','').startswith('reading_')
        options=request_options(binding)
        if reading:options['max_tokens']=binding['output_tokens']
        if json_mode:options['response_format']={'type':'json_object'}
        if tools:options['tools']=tools
        async with self.client() as client:
            stream = await client.chat.completions.create(model=model, messages=messages,
                       stream=True, stream_options={'include_usage': True}, temperature=.3,
                       **options)
            usage=None;finished=False;truncated=False
            try:
                async for chunk in stream:
                    if chunk.usage:usage=chunk.usage
                    if chunk.choices:
                        choice=chunk.choices[0]
                        truncated=truncated or choice.finish_reason=='length'
                        finished=finished or choice.finish_reason is not None
                        yield choice.delta
                if reading and truncated:
                    raise ValueError('精读输出达到长度上限，未完成的内容已保留；可调整思考模式后重试')
                if reading and not finished:
                    raise ValueError('模型连接中断，精读尚未完成，请重试')
            finally:
                self.record_usage(model,usage)

    async def embed(self, texts):
        binding=current_binding()
        from .catalog import embedding_info
        info=binding.get('embedding_metadata') or embedding_info(binding['model'],binding)
        options={'dimensions':binding['embedding_dim']} if info.get('dimensions_parameter') else {}
        async with self.client() as client:
            result = await client.embeddings.create(model=self.model('precise'), input=texts,encoding_format='float',**options)
        vectors = [item.embedding for item in sorted(result.data, key=lambda item:item.index)]
        from ..db import validate_vectors
        validate_vectors(vectors, len(texts))
        self.record_usage(self.model('precise'), result.usage)
        return vectors


cloud = CloudLLM()
