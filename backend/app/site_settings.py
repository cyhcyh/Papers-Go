"""Public branding and a server-owned administration entry point."""
import base64
import json
import re
import secrets
from pathlib import Path
from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from .config import settings, now
from .db import connect, one, dumps

DEFAULTS = {'name':'刷论文', 'description':'像刷视频一样刷论文。发现论文，追踪趋势。', 'logo_url':'', 'favicon_url':''}
CACHE_DEFAULTS = {'fulltext_cache_days':7, 'fulltext_cache_mb':500}
LOG_DEFAULTS = {'log_retention_days':30, 'log_max_entries':500_000}
PUBLIC_PATHS = {'', 'login', 'register', 'browse', 'onboarding', 'trends', 'library', 'chat', 'notifications', 'settings'}
ASSET_NAME = re.compile(r'^[a-f0-9]{32}\.(png|jpg|webp|ico)$')


def initialize(db):
    existing_admin=db.execute('SELECT 1 FROM users WHERE is_admin=1 LIMIT 1').fetchone()
    db.execute("INSERT OR IGNORE INTO app_settings(name,value,updated_at) VALUES('site',?,?)",
               (dumps({**DEFAULTS,**CACHE_DEFAULTS,**LOG_DEFAULTS,'admin_path':'/console-'+secrets.token_urlsafe(18) if existing_admin else ''}),now()))


def create_admin_entry(db):
    config=json.loads(db.execute("SELECT value FROM app_settings WHERE name='site'").fetchone()['value'])
    config['admin_path']='/console-'+secrets.token_urlsafe(18)
    db.execute("UPDATE app_settings SET value=?,updated_at=? WHERE name='site'",(dumps(config),now()))
    return config['admin_path']


def configuration():
    saved=one("SELECT value FROM app_settings WHERE name='site'")
    return {**DEFAULTS, **CACHE_DEFAULTS, **LOG_DEFAULTS, **json.loads(saved['value'])} if saved else {**DEFAULTS,**CACHE_DEFAULTS,**LOG_DEFAULTS}


def public_configuration():
    config=configuration()
    return {key:config[key] for key in DEFAULTS}


def entry_for(path):
    base=configuration().get('admin_path','')
    return base if base and (path==base or path.startswith(base+'/')) else None


def is_retired(path):
    saved=one("SELECT value FROM app_settings WHERE name='retired_admin_paths'")
    bases=['/admin',*(json.loads(saved['value']) if saved else [])]
    return any(path==base or path.startswith(base+'/') for base in bases)


class ImageUpload(BaseModel):
    data: str = Field(min_length=1,max_length=2_800_000)


class SiteInput(BaseModel):
    name: str = Field(min_length=1,max_length=60)
    description: str = Field(max_length=500)
    admin_path: str = Field(min_length=7,max_length=81)
    logo_url: str = Field('',max_length=120)
    favicon_url: str = Field('',max_length=120)
    logo_upload: ImageUpload | None = None
    favicon_upload: ImageUpload | None = None
    fulltext_cache_days: int = Field(7,ge=0,le=3650)
    fulltext_cache_mb: int = Field(500,ge=0,le=1048576)
    log_retention_days: int = Field(30,ge=1,le=3650,strict=True)
    log_max_entries: int = Field(500_000,ge=1,le=10_000_000,strict=True)

    @field_validator('name')
    @classmethod
    def trim_name(cls,value):
        if not value.strip():raise ValueError('网站名字不能为空')
        return value.strip()

    @field_validator('admin_path')
    @classmethod
    def valid_entry(cls,value):
        value=value.strip().rstrip('/')
        if not re.fullmatch(r'/[A-Za-z0-9][A-Za-z0-9_-]{5,79}',value) or value[1:].lower() in PUBLIC_PATHS|{'admin','api','assets','site-assets'}:
            raise ValueError('入口使用 / 开头的 6～80 位字母、数字、下划线或短横线，不能与网站页面重名')
        return value


def asset_path(url):
    prefix='/api/site/assets/'
    if not url.startswith(prefix) or not ASSET_NAME.fullmatch(url[len(prefix):]):raise HTTPException(400,'网站图标地址无效')
    return settings().data_dir/'site-assets'/url[len(prefix):]


def save_image(upload):
    try:data=base64.b64decode(upload.data,validate=True)
    except ValueError:raise HTTPException(400,'图标文件编码无效')
    if not data or len(data)>2*1024*1024:raise HTTPException(400,'每个图标最多 2 MB')
    if data.startswith(b'\x89PNG\r\n\x1a\n'):suffix='png'
    elif data.startswith(b'\xff\xd8\xff'):suffix='jpg'
    elif data.startswith(b'RIFF') and data[8:12]==b'WEBP':suffix='webp'
    elif data.startswith(b'\x00\x00\x01\x00'):suffix='ico'
    else:raise HTTPException(400,'请选择 PNG、JPEG、WebP 或 ICO 图标')
    folder=settings().data_dir/'site-assets';folder.mkdir(parents=True,exist_ok=True)
    filename=secrets.token_hex(16)+'.'+suffix
    path=folder/filename;path.write_bytes(data)
    return '/api/site/assets/'+filename,path


def update(body):
    previous=configuration();config={key:getattr(body,key) for key in (*DEFAULTS,'admin_path')};created=[]
    config.update({key:getattr(body,key) if key in body.model_fields_set else previous[key] for key in (*CACHE_DEFAULTS,*LOG_DEFAULTS)})
    try:
        for field in ('logo','favicon'):
            upload=getattr(body,field+'_upload')
            if upload:
                url,path=save_image(upload);created.append(path);config[field+'_url']=url
            elif config[field+'_url'] and not asset_path(config[field+'_url']).is_file():
                raise HTTPException(400,'图标文件不存在，请重新上传')
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            retired=db.execute("SELECT value FROM app_settings WHERE name='retired_admin_paths'").fetchone()
            old=json.loads(retired['value']) if retired else []
            current=json.loads(db.execute("SELECT value FROM app_settings WHERE name='site'").fetchone()['value'])
            if 'recommendation' in current:
                config['recommendation']=current['recommendation']
            if current['admin_path']!=config['admin_path']:
                old=list(dict.fromkeys([*old,current['admin_path']]))
                db.execute("INSERT INTO app_settings VALUES('retired_admin_paths',?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(dumps(old),now()))
            db.execute("UPDATE app_settings SET value=?,updated_at=? WHERE name='site'",(dumps(config),now()))
    except BaseException:
        for path in created:path.unlink(missing_ok=True)
        raise
    for field in ('logo_url','favicon_url'):
        url=previous[field]
        if url and url not in (config['logo_url'],config['favicon_url']):
            asset_path(url).unlink(missing_ok=True)
    return config
