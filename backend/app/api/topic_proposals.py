import json
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from ..auth import current_user
from ..db import connect, rows, dumps
from ..config import now
from ..standard_topics import catalog, search, scope_keys
from ..catalog import validate_category_keys
from ..logs import event

router=APIRouter(prefix='/api',dependencies=[Depends(current_user)])

@router.get('/standards')
def directory(query:str=Query('',max_length=150),system:str='',parent:str|None=None):return search(query,system,parent,500)

@router.get('/topic-proposals')
def mine(user=Depends(current_user)):
    return rows("SELECT p.id,p.standard_key,p.note,p.created_at,t.id topic_id,t.name_zh,t.status FROM topic_proposals p LEFT JOIN topics t ON t.id=p.topic_id WHERE p.user_id=? ORDER BY p.id DESC",(user['id'],))

class Proposal(BaseModel):
    standard_key:str=Field(max_length=100)
    category_keys:list[str]=Field(min_length=1)
    note:str=Field('',max_length=1000)

@router.post('/topic-proposals')
def propose(body:Proposal,user=Depends(current_user)):
    entry=catalog().get(body.standard_key)
    if not entry:raise HTTPException(400,'请选择研究方向目录中的主题')
    keys=validate_category_keys(body.category_keys)
    if scope_keys(entry,keys)!=sorted(keys):raise HTTPException(400,'所选主类无效')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        topic=db.execute('SELECT * FROM topics WHERE standard_key=?',(entry['key'],)).fetchone()
        if topic and topic['status']!='proposed':raise HTTPException(409,'该主题已启用或停用，请联系管理员管理现有主题')
        if not topic:
            tid=db.execute("INSERT INTO topics(name_zh,name_en,status,created_by,created_at,category_keys,standard_key,standard_system,standard_code,standard_path,discipline,description) VALUES(?,?,'proposed','user',?,?,?,?,?,?,?,?)",(entry['name_zh'],entry['label'],now(),dumps(keys),entry['key'],entry['system'],entry['code'],entry['path'],entry['discipline'],entry['description'])).lastrowid
        else:
            tid=topic['id'];db.execute('UPDATE topics SET category_keys=? WHERE id=?',(dumps(sorted(set(keys)|set(json.loads(topic['category_keys'] or '[]')))),tid))
        db.execute('INSERT INTO topic_proposals(user_id,topic_id,standard_key,note,created_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id,standard_key) DO UPDATE SET topic_id=excluded.topic_id,note=excluded.note,created_at=excluded.created_at',(user['id'],tid,entry['key'],body.note.strip(),now()))
    event('topic','用户提交主题提议',user_id=user['id'],topic_id=tid,standard_key=entry['key'])
    return {'topic_id':tid,'status':'proposed'}
