import json
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from ..auth import current_user
from ..config import now, settings
from ..db import rows, one, execute, connect
from ..agent.chat import chat_events, _active_sessions, _active_tasks
from ..llm.thinking import clean_answer
from ..agent.tools import confirm_tool

router = APIRouter(prefix='/api/chat',dependencies=[Depends(current_user)])


def owned_session(session_id,user_id):
    session = one('SELECT * FROM chat_sessions WHERE id=? AND user_id=?',(session_id,user_id))
    if not session: raise HTTPException(404,'会话不存在')
    return session


@router.get('/sessions')
def sessions(user=Depends(current_user)):
    return rows('SELECT * FROM chat_sessions WHERE user_id=? ORDER BY id DESC',(user['id'],))


class Session(BaseModel):
    title: str = Field('新对话',min_length=1,max_length=100)


@router.post('/sessions')
def new_session(body: Session,user=Depends(current_user)):
    ident = execute('INSERT INTO chat_sessions(user_id,title,created_at) VALUES(?,?,?)',(user['id'],body.title,now()))
    return {'id':ident,'title':body.title}


@router.get('/sessions/{session_id}/messages')
def messages(session_id: int,user=Depends(current_user)):
    owned_session(session_id,user['id'])
    items = rows('SELECT * FROM chat_messages WHERE session_id=? ORDER BY id',(session_id,))
    pending = {p['id']:p for p in rows('SELECT * FROM pending_tools WHERE session_id=? AND user_id=?',(session_id,user['id']))}
    for message in items:
        if message['role']=='assistant':
            message['content'] = clean_answer(message['content'])
        proposals = json.loads(message['tool_calls'] or '[]') if message['role']=='assistant' else []
        message['proposals']=[{**p,'status':pending.get(p['id'],{}).get('status','rejected')} for p in proposals if 'skill_event' not in p]
        message['skill_events']=[p['skill_event'] for p in proposals if 'skill_event' in p]
    return [m for m in items if m['role']!='tool']


async def stop_session(session_id):
    task = _active_tasks.get(session_id)
    if task and not task.done():
        task.cancel()
        await asyncio.wait([task],timeout=5)
        if not task.done():
            raise HTTPException(409,'回复正在停止，请稍后重试')
    elif session_id in _active_sessions:
        raise HTTPException(409,'回复正在开始，请稍后重试')


@router.post('/sessions/{session_id}/stop')
async def stop_reply(session_id: int,user=Depends(current_user)):
    owned_session(session_id,user['id'])
    await stop_session(session_id)
    return {'ok':True}


@router.delete('/sessions/{session_id}')
async def delete_session(session_id: int,user=Depends(current_user)):
    owned_session(session_id,user['id'])
    await stop_session(session_id)
    with connect() as db:
        if not db.execute('SELECT id FROM chat_sessions WHERE id=? AND user_id=?',(session_id,user['id'])).fetchone():
            raise HTTPException(404,'会话不存在')
        db.execute('DELETE FROM pending_tools WHERE session_id=? AND user_id=?',(session_id,user['id']))
        db.execute('DELETE FROM chat_messages WHERE session_id=?',(session_id,))
        db.execute('DELETE FROM chat_sessions WHERE id=? AND user_id=?',(session_id,user['id']))
    return {'ok':True}


class Message(BaseModel):
    content: str = Field(min_length=1,max_length=6000)
    paper_id: int | None = None


class SessionBatch(BaseModel):
    ids: list[int] = Field(min_length=1)


@router.post('/sessions/batch-delete')
async def delete_sessions(body: SessionBatch,user=Depends(current_user)):
    ids = list(dict.fromkeys(body.ids))
    def selected(db):
        db.execute('CREATE TEMP TABLE selected_sessions(id INTEGER PRIMARY KEY)')
        db.executemany('INSERT INTO selected_sessions VALUES(?)',[(i,) for i in ids])
        return db.execute('SELECT COUNT(*) FROM chat_sessions WHERE user_id=? AND id IN (SELECT id FROM selected_sessions)',(user['id'],)).fetchone()[0]
    with connect() as db:
        if selected(db)!=len(ids):raise HTTPException(404,'部分会话不存在')
    for ident in ids: await stop_session(ident)
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        count=selected(db)
        if count!=len(ids): raise HTTPException(404,'部分会话已被删除，请刷新列表')
        db.execute('DELETE FROM pending_tools WHERE user_id=? AND session_id IN (SELECT id FROM selected_sessions)',(user['id'],))
        db.execute('DELETE FROM chat_messages WHERE session_id IN (SELECT id FROM selected_sessions)')
        db.execute('DELETE FROM chat_sessions WHERE user_id=? AND id IN (SELECT id FROM selected_sessions)',(user['id'],))
    return {'deleted':ids}


@router.post('/sessions/{session_id}/messages')
async def send_message(session_id: int,body: Message,user=Depends(current_user)):
    session = owned_session(session_id,user['id'])
    if session_id in _active_sessions: raise HTTPException(409,'当前会话正在回复')
    if len(_active_sessions)>=settings().chat_concurrency: raise HTTPException(429,'当前对话较多，请稍后发送')
    if _active_sessions:
        ids=list(_active_sessions)
        count=one('SELECT COUNT(*) n FROM chat_sessions WHERE user_id=? AND id IN ('+','.join('?' for _ in ids)+')',[user['id'],*ids])['n']
        if count>=2: raise HTTPException(429,'您已有两个会话正在回复，请先等待或停止回复')
    if body.paper_id and not one('SELECT id FROM papers WHERE id=?',(body.paper_id,)): raise HTTPException(404,'论文不存在')
    if session['title']=='新对话': execute('UPDATE chat_sessions SET title=? WHERE id=?',(body.content[:30],session_id))
    _active_sessions.add(session_id)
    return StreamingResponse(chat_events(session_id,user['id'],body.content,body.paper_id),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})


class Confirm(BaseModel):
    id: str
    approve: bool = True


@router.post('/confirm')
async def confirm(body: Confirm,user=Depends(current_user)):
    return await confirm_tool(body.id,user['id'],body.approve)
