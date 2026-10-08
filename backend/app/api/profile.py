import json
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from ..auth import current_user, public_user, hash_password, verify_password
from ..db import rows, one, execute, connect, dumps
from ..config import now
from ..interest.profile import current
from ..interest.form import ProfileForm, parse_form, render_form
from ..interest.vectors import active_vectors
from ..interest import profile_updates
from ..catalog import CategorySelection, validate_selection, selected_topic_ids, directory

router = APIRouter(prefix='/api',dependencies=[Depends(current_user)])


def public_profile(p):
    return {**{k:v for k,v in p.items() if k not in ('embedding','embedding_parts')},'structured':json.loads(p['structured']), 'form':parse_form(p['content']), 'embedding_ready':bool(active_vectors(p))} if p else None


@router.get('/profile')
def profile(user=Depends(current_user)):
    newest = current(user['id'])
    job = profile_updates.latest(user['id'])
    draft = None
    if job and job['status'] in ('queued','processing','failed'):
        draft = {'id':job['base_profile_id'] or 0,'version':newest['version'] if newest else 0,
                 'content':job['content'],'structured':json.loads(job['structured']),
                 'form':parse_form(job['content']),'change_reason':job['reason'],
                 'created_at':job['created_at'],'embedding_ready':False}
    return {'current':public_profile(newest),'draft':draft,'update':profile_updates.state(job),'history':rows('SELECT id,version,change_reason,created_at FROM interest_profile WHERE user_id=? AND version<? ORDER BY version DESC LIMIT 3',(user['id'],newest['version'] if newest else 0))}


class ProfileInit(BaseModel):
    description: str = Field('',max_length=2000)
    topic_ids: list[int] = Field(default_factory=list,max_length=200)
    exclusions: list[str] = Field(default_factory=list,max_length=30)
    category_selection: CategorySelection | None = None


@router.post('/profile/init')
def init_profile(body: ProfileInit,user=Depends(current_user)):
    catalog = directory(with_counts=False) if body.category_selection is not None else None
    selection = validate_selection(body.category_selection, catalog) if catalog is not None else None
    ids = selected_topic_ids(selection, catalog) if selection is not None else body.topic_ids
    if not body.description.strip() and not ids and not (selection and selection['categories']):
        raise HTTPException(400,'请描述兴趣或选择至少一个主题')
    selected = [t for t in rows("SELECT * FROM topics WHERE status='active'") if t['id'] in ids]
    content = render_form(ProfileForm(description=body.description, exclusions=body.exclusions))
    entries = '\n'.join(f"- [w:0.7] {t['name_zh']}（{t['name_en']}）" for t in selected)
    content = content.replace('## 核心兴趣（长期）', '## 核心兴趣（长期）\n' + entries, 1)
    structured = {'topic_ids':[t['id'] for t in selected]}
    if selection is not None: structured['category_selection'] = selection
    return profile_updates.submit(user['id'],content,structured,'init')


class ProfileUpdate(BaseModel):
    content: str | None = Field(None,min_length=1,max_length=12000)
    topic_ids: list[int] | None = None
    form: ProfileForm | None = None
    category_selection: CategorySelection | None = None


@router.put('/profile')
def edit_profile(body: ProfileUpdate,user=Depends(current_user)):
    old = current(user['id'])
    pending = profile_updates.latest(user['id'])
    previous = pending if pending and pending['status'] in ('queued','processing','failed') else old
    structured = json.loads(previous['structured']) if previous else {}
    content = render_form(body.form, previous['content'] if previous else '') if body.form is not None else body.content
    if content is None:
        raise HTTPException(400,'请填写兴趣表单')
    if body.category_selection is not None:
        structured.pop('source_selection_removed',None)
        catalog = directory(with_counts=False)
        selection = validate_selection(body.category_selection, catalog)
        structured['category_selection'] = selection
        structured['topic_ids'] = selected_topic_ids(selection, catalog)
    if body.topic_ids is not None and body.category_selection is None:
        structured.pop('source_selection_removed',None)
        valid = {t['id'] for t in rows("SELECT id FROM topics WHERE status='active'")}
        if not set(body.topic_ids)<=valid:
            raise HTTPException(400,'主题不存在')
        structured['topic_ids']=body.topic_ids
        structured.pop('category_selection',None)
    return profile_updates.submit(user['id'],content,structured,'manual')


class Rollback(BaseModel):
    version: int


@router.post('/profile/rollback')
def rollback(body: Rollback,user=Depends(current_user)):
    target = one('SELECT * FROM interest_profile WHERE user_id=? AND version=?',(user['id'],body.version))
    if not target: raise HTTPException(404,'版本不存在')
    return profile_updates.submit(user['id'],target['content'],json.loads(target['structured']),'rollback')


class Watch(BaseModel):
    type: Literal['keyword','benchmark','author','topic']
    value: str = Field(min_length=1,max_length=200)


def add_watch(user_id,kind,value,db=None):
    value = value.strip()
    if not value: raise HTTPException(400,'监视值不能为空')
    def write(conn):
        existing = conn.execute('SELECT id FROM watches WHERE user_id=? AND type=? AND value=? AND active=1',(user_id,kind,value)).fetchone()
        if existing: return {'id':existing['id']}
        ident = conn.execute('INSERT INTO watches(user_id,type,value,created_at) VALUES(?,?,?,?)',(user_id,kind,value,now())).lastrowid
        from ..pipeline.alerts import bind_watch
        bind_watch(conn,dict(conn.execute('SELECT * FROM watches WHERE id=?',(ident,)).fetchone()))
        conn.execute('INSERT INTO audit_log(user_id,action,detail,created_at) VALUES(?,?,?,?)',(user_id,'watch.add',dumps({'id':ident,'type':kind,'value':value}),now()))
        return {'id':ident}
    if db is not None: return write(db)
    with connect() as conn: return write(conn)


@router.get('/watches')
def watches(user=Depends(current_user)):
    return rows('SELECT * FROM watches WHERE user_id=? AND active=1 ORDER BY id DESC',(user['id'],))


@router.post('/watches')
def create_watch(body: Watch,user=Depends(current_user)):
    return add_watch(user['id'],body.type,body.value)


@router.delete('/watches/{watch_id}')
def delete_watch(watch_id: int,user=Depends(current_user)):
    with connect() as db:
        if not db.execute('UPDATE watches SET active=0 WHERE id=? AND user_id=? AND active=1',(watch_id,user['id'])).rowcount:
            raise HTTPException(404,'监视项不存在')
        db.execute('INSERT INTO audit_log(user_id,action,detail,created_at) VALUES(?,?,?,?)',(user['id'],'watch.remove',dumps({'id':watch_id}),now()))
    return {'ok':True}


@router.get('/me')
def me(user=Depends(current_user)):
    return public_user(user)


class Preferences(BaseModel):
    notifications_enabled: bool
    telegram_enabled: bool = True
    telegram_chat_id: str | None = Field(None,max_length=50,pattern=r'^-?\d+$')


@router.put('/me/preferences')
def preferences(body: Preferences,user=Depends(current_user)):
    execute('UPDATE users SET notifications_enabled=?,telegram_enabled=?,telegram_chat_id=? WHERE id=?',(int(body.notifications_enabled),int(body.telegram_enabled),body.telegram_chat_id,user['id']))
    return {'ok':True}


class Password(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8,max_length=128)
    confirm_password: str = Field(min_length=8,max_length=128)


@router.put('/me/password')
def password(body: Password,user=Depends(current_user)):
    if body.new_password!=body.confirm_password:raise HTTPException(400,'两次输入的新密码不一致')
    if not verify_password(body.old_password,user['password_hash']): raise HTTPException(400,'原密码错误')
    with connect() as db:
        import secrets
        db.execute('UPDATE users SET password_hash=?,auth_epoch=? WHERE id=?',(hash_password(body.new_password),secrets.token_urlsafe(24),user['id']))
        db.execute('DELETE FROM refresh_tokens WHERE user_id=?',(user['id'],))
    return {'ok':True}
