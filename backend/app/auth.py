import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path
import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from .config import settings, now
from .db import connect, one

router = APIRouter(prefix='/api/auth', tags=['auth'])
bearer = HTTPBearer(auto_error=False)


def secret():
    cfg = settings()
    if cfg.jwt_secret:
        return cfg.jwt_secret
    path = cfg.data_dir / '.jwt-secret'
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open('x', encoding='utf-8') as f:
                f.write(secrets.token_urlsafe(48))
            path.chmod(0o600)
        except FileExistsError:
            pass
    return path.read_text(encoding='utf-8')


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f'scrypt${salt}${digest}'


def verify_password(password, stored):
    try:
        _, salt, expected = stored.split('$')
        result = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(result, expected)
    except ValueError:
        return False


def tokens(db, user):
    cfg = settings()
    issued = int(time.time())
    jti = secrets.token_urlsafe(32)
    expires = issued + cfg.refresh_days * 86400
    db.execute('INSERT INTO refresh_tokens VALUES(?,?,?)', (jti, user['id'], expires))
    db.execute('DELETE FROM refresh_tokens WHERE expires_at<?', (issued,))
    def encode(kind, expiry, token_id):
        return jwt.encode({'sub': str(user['id']), 'type': kind, 'iat': issued, 'exp': expiry, 'jti': token_id,'v':user.get('auth_epoch','legacy')}, secret(), algorithm='HS256')
    return {'access_token': encode('access', issued + cfg.access_minutes * 60, secrets.token_urlsafe(16)),
            'refresh_token': encode('refresh', expires, jti), 'token_type': 'bearer',
            'user': public_user(user)}


def public_user(user):
    return {k: user[k] for k in ('id', 'username', 'is_admin', 'telegram_chat_id', 'notifications_enabled', 'telegram_enabled')}


def decode(token, kind):
    try:
        payload = jwt.decode(token, secret(), algorithms=['HS256'], options={'require': ['sub','exp','iat','jti','type']})
        if payload['type'] != kind:
            raise ValueError()
        int(payload['sub'])
        return payload
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, '登录已过期，请重新登录')


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if not credentials:
        raise HTTPException(401, '请先登录')
    payload = decode(credentials.credentials, 'access')
    user = one('SELECT * FROM users WHERE id=?', (int(payload['sub']),))
    if not user or user['disabled'] or payload.get('v','legacy')!=user['auth_epoch']:
        raise HTTPException(401, '账号不可用')
    return user


def optional_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    return current_user(credentials) if credentials else None


def admin_user(user=Depends(current_user)):
    if not user['is_admin']:
        raise HTTPException(403, '仅管理员可操作')
    return user


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=40, pattern=r'^[\w.-]+$')
    password: str = Field(min_length=8, max_length=128)
    invite_code: str | None = None


@router.get('/config')
def auth_config():
    return {'require_invite_code': settings().require_invite_code, 'first_user': one('SELECT id FROM users LIMIT 1') is None}


@router.post('/register', status_code=201)
def register(body: Credentials):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        first = not db.execute('SELECT 1 FROM users LIMIT 1').fetchone()
        invite = db.execute('SELECT * FROM invite_codes WHERE code=? AND used_by IS NULL', (body.invite_code or '',)).fetchone()
        if settings().require_invite_code and not first and not invite:
            raise HTTPException(400, '邀请码无效或已使用')
        try:
            user_id=max(int(db.execute("SELECT value FROM app_settings WHERE name='last_user_id'").fetchone()[0]),db.execute('SELECT COALESCE(MAX(id),0) FROM users').fetchone()[0])+1
            db.execute('INSERT INTO users(id,username,password_hash,is_admin,created_at,auth_epoch) VALUES(?,?,?,?,?,?)',
                       (user_id,body.username, hash_password(body.password), int(first), now(),secrets.token_urlsafe(24)))
            db.execute("UPDATE app_settings SET value=?,updated_at=? WHERE name='last_user_id'",(str(user_id),now()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, '用户名已存在')
        if invite:
            db.execute('UPDATE invite_codes SET used_by=? WHERE code=?', (user_id, body.invite_code))
        user = dict(db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone())
        result=tokens(db,user)
        if first:
            from .site_settings import create_admin_entry
            result['admin_entry']=create_admin_entry(db)
        return result


@router.post('/login')
def login(body: Credentials):
    with connect() as db:
        row = db.execute('SELECT * FROM users WHERE username=?', (body.username,)).fetchone()
        if not row or row['disabled'] or not verify_password(body.password, row['password_hash']):
            raise HTTPException(401, '用户名或密码错误')
        return tokens(db, dict(row))


@router.post('/admin-login')
def admin_login(body: Credentials):
    with connect() as db:
        row=db.execute('SELECT * FROM users WHERE username=?',(body.username,)).fetchone()
        if not row or row['disabled'] or not row['is_admin'] or not verify_password(body.password,row['password_hash']):
            raise HTTPException(401,'管理员账号或密码错误')
        return tokens(db,dict(row))


class Refresh(BaseModel):
    refresh_token: str


@router.post('/refresh')
def refresh(body: Refresh):
    payload = decode(body.refresh_token, 'refresh')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        token = db.execute('SELECT * FROM refresh_tokens WHERE jti=? AND user_id=?', (payload['jti'], int(payload['sub']))).fetchone()
        user = db.execute('SELECT * FROM users WHERE id=? AND disabled=0', (int(payload['sub']),)).fetchone()
        if not token or not user or payload.get('v','legacy')!=user['auth_epoch']:
            raise HTTPException(401, '刷新凭证无效')
        db.execute('DELETE FROM refresh_tokens WHERE jti=?', (payload['jti'],))
        return tokens(db, dict(user))


@router.post('/logout')
def logout(body: Refresh, user=Depends(current_user)):
    payload = decode(body.refresh_token, 'refresh')
    with connect() as db:
        db.execute('DELETE FROM refresh_tokens WHERE jti=? AND user_id=?', (payload['jti'], user['id']))
    return {'ok': True}
