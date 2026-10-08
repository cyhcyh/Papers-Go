"""Isolated UI fixtures: synthetic papers, simulated jobs and a model-list stub."""
import asyncio
import os
from types import SimpleNamespace
from pathlib import Path

import httpx
from openai import AsyncOpenAI
import uvicorn
from fastapi.responses import JSONResponse

os.environ['DATA_DIR']=str(Path('data/ui-redesign').resolve())
os.environ['BOOTSTRAP_ENABLED']='false'
os.environ['SCHEDULER_ENABLED']='false'
os.environ['PIPELINE_MODE']='inline'
from app.db import init_db, execute, dumps
from app.config import today
from app.llm import runtime
from app.llm.provider import cloud
from app.llm.ollama import ollama
from app import scheduler
from app.config import now
from app.db import one
from app.api import model_config
from app.llm.controls import capabilities
from app.main import app

init_db()
async def preview_capabilities(binding):
    return capabilities(binding)
model_config.discover=preview_capabilities
async def preview_stream(messages,tools):
    if any('新会话工具显示测试' in (m.get('content') or '') for m in messages if m['role']=='user'):
        completed=sum(m['role']=='tool' for m in messages)
        if completed<2:
            await asyncio.sleep(.15)
            name,args=('show_profile','{}') if completed==0 else ('search_papers','{"query":"组合数学"}')
            call=SimpleNamespace(index=0,id='preview-tool-'+str(completed),function=SimpleNamespace(name=name,arguments=args))
            yield SimpleNamespace(content=None,tool_calls=[call])
            return
        for text in ('## 组合数学方向（测试数据）\n\n','| 方向 | 典型问题 |\n| --- | --- |\n','| 图论 | 图的结构、染色与极值 |\n','| 计数组合 | 枚举、生成函数与渐近估计 |\n\n','两次工具调用后的最终回答应持续可见。'):
            await asyncio.sleep(.15)
            yield SimpleNamespace(content=text,tool_calls=[])
        return
    yield SimpleNamespace(content='<thi',tool_calls=[],reasoning_content='界面测试的隐藏思考')
    yield SimpleNamespace(content='nk>界面测试的隐藏思考</think>',tool_calls=[])
    if messages[-1].get('content') == '流式前缀回归测试':
        for text in ('## ', '研究方法\n\n', '1. ', '使用 $O(n)$ 描述复杂度。\n', '2. ', '保留公式与完整结果。\n\n', '- ', '第一条研究线索。\n', '* ', '第二条研究线索。\n\n', '### ', '主要结果\n\n', '前缀分块显示完成，页面继续正常运行。'):
            await asyncio.sleep(.2)
            yield SimpleNamespace(content=text,tool_calls=[])
        return
    if messages[-1].get('content', '').startswith('表格渲染回归测试'):
        for text in ('## 方法比较\n\n', '| 方法 | 复杂度 | 适用场景 |\n', '| :--- | :---: | ---: |\n', '| **向量检索** | $O(n)$ | 语义相似的论文 |\n', '| 图结构检索 | $O(n+m)$ | 关联路径与结构分析 |\n', '| 混合方法 | `a\\|b` | 同时保留语义和关系 |\n', '\n表格输出完成，公式与正文均可正常显示。'):
            await asyncio.sleep(.2)
            yield SimpleNamespace(content=text, tool_calls=[])
        return
    for text in ('可以从**记忆机制**和评测方法两条线索入手。\n\n','- 用 $O(n)$ 描述检索复杂度。\n- 对比长任务中的信息保留。\n\n','```python\nprint("research")\n```'):
        await asyncio.sleep(.3)
        yield SimpleNamespace(content=text,tool_calls=[])
    if any('停止测试' in (m.get('content') or '') for m in messages if m['role']=='user'):
        await asyncio.sleep(60)
cloud.stream=preview_stream
ollama.stream=preview_stream

# Delay snapshots only for this disposable fixture. Older GET responses must
# not replace replies sent while those responses were still in flight.
history_requests = {}
reusable_session = None
@app.middleware('http')
async def delayed_history(request, call_next):
    global reusable_session
    # Simulate a successful deletion and ID reuse without deleting fixture
    # history. This exercises the real frontend delete/new-session lifecycle.
    path = request.url.path
    if request.method == 'DELETE' and path.startswith('/api/chat/sessions/'):
        item = one('SELECT id,title FROM chat_sessions WHERE id=?', (path.rsplit('/', 1)[-1],))
        if item and item['title'].startswith('会话编号复用测试'):
            reusable_session = item['id']
            return JSONResponse({'ok': True})
    if request.method == 'POST' and path == '/api/chat/sessions' and reusable_session is not None:
        ident, reusable_session = reusable_session, None
        body = await request.json()
        title = body.get('title', '新对话')
        execute('UPDATE chat_sessions SET title=? WHERE id=?', (title, ident))
        return JSONResponse({'id': ident, 'title': title})
    response = await call_next(request)
    parts = request.url.path.split('/')
    if request.method == 'GET' and len(parts) == 6 and parts[1:4] == ['api', 'chat', 'sessions'] and parts[5] == 'messages':
        session = one('SELECT title FROM chat_sessions WHERE id=?', (parts[4],))
        if session and session['title'] == '记录同步竞态测试':
            count = history_requests.get(parts[4], 0) + 1
            history_requests[parts[4]] = count
            if count <= 2:
                await asyncio.sleep(4)
    return response

preview_user = one("SELECT id FROM users WHERE username='ui_preview'")
if preview_user and not one("SELECT id FROM chat_sessions WHERE user_id=? AND title='记录同步竞态测试'", (preview_user['id'],)):
    ident = execute('INSERT INTO chat_sessions(user_id,title,created_at) VALUES(?,?,?)', (preview_user['id'], '记录同步竞态测试', now()))
    execute("INSERT INTO chat_messages(session_id,role,content,created_at) VALUES(?,'user',?,?)", (ident, '历史问题（测试数据）', now()))
    execute("INSERT INTO chat_messages(session_id,role,content,created_at) VALUES(?,'assistant',?,?)", (ident, '历史回复（测试数据）', now()))
summary='记忆研究通过检索与外部存储，探索语言模型在长程任务中保留和调用信息的方法。组合数学则围绕图的结构与染色约束，利用概率和构造方法改进相关界限。'
for user in [None]+[u['id'] for u in __import__('app.db',fromlist=['rows']).rows('SELECT id FROM users')]:
    profile=one('SELECT version FROM interest_profile WHERE user_id=? ORDER BY version DESC LIMIT 1',(user,)) if user else None
    execute('INSERT OR REPLACE INTO direction_trends(audience,profile_version,summary,evidence,created_at,attempted_at,error) VALUES(?,?,?,?,?,?,NULL)',('user:'+str(user) if user else 'guest',profile['version'] if profile else 0,summary,'[]',now(),now()))
execute('UPDATE papers SET published=?,ingested_date=?',(today(),today()))
execute('UPDATE papers SET authors=? WHERE id=1',(dumps(['Alice Researcher','Bo Chen','Carol Smith','Diana Wang','Evan Li','Fiona Zhang','Grace Lee','Hector Zhao','Irene Wu','Jasper Liu','Kira Xu','Leo Sun']),))
execute('UPDATE papers SET authors=? WHERE id=2',(dumps(['Alice Wang','Bo Chen','Carol Li']),))
execute("DELETE FROM interactions WHERE action='view'")

def model_list(request):
    if request.url.path.endswith('/models'):
        return httpx.Response(200,json={'object':'list','data':[{'id':m,'object':'model','created':0,'owned_by':'preview'} for m in ('preview-chat','preview-reader','preview-classifier')]})
    return httpx.Response(503,json={'error':{'message':'Preview only: generation is disabled'}})

def preview_client():
    binding=runtime.current_binding()
    return AsyncOpenAI(base_url=binding['base_url'],api_key=binding.get('api_key') or 'ollama',max_retries=0,http_client=httpx.AsyncClient(transport=httpx.MockTransport(model_list)))

async def status():
    return {'ready':True,'connected':True,'models':['qwen3:4b','bge-m3'],'required':['qwen3:4b','bge-m3'],'missing':[]}

cloud.client=preview_client
ollama.status=status

def stage(name):
    async def run():
        if name in ('fetch_arxiv','fetch_community'):
            await asyncio.sleep(120)
        return 0
    return run

for name in scheduler.jobs:
    scheduler.jobs[name]=stage(name)

if __name__=='__main__':
    print('Preview fixtures only; production database and model endpoints are untouched.',flush=True)
    uvicorn.run(app,host='127.0.0.1',port=18765)
