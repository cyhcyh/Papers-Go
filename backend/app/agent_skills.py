"""Standard skill packages: indexed discovery, immutable bundles, scoped access."""
import json
import re
import shutil
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path, PurePosixPath

import yaml
from fastapi import HTTPException
from pydantic import BaseModel, Field, ConfigDict

from .config import settings, now
from .db import connect, one, rows, dumps
from .logs import event

SCHEMA = '''
CREATE TABLE IF NOT EXISTS agent_skills (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
 scope TEXT NOT NULL CHECK(scope IN ('system','shared','personal')),
 owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
 status TEXT NOT NULL DEFAULT 'active', enabled INTEGER NOT NULL DEFAULT 1,
 revision INTEGER NOT NULL DEFAULT 1, version_id TEXT NOT NULL, system_key TEXT UNIQUE, instruction_version TEXT,
 source_id TEXT REFERENCES agent_skills(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_skill_personal_name ON agent_skills(owner_id,name) WHERE scope='personal';
CREATE UNIQUE INDEX IF NOT EXISTS idx_skill_public_name ON agent_skills(name) WHERE scope IN ('shared','system') AND status='active';
CREATE INDEX IF NOT EXISTS idx_skills_catalog ON agent_skills(scope,status,enabled,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_skills_owner ON agent_skills(owner_id,scope,status,updated_at DESC);
CREATE TABLE IF NOT EXISTS agent_skill_versions (
 id TEXT PRIMARY KEY, skill_id TEXT NOT NULL REFERENCES agent_skills(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL, author_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL, UNIQUE(skill_id,revision));
CREATE TABLE IF NOT EXISTS chat_session_skills (
 session_id INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
 skill_id TEXT NOT NULL REFERENCES agent_skills(id) ON DELETE CASCADE,
 PRIMARY KEY(session_id,skill_id));
'''
NAME = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
BASE_TOOLS = {'search_papers', 'read_now', 'show_profile', 'update_interest', 'add_watch'}
SKILL_TOOLS = {'list_skills', 'get_skill', 'activate_skill', 'read_skill_resource', 'create_skill', 'update_skill', 'set_skill_enabled', 'propose_shared_skill'}
CHAT_TOOLS = BASE_TOOLS | SKILL_TOOLS
SYSTEM_DIR = Path(__file__).with_name('skills')
_loaded = ContextVar('loaded_skill_versions', default=None)


@contextmanager
def invocation():
    token=_loaded.set({})
    try:yield
    finally:_loaded.reset(token)


class SkillEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1024)
    text: str = Field(min_length=1, max_length=16000)
    allowed_tools: list[str] = Field(default_factory=list, max_length=len(CHAT_TOOLS))
    resources: dict[str, str] = Field(default_factory=dict)
    document: str | None = Field(None, max_length=20000)
    expected_revision: int | None = None


def parse_document(document):
    match = re.match(r'\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n', document, re.S)
    if not match: raise HTTPException(400, 'SKILL.md 需要 YAML 元数据头')
    try: meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError: raise HTTPException(400, 'SKILL.md 元数据格式错误')
    if not isinstance(meta, dict): raise HTTPException(400, '技能元数据必须为对象')
    name, description = meta.get('name'), meta.get('description')
    if not isinstance(name, str) or not NAME.fullmatch(name) or len(name)>64:
        raise HTTPException(400, '技能名称须为小写英文、数字及单个连字符，最多 64 字符')
    if not isinstance(description, str) or not description.strip() or len(description)>1024:
        raise HTTPException(400, '技能用途不能为空，最多 1024 字符')
    if set(meta)-{'name','description','license','compatibility','metadata','allowed-tools'}:
        raise HTTPException(400, '技能元数据包含不支持的字段')
    metadata = meta.get('metadata', {})
    if not isinstance(metadata, dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in metadata.items()):
        raise HTTPException(400, 'metadata 须为字符串键值对')
    for field in ('license','compatibility','allowed-tools'):
        if field in meta and not isinstance(meta[field],str): raise HTTPException(400, field+' 须为字符串')
    allowed = meta.get('allowed-tools','').split()
    if not set(allowed)<=CHAT_TOOLS: raise HTTPException(400, '技能只能使用网站已授权的工具，不支持脚本、Shell 或管理员工具')
    body = document[match.end():]
    if not body.strip() or len(body)>16000: raise HTTPException(400, '技能正文不能为空，最多 16000 字符')
    title=metadata.get('title',name)
    if not title.strip() or len(title)>100: raise HTTPException(400, '技能显示名称最多 100 字符')
    return meta, body


def document_for(name, title, description, text, allowed_tools=()):
    meta={'name':name,'description':description,'metadata':{'title':title}}
    if allowed_tools:meta['allowed-tools']=' '.join(allowed_tools)
    return '---\n'+yaml.safe_dump(meta,allow_unicode=True,sort_keys=False)+'---\n'+text


def validate_edit(body):
    doc=body.document if body.document is not None else document_for(body.name,body.title,body.description,body.text,body.allowed_tools)
    meta,text=parse_document(doc)
    resources=body.resources
    if len(resources)>8 or sum(len(v) for v in resources.values())>96000:
        raise HTTPException(400, '最多 8 份参考资料，总计最多 96000 字符')
    for name,value in resources.items():
        path=PurePosixPath(name)
        if (len(name)>160 or not re.fullmatch(r'(references|assets)/[a-zA-Z0-9_./-]+',name)
            or any(p in ('.','..') for p in name.split('/')) or path.suffix.lower() not in ('.md','.txt','.json','.csv')
            or len(value)>24000):
            raise HTTPException(400, '参考资料须为 references/ 或 assets/ 下的文本文件，每份最多 24000 字符')
    return meta,text,doc,resources


def root(): return settings().data_dir / 'skills'


def bundle_path(skill_id,version_id,name):
    # IDs and package names never come from filesystem paths supplied by the model.
    if not re.fullmatch(r'(quality|sk_[a-f0-9]{32})',skill_id) or not re.fullmatch(r'[a-f0-9]{32}',version_id) or not NAME.fullmatch(name):
        raise HTTPException(400,'技能路径无效')
    return root()/skill_id/version_id/name


def write_bundle(skill_id,version_id,name,document,resources):
    directory=bundle_path(skill_id,version_id,name)
    directory.mkdir(parents=True,exist_ok=False)
    (directory/'SKILL.md').write_text(document,encoding='utf-8',newline='')
    for relative,text in resources.items():
        target=directory/relative;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(text,encoding='utf-8',newline='')
    (directory.parent/'manifest.json').write_text(dumps({'name':name,'resources':list(resources)}),encoding='utf-8')


def ensure_unique(db,name,scope,owner_id,ident=None):
    clause="scope='personal' AND owner_id=?" if scope=='personal' else "scope IN ('system','shared') AND status='active'"
    args=[owner_id] if scope=='personal' else []
    if db.execute('SELECT 1 FROM agent_skills WHERE '+clause+' AND name=? AND id!=?',args+[name,ident or '']).fetchone():
        raise HTTPException(409,'已有同名技能，请复用或使用不同的标准名称')


@lru_cache(maxsize=32)
def read_bundle(data_root,skill_id,version_id):
    # Versions are immutable. Reading an index row first makes edits visible across processes.
    directory=Path(data_root)/skill_id/version_id
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    package=directory/manifest['name']
    doc=(package/'SKILL.md').read_bytes().decode('utf-8')
    meta,text=parse_document(doc)
    return {'document':doc,'text':text,'allowed_tools':meta.get('allowed-tools','').split(),
            'resources':manifest['resources'],'package':str(package)}


def builtin():
    doc=(SYSTEM_DIR/'paper-quality'/'SKILL.md').read_bytes().decode('utf-8')
    meta,text=parse_document(doc)
    return {'name':meta['metadata']['title'],'description':meta['description'],'bindings':['向量与质量 · 质量评估'],
            'version':'quality-contribution-v1.1','text':text,'document':doc}


def initialize(db):
    db.executescript(SCHEMA)
    if 'instruction_version' not in {r['name'] for r in db.execute('PRAGMA table_info(agent_skills)')}:
        db.execute('ALTER TABLE agent_skills ADD COLUMN instruction_version TEXT')
    if db.execute("SELECT 1 FROM agent_skills WHERE id='quality'").fetchone():return
    original=builtin()
    saved=db.execute("SELECT value FROM app_settings WHERE name='prompts'").fetchone()
    text=(json.loads(saved['value']) if saved else {}).get('quality',original['text'])
    old_versions=db.execute("SELECT value FROM app_settings WHERE name='prompt_versions'").fetchone()
    revision=(json.loads(old_versions['value']) if old_versions else {}).get('quality',original['version'])
    doc=document_for('paper-quality',original['name'],original['description'],text)
    vid=uuid.uuid4().hex;stamp=now()
    write_bundle('quality',vid,'paper-quality',doc,{})
    db.execute("INSERT INTO agent_skills(id,name,title,description,scope,system_key,version_id,created_at,updated_at) VALUES('quality','paper-quality',?,?,'system','quality',?,?,?)",
               (original['name'],original['description'],vid,stamp,stamp))
    db.execute('INSERT INTO agent_skill_versions VALUES(?,?,?,?,?)',(vid,'quality',1,None,stamp))
    db.execute("UPDATE agent_skills SET instruction_version=? WHERE id='quality'",(revision,))


def record(ident):
    item=one('SELECT * FROM agent_skills WHERE id=?',(ident,))
    if not item:raise HTTPException(404,'技能不存在')
    return item


def authorized(item,user_id,admin=False,edit=False):
    own=item['scope']=='personal' and item['owner_id']==user_id
    public=item['scope']=='shared' and item['status']=='active'
    managed=admin and item['scope']!='personal'
    if not (own or managed or public and not edit):raise HTTPException(404,'技能不存在')


def summary(item):
    return {k:item[k] for k in ('id','name','title','description','scope','owner_id','status','enabled','revision','system_key','created_at','updated_at')}


def detail(ident,user_id=None,admin=False):
    item=record(ident);authorized(item,user_id,admin)
    data=read_bundle(str(root()),ident,item['version_id'])
    result={**summary(item),**{k:v for k,v in data.items() if k!='package'},
            'resources':{name:(Path(data['package'])/name).read_bytes().decode('utf-8') for name in data['resources']},
            'editable':bool(admin and item['scope']!='personal' or item['scope']=='personal' and item['owner_id']==user_id),
            'bindings':['向量与质量 · 质量评估'] if item['system_key']=='quality' else ['智能助手']}
    if item['system_key']=='quality':
        default=builtin()['text'];result.update(default=default,customized=data['text']!=default)
    if item['status']=='pending' and admin:
        proposer=one('SELECT username FROM users WHERE id=?',(item['owner_id'],))
        result['proposer_name']=proposer['username'] if proposer else '已删除用户'
    return result


def catalog(user_id,admin=False,scope='all',query='',page=1,page_size=20):
    clause="scope!='personal'" if admin else "(scope='personal' AND owner_id=?)"
    args=[] if admin else [user_id]
    if scope=='pending':clause+=" AND status='pending'"
    elif scope in ('system','shared','personal'):clause+=' AND scope=? AND status!=\'pending\'';args.append(scope)
    if query:
        clause+=' AND (title LIKE ? OR name LIKE ? OR description LIKE ?)';args.extend(['%'+query[:100]+'%']*3)
    with connect() as db:
        total=db.execute('SELECT COUNT(*) FROM agent_skills WHERE '+clause,args).fetchone()[0]
        items=[dict(r) for r in db.execute('SELECT * FROM agent_skills WHERE '+clause+' ORDER BY updated_at DESC,id LIMIT ? OFFSET ?',args+[page_size,(page-1)*page_size])]
    return {'items':[summary(i) for i in items],'total':total,'page':page,'page_size':page_size}


def system_instruction(key):
    item=one('SELECT id,version_id,revision,enabled,instruction_version FROM agent_skills WHERE system_key=?',(key,))
    if not item:return None
    return {**read_bundle(str(root()),item['id'],item['version_id']),'revision':item['instruction_version'] or str(item['revision']),'enabled':bool(item['enabled'])}


def _persist(db,item,body,user_id):
    if body.document is None:
        old=read_bundle(str(root()),item['id'],item['version_id'])
        meta,_=parse_document(old['document'])
        meta={**meta,'name':body.name,'description':body.description,'metadata':{**meta.get('metadata',{}),'title':body.title}}
        if body.allowed_tools:meta['allowed-tools']=' '.join(body.allowed_tools)
        else:meta.pop('allowed-tools',None)
        body=body.model_copy(update={'document':'---\n'+yaml.safe_dump(meta,allow_unicode=True,sort_keys=False)+'---\n'+body.text})
    meta,text,doc,resources=validate_edit(body)
    if body.expected_revision is not None and body.expected_revision!=item['revision']:
        raise HTTPException(409,'技能已更新，请刷新后再保存')
    if item['system_key'] and meta['name']!=item['name']:raise HTTPException(400,'系统技能的标准名称不可修改')
    if item['status']=='active':ensure_unique(db,meta['name'],item['scope'],item['owner_id'],item['id'])
    vid=uuid.uuid4().hex;rev=item['revision']+1;stamp=now()
    write_bundle(item['id'],vid,meta['name'],doc,resources)
    db.execute('UPDATE agent_skills SET name=?,title=?,description=?,revision=?,version_id=?,updated_at=? WHERE id=?',
               (meta['name'],meta.get('metadata',{}).get('title',meta['name']),meta['description'],rev,vid,stamp,item['id']))
    db.execute('INSERT INTO agent_skill_versions VALUES(?,?,?,?,?)',(vid,item['id'],rev,user_id,stamp))
    db.execute('UPDATE agent_skills SET instruction_version=? WHERE id=?',('skill:'+vid,item['id']))
    if item['system_key']=='quality':
        # Keep old clients and prompt task snapshots compatible; the actual loader reads SKILL.md.
        saved=db.execute("SELECT value FROM app_settings WHERE name='prompts'").fetchone()
        values=json.loads(saved['value']) if saved else {}
        if text==builtin()['text']:values.pop('quality',None)
        else:values['quality']=text
        db.execute("INSERT INTO app_settings VALUES('prompts',?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(dumps(values),stamp))


def save(ident,body,user_id,admin=False):
    import sqlite3
    try:
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            item=db.execute('SELECT * FROM agent_skills WHERE id=?',(ident,)).fetchone()
            if not item:raise HTTPException(404,'技能不存在')
            authorized(item,user_id,admin,edit=True)
            _persist(db,dict(item),body,user_id)
    except sqlite3.IntegrityError:raise HTTPException(409,'已有同名技能，请使用不同的标准名称')
    event('skill','技能已更新',user_id=user_id,skill_id=ident)
    return detail(ident,user_id,admin)


def create(body,user_id,shared=False):
    import sqlite3
    meta,text,doc,resources=validate_edit(body)
    ident='sk_'+uuid.uuid4().hex;vid=uuid.uuid4().hex;stamp=now()
    try:
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            count=db.execute("SELECT COUNT(*) FROM agent_skills WHERE scope=? AND (? OR owner_id=?)",('shared' if shared else 'personal',int(shared),user_id)).fetchone()[0]
            if count>=(100 if shared else 30):raise HTTPException(400,'技能数量已达到上限，请整理后再创建')
            ensure_unique(db,meta['name'],'shared' if shared else 'personal',user_id)
            write_bundle(ident,vid,meta['name'],doc,resources)
            db.execute('INSERT INTO agent_skills(id,name,title,description,scope,owner_id,version_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                       (ident,meta['name'],meta.get('metadata',{}).get('title',meta['name']),meta['description'],'shared' if shared else 'personal',None if shared else user_id,vid,stamp,stamp))
            db.execute('INSERT INTO agent_skill_versions VALUES(?,?,?,?,?)',(vid,ident,1,user_id,stamp))
    except sqlite3.IntegrityError:raise HTTPException(409,'已有同名技能，请复用或修改它')
    event('skill','技能已创建并启用',user_id=user_id,skill_id=ident,scope='shared' if shared else 'personal')
    return detail(ident,user_id,shared)


def set_enabled(ident,enabled,user_id,admin=False):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE');item=db.execute('SELECT * FROM agent_skills WHERE id=?',(ident,)).fetchone()
        if not item:raise HTTPException(404,'技能不存在')
        authorized(item,user_id,admin,edit=True)
        if item['status']!='active':raise HTTPException(400,'请先审核技能')
        db.execute('UPDATE agent_skills SET enabled=?,updated_at=? WHERE id=?',(int(enabled),now(),ident))
    event('skill','技能已启用' if enabled else '技能已停用',user_id=user_id,skill_id=ident)
    return detail(ident,user_id,admin)


def propose(ident,user_id):
    original=record(ident);authorized(original,user_id,edit=True)
    data=detail(ident,user_id)
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        pending=db.execute("SELECT id FROM agent_skills WHERE source_id=? AND status='pending'",(ident,)).fetchone()
        if pending:return {'id':pending['id'],'status':'pending','title':data['title']}
        if db.execute("SELECT COUNT(*) FROM agent_skills WHERE scope='shared' AND status!='active' AND owner_id=?",(user_id,)).fetchone()[0]>=30:
            raise HTTPException(400,'共享提议较多，请等待管理员处理')
        item='sk_'+uuid.uuid4().hex;vid=uuid.uuid4().hex;stamp=now()
        write_bundle(item,vid,data['name'],data['document'],data['resources'])
        db.execute("INSERT INTO agent_skills(id,name,title,description,scope,owner_id,status,enabled,version_id,source_id,created_at,updated_at) VALUES(?,?,?,?,'shared',?,'pending',0,?,?,?,?)",
                   (item,data['name'],data['title'],data['description'],user_id,vid,ident,stamp,stamp))
        db.execute('INSERT INTO agent_skill_versions VALUES(?,?,?,?,?)',(vid,item,1,user_id,stamp))
    event('skill','已提交共享技能提议',user_id=user_id,skill_id=item)
    return {'id':item,'status':'pending','title':data['title']}


def review(ident,approve,user_id):
    import sqlite3
    try:
        with connect() as db:
            db.execute('BEGIN IMMEDIATE');item=db.execute('SELECT * FROM agent_skills WHERE id=?',(ident,)).fetchone()
            if not item or item['scope']!='shared' or item['status']!='pending':raise HTTPException(409,'提议已处理或不存在')
            if approve and db.execute("SELECT COUNT(*) FROM agent_skills WHERE scope='shared' AND status='active'").fetchone()[0]>=100:
                raise HTTPException(400,'共享技能较多，请先整理现有技能')
            db.execute('UPDATE agent_skills SET status=?,enabled=?,owner_id=?,updated_at=? WHERE id=?',
                       ('active' if approve else 'rejected',int(approve),None if approve else item['owner_id'],now(),ident))
    except sqlite3.IntegrityError:raise HTTPException(409,'已有同名系统或共享技能，请先修改标准名称')
    event('skill','共享技能已批准' if approve else '共享技能已拒绝',user_id=user_id,skill_id=ident)
    return detail(ident,user_id,True)


def versions(ident,user_id,admin=False):
    item=record(ident);authorized(item,user_id,admin)
    return rows('SELECT revision,created_at FROM agent_skill_versions WHERE skill_id=? ORDER BY revision DESC LIMIT 100',(ident,))


def version_detail(ident,revision,user_id,admin=False):
    item=record(ident);authorized(item,user_id,admin)
    old=one('SELECT id FROM agent_skill_versions WHERE skill_id=? AND revision=?',(ident,revision))
    if not old:raise HTTPException(404,'版本不存在')
    data=read_bundle(str(root()),ident,old['id'])
    return {'text':data['text'],'document':data['document'],'resources':data['resources'],'revision':revision}


def restore(ident,revision,user_id,admin=False):
    item=record(ident);authorized(item,user_id,admin,edit=True)
    old=one('SELECT id FROM agent_skill_versions WHERE skill_id=? AND revision=?',(ident,revision))
    if not old:raise HTTPException(404,'版本不存在')
    data=read_bundle(str(root()),ident,old['id']);meta,text=parse_document(data['document'])
    return save(ident,SkillEdit(name=meta['name'],title=meta.get('metadata',{}).get('title',meta['name']),description=meta['description'],
                text=text,document=data['document'],resources={name:(Path(data['package'])/name).read_bytes().decode('utf-8') for name in data['resources']},expected_revision=item['revision']),user_id,admin)


def remove_files(ids):
    target_root=root().resolve()
    for ident in ids:
        if not re.fullmatch(r'sk_[a-f0-9]{32}',ident):continue
        path=(target_root/ident).resolve()
        if path.parent!=target_root:raise ValueError('技能目录越界')
        shutil.rmtree(path,ignore_errors=True)
    read_bundle.cache_clear()


def delete(ident,user_id,admin=False):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE');item=db.execute('SELECT * FROM agent_skills WHERE id=?',(ident,)).fetchone()
        if not item:raise HTTPException(404,'技能不存在')
        authorized(item,user_id,admin,edit=True)
        if item['system_key']:raise HTTPException(400,'已绑定流水线的系统技能不能删除，可以停用或恢复默认')
        db.execute('DELETE FROM agent_skills WHERE id=?',(ident,))
    remove_files([ident]);event('skill','技能已删除',user_id=user_id,skill_id=ident)
    return {'ok':True}


def available(user_id,query='',limit=20,*,include_disabled=False):
    # Only compact metadata is discovered. No folder walks or model calls.
    clause="status='active' AND ((scope='shared' AND enabled=1) OR (scope='personal' AND owner_id=?"+('' if include_disabled else ' AND enabled=1')+'))'
    args=[user_id]
    if query:clause+=' AND (title LIKE ? OR description LIKE ? OR name LIKE ?)';args.extend(['%'+query[:100]+'%']*3)
    return rows('SELECT id,name,title,description,revision,scope,enabled FROM agent_skills WHERE '+clause+' ORDER BY scope,name LIMIT ?',args+[limit])


def inspect(ident,user_id):
    """Read the latest editable instructions without activating or pinning a version."""
    item=record(ident);authorized(item,user_id)
    if item['scope']=='shared' and (item['status']!='active' or not item['enabled']):
        raise HTTPException(404,'技能不存在')
    data=read_bundle(str(root()),ident,item['version_id'])
    return {**{k:item[k] for k in ('id','name','title','description','scope','enabled','revision')},
            'text':data['text'],'allowed_tools':data['allowed_tools'],'resources':list(data['resources'])}


def activate(ident,user_id,session_id):
    item=record(ident);authorized(item,user_id)
    if not item['enabled'] or item['status']!='active' or item['scope']=='system':raise HTTPException(400,'技能当前不可用于智能助手')
    with connect() as db:
        if not db.execute('SELECT 1 FROM chat_sessions WHERE id=? AND user_id=?',(session_id,user_id)).fetchone():raise HTTPException(404,'会话不存在')
        count=db.execute('SELECT COUNT(*) FROM chat_session_skills WHERE session_id=?',(session_id,)).fetchone()[0]
        exists=db.execute('SELECT 1 FROM chat_session_skills WHERE session_id=? AND skill_id=?',(session_id,ident)).fetchone()
        if count>=4 and not exists:raise HTTPException(400,'每个会话最多同时加载 4 个技能')
        active=[dict(r) for r in db.execute('SELECT s.* FROM agent_skills s JOIN chat_session_skills c ON c.skill_id=s.id WHERE c.session_id=? AND s.id!=?',(session_id,ident))]
        if sum(len(activation_content(s)['instructions']) for s in active)+len(activation_content(item)['instructions'])>24000:
            raise HTTPException(400,'本会话的技能说明已较长，请使用新会话加载')
        db.execute('INSERT OR IGNORE INTO chat_session_skills VALUES(?,?)',(session_id,ident))
    return activation_content(item)


def activation_content(item):
    frozen=_loaded.get()
    if frozen is not None and item['id'] in frozen:item=frozen[item['id']]
    elif frozen is not None:frozen[item['id']]=dict(item)
    data=read_bundle(str(root()),item['id'],item['version_id'])
    return {'id':item['id'],'title':item['title'],'name':item['name'],'revision':item['revision'],
            'instructions':data['text'],'resources':list(data['resources']),'allowed_tools':data['allowed_tools']}


def session_context(session_id,user_id):
    items=rows("SELECT s.* FROM agent_skills s JOIN chat_session_skills c ON c.skill_id=s.id JOIN chat_sessions t ON t.id=c.session_id WHERE c.session_id=? AND t.user_id=? AND s.enabled=1 AND s.status='active' AND (s.scope='shared' OR (s.scope='personal' AND s.owner_id=?)) ORDER BY s.id LIMIT 4",(session_id,user_id,user_id))
    return [activation_content(item) for item in items]


def resource(ident,path,user_id,session_id):
    item=record(ident);authorized(item,user_id)
    if not item['enabled'] or not one('SELECT 1 FROM chat_session_skills c JOIN chat_sessions s ON s.id=c.session_id WHERE c.session_id=? AND c.skill_id=? AND s.user_id=?',(session_id,ident,user_id)):
        raise HTTPException(400,'请先加载技能')
    item=(_loaded.get() or {}).get(ident,item)
    data=read_bundle(str(root()),ident,item['version_id'])
    if path not in data['resources']:raise HTTPException(404,'参考资料不存在')
    return {'path':path,'text':(Path(data['package'])/path).read_bytes().decode('utf-8')}
