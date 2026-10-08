"""Administrator account edits and complete removal of personal records."""
import secrets
import sqlite3
from fastapi import HTTPException
from pydantic import BaseModel, Field
from .auth import hash_password, public_user
from .db import connect, one, rows
from .logs import event


class UserUpdate(BaseModel):
    username: str | None = Field(None,min_length=3,max_length=40,pattern=r'^[\w.-]+$')
    password: str | None = Field(None,min_length=8,max_length=128)
    disabled: bool | None = None
    is_admin: bool | None = None


def guard_admin(db, user, admin_id, removing=False):
    if user['id']==admin_id:
        raise HTTPException(400,'不能删除或禁用当前登录管理员')
    if user['is_admin'] and not user['disabled'] and db.execute('SELECT COUNT(*) FROM users WHERE is_admin=1 AND disabled=0').fetchone()[0]<=1:
        raise HTTPException(400,'不能删除或禁用最后一个可用管理员')


def edit_user(user_id, body, admin):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        user=db.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
        if not user:raise HTTPException(404,'用户不存在')
        role_changed=body.is_admin is not None and bool(user['is_admin'])!=body.is_admin
        if body.disabled or role_changed and not body.is_admin:guard_admin(db,user,admin['id'])
        updates={k:v for k,v in {'username':body.username,'disabled':int(body.disabled) if body.disabled is not None else None,'is_admin':int(body.is_admin) if body.is_admin is not None else None}.items() if v is not None}
        if body.password is not None:updates['password_hash']=hash_password(body.password)
        if body.password is not None or body.disabled or role_changed:
            updates['auth_epoch']=secrets.token_urlsafe(24)
            db.execute('DELETE FROM refresh_tokens WHERE user_id=?',(user_id,))
        if not updates:raise HTTPException(400,'没有需要更新的内容')
        try:db.execute('UPDATE users SET '+','.join(k+'=?' for k in updates)+' WHERE id=?',(*updates.values(),user_id))
        except sqlite3.IntegrityError:raise HTTPException(409,'用户名已存在')
        updated=dict(db.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone())
    event('admin','管理员更新用户',user_id=admin['id'],target_user_id=user_id,username_changed=body.username is not None,password_reset=body.password is not None,disabled=body.disabled,is_admin=body.is_admin)
    return {'ok':True,'user':public_user(updated),'logged_out':user_id==admin['id'] and body.password is not None}


def deletion_impact(user_id):
    user=one('SELECT id,username,is_admin FROM users WHERE id=?',(user_id,))
    if not user:raise HTTPException(404,'用户不存在')
    counts={name:one('SELECT COUNT(*) n FROM '+table+' WHERE user_id=?',(user_id,))['n'] for name,table in [('sessions','chat_sessions'),('profiles','interest_profile'),('paper_states','user_paper_state'),('interactions','interactions'),('watches','watches'),('notifications','notifications')]}
    counts['skills']=one('SELECT COUNT(*) n FROM agent_skills WHERE owner_id=?',(user_id,))['n']
    return {**user,'counts':counts}


class UserBatch(BaseModel):
    ids: list[int] = Field(min_length=1)


def batch_users(db, ids, admin_id):
    ids=sorted(set(ids))
    db.execute('CREATE TEMP TABLE selected_users(id INTEGER PRIMARY KEY)')
    db.executemany('INSERT INTO selected_users VALUES(?)',[(i,) for i in ids])
    found=[dict(u) for u in db.execute('SELECT * FROM users WHERE id IN (SELECT id FROM selected_users)')]
    if len(found)!=len(ids):raise HTTPException(409,'用户列表已变化，请刷新后重试')
    if admin_id in ids:raise HTTPException(400,'不能删除、禁用或降级当前登录管理员')
    remaining=db.execute('SELECT COUNT(*) FROM users WHERE is_admin=1 AND disabled=0 AND id NOT IN (SELECT id FROM selected_users)').fetchone()[0]
    if not remaining and any(u['is_admin'] and not u['disabled'] for u in found):
        raise HTTPException(400,'必须保留至少一个可用管理员')
    return found


def batch_impact(ids, admin):
    with connect() as db:
        found=batch_users(db,ids,admin['id'])
    details=[deletion_impact(u['id']) for u in found]
    return {'users':[{'id':u['id'],'username':u['username']} for u in found],
            'counts':{key:sum(d['counts'][key] for d in details) for key in details[0]['counts']}}


async def stop_personal_tasks(user_id):
    from .api.chat import stop_session
    from .pipeline.direction_trends import cancel_user_generation
    await cancel_user_generation(user_id)
    for session in rows('SELECT id FROM chat_sessions WHERE user_id=?',(user_id,)):
        await stop_session(session['id'])


async def disable_users(ids, admin):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        found=batch_users(db,ids,admin['id'])
        for user in found:
            db.execute('UPDATE users SET disabled=1,auth_epoch=? WHERE id=?',(secrets.token_urlsafe(24),user['id']))
            db.execute('DELETE FROM refresh_tokens WHERE user_id=?',(user['id'],))
    for user in found:await stop_personal_tasks(user['id'])
    event('admin','管理员批量禁用用户',user_id=admin['id'],target_user_ids=[u['id'] for u in found])
    return {'ok':True,'updated':[u['id'] for u in found]}


async def delete_users(ids, admin):
    # Validate the entire selection before invalidating sessions or removing any data.
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        found=batch_users(db,ids,admin['id'])
        epochs={u['id']:secrets.token_urlsafe(24) for u in found}
        for ident,epoch in epochs.items():
            db.execute('UPDATE users SET disabled=1,auth_epoch=? WHERE id=?',(epoch,ident))
            db.execute('DELETE FROM refresh_tokens WHERE user_id=?',(ident,))
    for ident in epochs:await stop_personal_tasks(ident)
    skill_ids=[]
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        latest=batch_users(db,list(epochs),admin['id'])
        if any(u['auth_epoch']!=epochs[u['id']] for u in latest):raise HTTPException(409,'账号状态已变化，请刷新后重试')
        for user_id in epochs:
            skill_ids.extend(r['id'] for r in db.execute('SELECT id FROM agent_skills WHERE owner_id=?',(user_id,)))
            db.execute('DELETE FROM chat_messages WHERE session_id IN (SELECT id FROM chat_sessions WHERE user_id=?)',(user_id,))
            for table in ('pending_tools','chat_sessions','user_paper_state','interactions','interest_profile','watches','notifications','audit_log','daily_metrics','trend_reports','refresh_tokens','topic_proposals'):
                db.execute('DELETE FROM '+table+' WHERE user_id=?',(user_id,))
            db.execute('DELETE FROM direction_trends WHERE audience=? OR audience LIKE ?',(f'user:{user_id}',f'user:{user_id}:%'))
            db.execute('DELETE FROM app_logs WHERE user_id=?',(user_id,))
            db.execute('DELETE FROM invite_codes WHERE used_by=?',(user_id,))
            db.execute('DELETE FROM users WHERE id=?',(user_id,))
    from .agent_skills import remove_files
    remove_files(skill_ids)
    from .pipeline.score import clear_user_cache
    for user_id in epochs:clear_user_cache(user_id)
    event('admin','管理员删除用户及个人数据',user_id=admin['id'],target_user_ids=list(epochs))
    return {'ok':True,'deleted':list(epochs)}


async def delete_user(user_id, admin):
    if not one('SELECT id FROM users WHERE id=?',(user_id,)):raise HTTPException(404,'用户不存在')
    await delete_users([user_id],admin)
    return {'ok':True,'deleted':user_id}
