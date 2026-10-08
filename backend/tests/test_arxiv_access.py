import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from app import arxiv_client
from app.config import settings


@pytest.mark.asyncio
@pytest.mark.parametrize('status,headers,expected,next_start',[
    (429,{'Retry-After':'12'},[1000.,1012.,1024.],1036.),
    (503,{},[1000.,1030.,1090.],1210.),
])
async def test_retry_after_is_shared_and_retries_are_bounded(client, monkeypatch,status,headers,expected,next_start):
    clock = [1000.]
    started = []
    async def sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(arxiv_client.time, 'time', lambda: clock[0])
    monkeypatch.setattr(arxiv_client.asyncio, 'sleep', sleep)
    def respond(request):
        started.append(clock[0])
        assert request.headers['Connection'] == 'close'
        return httpx.Response(status, headers=headers)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await arxiv_client.get(http,'https://export.arxiv.org/api/query',retries=2)
        assert started == expected
        # A different feature reads the cooldown left by the last failed attempt.
        with pytest.raises(httpx.HTTPStatusError):
            await arxiv_client.get(http,'https://arxiv.org/abs/2609.00001')
        assert started[-1] == next_start


@pytest.mark.asyncio
async def test_forbidden_stops_subsequent_calls_without_more_network_requests(client):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(403)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        for url in ('https://export.arxiv.org/api/query','https://arxiv.org/abs/2609.00001'):
            with pytest.raises(httpx.HTTPStatusError) as error:
                await arxiv_client.get(http,url,retries=2)
            assert error.value.response.status_code == 403
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_redirects_also_respect_global_spacing(client,monkeypatch):
    clock=[1000.]; calls=[]
    async def sleep(seconds):clock[0]+=seconds
    monkeypatch.setattr(arxiv_client.time,'time',lambda:clock[0])
    monkeypatch.setattr(arxiv_client.asyncio,'sleep',sleep)
    def respond(request):
        calls.append((str(request.url),clock[0]))
        return httpx.Response(302,headers={'Location':'https://export.arxiv.org/api/query?q=ok'}) if len(calls)==1 else httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond),follow_redirects=True) as http:
        assert (await arxiv_client.get(http,'https://arxiv.org/old',params={'q':'old'})).status_code==200
    assert calls==[('https://arxiv.org/old?q=old',1000.),('https://export.arxiv.org/api/query?q=ok',1003.2)]


@pytest.mark.asyncio
async def test_cancelling_a_request_releases_the_shared_connection(client, monkeypatch):
    started = asyncio.Event()
    async def respond(request):
        if request.url.params.get('first'):
            started.set()
            await asyncio.Event().wait()
        return httpx.Response(200)
    monkeypatch.setattr(arxiv_client,'INTERVAL',.01)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        first = asyncio.create_task(arxiv_client.get(http,'https://export.arxiv.org/api/query',params={'first':'1'}))
        await asyncio.wait_for(started.wait(),1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert (await asyncio.wait_for(arxiv_client.get(http,'https://export.arxiv.org/api/query'),1)).status_code == 200


def test_web_and_worker_processes_share_connection_and_request_spacing(client, tmp_path):
    code = '''
import asyncio,json,time,sys
from pathlib import Path
import httpx
from app import arxiv_client
from app.config import settings
arxiv_client.INTERVAL=.3
root=settings().data_dir
(root/('ready-'+sys.argv[1])).touch()
deadline=time.time()+10
while len(list(root.glob('ready-*')))<2:
    if time.time()>deadline: raise RuntimeError('process barrier timed out')
    time.sleep(.01)
async def run():
    timings={}
    async def respond(request):
        timings['start']=time.monotonic()
        await asyncio.sleep(.15)
        timings['end']=time.monotonic()
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await arxiv_client.get(http,'https://export.arxiv.org/api/query')
    print(json.dumps(timings))
asyncio.run(run())
'''
    env = {**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]),'DATA_DIR':str(settings().data_dir)}
    processes = [subprocess.Popen([sys.executable,'-c',code,str(i)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for i in range(2)]
    try:
        results = []
        for process in processes:
            out, err = process.communicate(timeout=15)
            assert process.returncode == 0, err
            results.append(json.loads(out))
        first, second = sorted(results,key=lambda r:r['start'])
        assert second['start'] >= first['end']
        assert second['start'] - first['start'] >= .28
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
