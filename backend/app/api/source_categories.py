import asyncio
import httpx
from fastapi import APIRouter, Depends, HTTPException
import xml.etree.ElementTree as ET
from ..auth import admin_user
from ..db import rows,connect
from ..source_catalog import SourceInput, ArxivBatchInput, add_arxiv_sources, official_categories, venue_catalog, registry, save_source, validate_source,invalidate
from ..pipeline.fetch import HEADERS, parse_conference_feed
from ..logs import event
from pydantic import BaseModel, Field
from ..source_deletion import deletion_impact,deletion_impact_many
from .. import source_deletion_queue

router=APIRouter(prefix='/api/admin/source-categories',dependencies=[Depends(admin_user)])

@router.get('')
def sources():
    counts={r['category_key']:r['n'] for r in rows('SELECT category_key,COUNT(*) n FROM paper_categories GROUP BY category_key')}
    return [{**s,'paper_count':counts.get(s['key'].casefold(),0)} for s in registry()]

@router.get('/catalog')
def official():return official_categories()

@router.get('/venues')
def conferences():return venue_catalog()

@router.post('/test')
async def test_source(body:SourceInput):
    data=validate_source(body)
    if body.kind=='arxiv':return {'ok':True,'code':body.code}
    try:
        async with httpx.AsyncClient(timeout=45,headers=HEADERS,follow_redirects=False) as client:
            response=await client.get(data['feed_url'])
            response.raise_for_status()
        year,papers=await asyncio.to_thread(parse_conference_feed,response.content,body.code)
    except (httpx.HTTPError,ET.ParseError,ValueError) as error:
        raise HTTPException(400,'来源校验未通过：'+(str(error)[:200] if isinstance(error,ValueError) else type(error).__name__))
    return {'ok':True,'year':year,'count':len(papers),'titles':[p['title'] for p in papers[:3]]}

@router.post('')
def create(body:SourceInput):
    result=save_source(body);event('admin','新增分类来源',source_key=result['key'])
    return result


@router.post('/arxiv/batch')
def create_arxiv_many(body:ArxivBatchInput,admin=Depends(admin_user)):
    result=add_arxiv_sources(body)
    event('admin','批量新增 arXiv 分类',user_id=admin['id'],source_keys=result['created'],skipped_keys=result['skipped'],
          fetch_enabled=body.fetch_enabled,guest_default=body.guest_default)
    return result


class SourceSelection(BaseModel):
    keys:list[str]=Field(min_length=1)

class SourceBatchUpdate(SourceSelection):
    fetch_enabled:bool|None=None
    guest_default:bool|None=None

class SourceBatchDelete(SourceSelection):
    confirm_text:str=Field(max_length=80)
    delete_papers:int=Field(ge=0)
    keep_papers:int=Field(ge=0)

@router.patch('/batch')
def update_many(body:SourceBatchUpdate,admin=Depends(admin_user)):
    fields=[(name,getattr(body,name)) for name in ('fetch_enabled','guest_default') if getattr(body,name) is not None]
    if not fields:raise HTTPException(400,'请选择要修改的配置')
    keys=sorted(set(body.keys))
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TEMP TABLE selected_sources(key TEXT PRIMARY KEY)')
        db.executemany('INSERT INTO selected_sources VALUES(?)',[(k,) for k in keys])
        if db.execute('SELECT key FROM source_deletion_sources WHERE key IN (SELECT key FROM selected_sources) LIMIT 1').fetchone():raise HTTPException(409,'所选分类正在后台删除，暂时不能修改')
        if db.execute('SELECT COUNT(*) FROM source_categories WHERE key IN (SELECT key FROM selected_sources)').fetchone()[0]!=len(keys):raise HTTPException(404,'部分分类已不存在，请刷新列表')
        db.execute('UPDATE source_categories SET '+','.join(name+'=?' for name,_ in fields)+' WHERE key IN (SELECT key FROM selected_sources)',[int(value) for _,value in fields])
    invalidate();event('admin','批量修改分类配置',user_id=admin['id'],source_keys=keys,fields=[name for name,_ in fields])
    return {'updated':keys}

@router.post('/batch/impact')
def batch_impact(body:SourceSelection):return deletion_impact_many(body.keys)

@router.get('/deletions')
def deletion_jobs():return source_deletion_queue.list_jobs()

@router.post('/deletions/{ident}/retry')
def retry_deletion(ident:int):return source_deletion_queue.retry(ident)

@router.delete('/batch',status_code=202)
def batch_remove(body:SourceBatchDelete,admin=Depends(admin_user)):
    return source_deletion_queue.enqueue(body.keys,admin['id'],body)

@router.patch('/{key}')
def update(key:str,body:SourceInput):
    result=save_source(body,key);event('admin','修改分类来源',source_key=key,enabled=body.enabled,fetch_enabled=body.fetch_enabled)
    return result

@router.get('/{key}/impact')
def impact(key:str):return deletion_impact(key)

class DeleteSource(BaseModel):
    confirm_code:str=Field(max_length=40)
    delete_papers:int=Field(ge=0)
    keep_papers:int=Field(ge=0)

@router.delete('/{key}',status_code=202)
def remove(key:str,body:DeleteSource,admin=Depends(admin_user)):
    return source_deletion_queue.enqueue([key],admin['id'],body)
