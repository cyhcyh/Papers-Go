"""The administrator-controlled source registry shared by UI and pipelines."""
import json
import time
import threading
from pathlib import Path
from functools import lru_cache
from urllib.parse import urlparse
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel, Field
from .config import settings, now
from .db import rows, connect
from .disciplines import Discipline, LABELS

DEFAULT_ARXIV=[('cs.AI','人工智能'),('cs.LG','机器学习'),('cs.CL','自然语言处理'),('cs.CV','计算机视觉'),('cs.MA','多智能体'),('cs.NE','神经与进化计算'),('cs.RO','机器人'),('stat.ML','统计机器学习'),('math.CO','组合数学')]
DEFAULT_VENUES=['ICML','AAAI','NeurIPS','ICLR']
ADDITIONAL_AI_VENUES=['ACL','COLM','COLT','CoRL','CVPR','ECCV','EMNLP','ICCV','IJCAI','INTERSPEECH','IWSLT','MICCAI','MLSYS','NAACL','UAI']
VENUE_CATALOG={
    'ICML':{'label':'International Conference on Machine Learning','discipline':'Computer Science'},
    'AAAI':{'label':'AAAI Conference on Artificial Intelligence','discipline':'Computer Science'},
    'NeurIPS':{'label':'Conference on Neural Information Processing Systems','discipline':'Computer Science'},
    'ICLR':{'label':'International Conference on Learning Representations','discipline':'Computer Science'},
    'ACL':{'label':'Annual Meeting of the Association for Computational Linguistics','discipline':'Computer Science'},
    'COLM':{'label':'Conference on Language Modeling','discipline':'Computer Science'},
    'COLT':{'label':'Conference on Learning Theory','discipline':'Computer Science'},
    'CoRL':{'label':'Conference on Robot Learning','discipline':'Computer Science'},
    'CVPR':{'label':'Conference on Computer Vision and Pattern Recognition','discipline':'Computer Science'},
    'ECCV':{'label':'European Conference on Computer Vision','discipline':'Computer Science'},
    'EMNLP':{'label':'Conference on Empirical Methods in Natural Language Processing','discipline':'Computer Science'},
    'ICCV':{'label':'International Conference on Computer Vision','discipline':'Computer Science'},
    'IJCAI':{'label':'International Joint Conference on Artificial Intelligence','discipline':'Computer Science'},
    'INTERSPEECH':{'label':'Conference of the International Speech Communication Association','discipline':'Computer Science'},
    'IWSLT':{'label':'International Conference on Spoken Language Translation','discipline':'Computer Science'},
    'MICCAI':{'label':'Medical Image Computing and Computer Assisted Intervention','discipline':'Computer Science'},
    'MLSYS':{'label':'Conference on Machine Learning and Systems','discipline':'Computer Science'},
    'NAACL':{'label':'Conference of the North American Chapter of the Association for Computational Linguistics','discipline':'Computer Science'},
    'UAI':{'label':'Conference on Uncertainty in Artificial Intelligence','discipline':'Computer Science'},
}
_cache={};_lock=threading.Lock()


@lru_cache(maxsize=1)
def official_categories():
    return json.loads((Path(__file__).parent/'resources/arxiv_categories.json').read_text(encoding='utf-8'))['categories']


def venue_catalog():
    return [{'code':code,**entry} for code,entry in VENUE_CATALOG.items()]


def _add_ai_venues(db):
    # Apply once: sources deleted by the administrator stay deleted on restart.
    if db.execute("SELECT 1 FROM app_migrations WHERE name='ai_venue_catalog_v1'").fetchone():return
    existing={r[0].casefold() for r in db.execute('SELECT key FROM source_categories')}
    position=db.execute('SELECT COALESCE(MAX(sort_order),-1)+1 FROM source_categories').fetchone()[0]
    for code in ADDITIONAL_AI_VENUES:
        if ('venue:'+code).casefold() in existing:continue
        db.execute('INSERT INTO source_categories(key,kind,code,label,enabled,fetch_enabled,guest_default,sort_order,feed_url,discipline,created_at) VALUES(?,?,?,?,1,0,0,?,?,?,?)',
                   ('venue:'+code,'venue',code,code,position,'https://papers.cool/venue/'+code+'/feed','Computer Science',now()))
        position+=1
    db.execute("INSERT INTO app_migrations VALUES('ai_venue_catalog_v1',?)",(now(),))
    invalidate()


def initialize(db):
    if 'discipline' not in {r['name'] for r in db.execute('PRAGMA table_info(source_categories)')}:
        db.execute('ALTER TABLE source_categories ADD COLUMN discipline TEXT')
    official={e['code']:e for e in official_categories()}
    for source in db.execute('SELECT key,kind,code FROM source_categories WHERE discipline IS NULL').fetchall():
        group=official.get(source['code'],{}).get('group','Computer Science') if source['kind']=='arxiv' else 'Computer Science'
        db.execute('UPDATE source_categories SET discipline=? WHERE key=?',(group,source['key']))
    if db.execute("SELECT 1 FROM app_migrations WHERE name='source_registry_v1'").fetchone():
        _add_ai_venues(db)
        return
    db.execute("INSERT INTO app_migrations VALUES('source_registry_v1',?)",(now(),))
    if db.execute('SELECT 1 FROM source_categories LIMIT 1').fetchone():
        _add_ai_venues(db)
    else:
        # Fresh installations start empty. Mark the upgrade as consumed so a
        # later restart cannot silently add conferences on the admin's behalf.
        db.execute("INSERT INTO app_migrations VALUES('ai_venue_catalog_v1',?)",(now(),))
    invalidate()


def invalidate():
    with _lock:_cache.clear()


def registry(enabled_only=False, guest=False):
    key=str(settings().data_dir.absolute())
    with _lock:
        saved=_cache.get(key)
        if not saved or time.monotonic()-saved[0]>.5:
            data=rows('SELECT s.*,d.job_id AS deletion_id FROM source_categories s LEFT JOIN source_deletion_sources d ON d.key=s.key ORDER BY s.sort_order,s.key')
            _cache[key]=(time.monotonic(),data)
        else:data=saved[1]
    official={e['code']:e for e in official_categories()}
    output=[]
    for s in data:
        if s.get('deletion_id') and (enabled_only or guest):continue
        if guest and not s['guest_default']:continue
        item=official.get(s['code'],{}) if s['kind']=='arxiv' else {}
        discipline=item.get('group') or s.get('discipline') or 'Computer Science'
        output.append({**s,'enabled':1,'discipline':discipline,'discipline_label':LABELS[discipline],
                       'label_en':item.get('label',s['label']),'label_zh':item.get('label_zh',s['label'])})
    return output


def supported_keys(enabled_only=True, guest=False):
    return {s['key'] for s in registry(enabled_only,guest)}


def sources_to_fetch(kind):
    return [s for s in registry(True) if s['kind']==kind and s['fetch_enabled']]


class SourceInput(BaseModel):
    kind: Literal['arxiv','venue']
    code: str = Field(min_length=2,max_length=40,pattern=r'^[A-Za-z][A-Za-z0-9._-]+$')
    label: str = Field(min_length=1,max_length=100)
    enabled: bool = True
    fetch_enabled: bool = True
    guest_default: bool = True
    sort_order: int = Field(100,ge=0,le=10000)
    feed_url: str | None = Field(None,max_length=500)
    standard_system: str | None = None
    discipline: Discipline | None = None


class ArxivBatchInput(BaseModel):
    codes: list[str] = Field(min_length=1)
    fetch_enabled: bool = True
    guest_default: bool = True
    sort_order: int = Field(100,ge=0,le=10000)


class VenueBatchItem(BaseModel):
    code: str = Field(min_length=2,max_length=40,pattern=r'^[A-Za-z][A-Za-z0-9._-]+$')
    label: str | None = Field(None,min_length=1,max_length=100)
    discipline: Discipline | None = None
    fetch_enabled: bool = True
    guest_default: bool = True
    sort_order: int = Field(100,ge=0,le=10000)


class VenueBatchInput(BaseModel):
    venues: list[VenueBatchItem] = Field(min_length=1)


def add_venue_sources(body: VenueBatchInput):
    catalog={entry['code'].casefold():entry for entry in venue_catalog()}
    selected={}
    for item in body.venues:
        entry=catalog.get(item.code.casefold())
        if not entry:raise HTTPException(400,'请选择目录中的会议')
        code=entry['code']
        if code in selected:continue
        selected[code]=validate_source(SourceInput(kind='venue',**{
            **item.model_dump(),'code':code,'label':item.label or code,
            'discipline':item.discipline or entry['discipline'],
        }))
    created,skipped=[],[]
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        deleting={row[0].casefold() for row in db.execute('SELECT key FROM source_deletion_sources')}
        if any(data['key'].casefold() in deleting for data in selected.values()):
            raise HTTPException(409,'所选会议正在后台删除，暂时不能重新添加')
        existing={row[0].casefold() for row in db.execute('SELECT key FROM source_categories')}
        for data in selected.values():
            key=data['key']
            if key.casefold() in existing:
                skipped.append(key);continue
            db.execute('INSERT INTO source_categories(key,kind,code,label,enabled,fetch_enabled,guest_default,sort_order,feed_url,discipline,created_at) VALUES(?,?,?,?,1,?,?,?,?,?,?)',
                       (key,'venue',data['code'],data['label'],int(data['fetch_enabled']),int(data['guest_default']),data['sort_order'],data['feed_url'],data['discipline'],now()))
            created.append(key)
    invalidate()
    return {'created':created,'skipped':skipped}


def add_arxiv_sources(body: ArxivBatchInput):
    official={s['code']:s for s in official_categories()}
    codes=list(dict.fromkeys(body.codes))
    if not set(codes)<=official.keys():raise HTTPException(400,'请选择 arXiv 官方分类代码')
    created,skipped=[],[]
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        existing={s[0].casefold() for s in db.execute('SELECT key FROM source_categories')}
        for code in codes:
            key='arxiv:'+code
            if key.casefold() in existing:
                skipped.append(key);continue
            item=official[code]
            db.execute('INSERT INTO source_categories(key,kind,code,label,enabled,fetch_enabled,guest_default,sort_order,discipline,created_at) VALUES(?,?,?, ?,1,?,?,?,?,?)',
                       (key,'arxiv',code,item.get('label_zh') or item['label'],int(body.fetch_enabled),int(body.guest_default),body.sort_order,item['group'],now()))
            created.append(key)
    invalidate()
    return {'created':created,'skipped':skipped}


def validate_source(body):
    data=body.model_dump();data['key']=body.kind+':'+body.code
    data['enabled']=True
    if body.kind=='arxiv':
        official=next((e for e in official_categories() if e['code']==body.code),None)
        if not official:raise HTTPException(400,'请选择 arXiv 官方分类代码')
        data['discipline']=official['group']
        data['feed_url']=None
        data['standard_system']=None
    else:
        # New conferences must come from the supplied catalog. Keep historical
        # custom records editable without adding them back to the catalog.
        if body.code not in VENUE_CATALOG and not rows('SELECT key FROM source_categories WHERE key=?',(data['key'],)):
            raise HTTPException(400,'请选择目录中的会议')
        data['discipline']=body.discipline or VENUE_CATALOG.get(body.code,{}).get('discipline','Computer Science')
        url=body.feed_url or 'https://papers.cool/venue/'+body.code+'/feed'
        parsed=urlparse(url)
        if parsed.scheme!='https' or parsed.hostname!='papers.cool' or parsed.username or parsed.password or parsed.port not in (None,443) or parsed.path!='/venue/'+body.code+'/feed' or parsed.query or parsed.fragment:raise HTTPException(400,'会议来源地址无效，请重新选择会议')
        data['feed_url']=url
        data['standard_system']=None
    return data


def save_source(body, previous_key=None):
    data=validate_source(body)
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if previous_key and previous_key!=data['key']:raise HTTPException(400,'分类类型和代码不能更换，请另建分类')
        if db.execute('SELECT key FROM source_deletion_sources WHERE key=? COLLATE NOCASE',(data['key'],)).fetchone():
            raise HTTPException(409,'此分类正在后台删除，暂时不能编辑或重新添加')
        exists=db.execute('SELECT key,fetch_enabled,guest_default FROM source_categories WHERE key=? COLLATE NOCASE',(data['key'],)).fetchone()
        if previous_key and not exists:raise HTTPException(404,'分类不存在')
        if not previous_key and exists:raise HTTPException(409,'该分类已经存在')
        names=['key','kind','code','label','enabled','fetch_enabled','guest_default','sort_order','feed_url','standard_system','discipline']
        if previous_key and body.kind=='venue' and body.discipline is None:
            data['discipline']=db.execute('SELECT discipline FROM source_categories WHERE key=?',(previous_key,)).fetchone()[0] or 'Computer Science'
        if previous_key:
            for field in ('fetch_enabled','guest_default'):
                if field not in body.model_fields_set:data[field]=bool(exists[field])
        values=[int(data[k]) if isinstance(data[k],bool) else data[k] for k in names]
        if previous_key:
            db.execute('UPDATE source_categories SET '+','.join(k+'=?' for k in names[3:])+' WHERE key=?',(*values[3:],previous_key))
        else:db.execute('INSERT INTO source_categories('+','.join(names)+',created_at) VALUES('+','.join('?' for _ in names)+',?)',(*values,now()))
    invalidate()
    return data
