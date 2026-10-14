import asyncio
import json
from types import SimpleNamespace as Obj

import pytest
from app.llm import provider
from app.llm.runtime import bind


def packet(content=None,*,reasoning=None,finish=None,usage=None):
    return Obj(choices=[] if usage else [Obj(delta=Obj(content=content,reasoning_content=reasoning),finish_reason=finish)],usage=usage)


def setup(monkeypatch,chunks,*,feature='trend_report',response=None):
    calls=[];recorded=[]
    class Client:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def create(self,**kwargs):
            calls.append(kwargs)
            if response is not None:return response
            async def stream():
                for chunk in chunks:
                    await asyncio.sleep(0)
                    if isinstance(chunk,Exception):raise chunk
                    yield chunk
            return stream()
    client=Client();client.chat=Obj(completions=client)
    monkeypatch.setattr(provider.cloud,'client',lambda:client)
    monkeypatch.setattr(provider.cloud,'record_usage',lambda model,usage:recorded.append((model,usage)))
    binding={'kind':'cloud','feature':feature,'model':'test-model','base_url':'https://example.invalid/v1','thinking':'off',
             'control_capabilities':{'protocol':'bailian','thinking_modes':['auto'],'reasoning_efforts':[]}}
    return binding,calls,recorded


@pytest.mark.asyncio
async def test_trend_collects_complete_json_and_records_usage_once(monkeypatch):
    value={'summary':'公式为 $n^2$。'};text=json.dumps(value,ensure_ascii=False);usage=Obj(prompt_tokens=11,completion_tokens=22)
    chunks=[packet(reasoning='hidden thought'),packet(text[:8]),packet(text[8:],finish='stop'),packet(usage=usage)]
    binding,calls,recorded=setup(monkeypatch,chunks)
    binding['json_schema']={'type':'object','properties':{'summary':{'type':'string'}}}
    messages=[{'role':'user','content':'public paper material'}]
    with bind(binding):result=await provider.cloud.complete(messages,json_mode=True)
    assert result==value and len(calls)==1 and len(recorded)==1
    assert calls[0]['stream'] is True and calls[0]['response_format']=={'type':'json_object'}
    assert calls[0]['stream_options']=={'include_usage':True}
    assert 'JSON Schema' in calls[0]['messages'][-1]['content']
    assert len(messages)==1 and 'hidden thought' not in json.dumps(result)
    assert recorded[0]==('test-model',usage)


@pytest.mark.asyncio
@pytest.mark.parametrize('finish,message',[('length','长度上限'),(None,'尚未完成')])
async def test_trend_rejects_valid_json_when_transport_is_incomplete(monkeypatch,finish,message):
    binding,calls,recorded=setup(monkeypatch,[packet('{"items":[]}',finish=finish)])
    with bind(binding),pytest.raises(ValueError,match=message):await provider.cloud.complete([],json_mode=True)
    assert len(calls)==1 and len(recorded)==1


@pytest.mark.asyncio
async def test_trend_keeps_strict_json_errors_and_does_not_retry_partial_stream(monkeypatch):
    binding,calls,recorded=setup(monkeypatch,[packet('{"items":',finish='stop')])
    with bind(binding),pytest.raises(json.JSONDecodeError):await provider.cloud.complete([],json_mode=True)
    assert len(calls)==1 and len(recorded)==1


@pytest.mark.asyncio
async def test_other_feature_keeps_original_non_streaming_request(monkeypatch):
    usage=Obj(prompt_tokens=1,completion_tokens=2)
    response=Obj(choices=[Obj(message=Obj(content='{"value":1}'),finish_reason='stop')],usage=usage)
    binding,calls,recorded=setup(monkeypatch,[],feature='brief',response=response)
    with bind(binding):result=await provider.cloud.complete([],json_mode=True)
    assert result=={'value':1} and len(calls)==1 and 'stream' not in calls[0]
    assert calls[0]['max_tokens']==2048 and recorded==[('test-model',usage)]


@pytest.mark.asyncio
async def test_other_connection_keeps_non_streaming_trend_request(monkeypatch):
    usage=Obj(prompt_tokens=1,completion_tokens=2)
    response=Obj(choices=[Obj(message=Obj(content='{"value":1}'),finish_reason='stop')],usage=usage)
    binding,calls,recorded=setup(monkeypatch,[],response=response)
    binding['control_capabilities']['protocol']=None
    with bind(binding):result=await provider.cloud.complete([],json_mode=True)
    assert result=={'value':1} and 'stream' not in calls[0] and len(recorded)==1


@pytest.mark.asyncio
async def test_trend_cancellation_propagates_without_publishing_partial_content(monkeypatch):
    started=asyncio.Event();recorded=[]
    async def stream(*args,**kwargs):
        started.set();yield Obj(content='{"items":')
        try:await asyncio.Event().wait()
        finally:recorded.append('closed')
    monkeypatch.setattr(provider.cloud,'stream',stream)
    binding={'kind':'cloud','feature':'trend_report','model':'test-model','base_url':'https://example.invalid/v1',
             'control_capabilities':{'protocol':'bailian','thinking_modes':['auto'],'reasoning_efforts':[]}}
    with bind(binding):
        task=asyncio.create_task(provider.cloud.complete([],json_mode=True))
        await started.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    assert recorded==['closed']
