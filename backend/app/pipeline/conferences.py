"""Light daily edition checks, weekly content reconciliation, and cached dates."""
import asyncio
import hashlib
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from ..background_load import yield_to_web
from ..config import now
from ..db import connect, dumps, execute, one
from ..logs import event
from ..paper_dates import refresh_batch
from ..source_catalog import sources_to_fetch

WEEK = timedelta(days=7)
MONTHS = {name: i for i,name in enumerate(('jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'),1)}


def recent(stamp):
    if not stamp:return False
    try:
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=timezone.utc)
        age=datetime.now(timezone.utc)-parsed
        return timedelta(0)<=age<WEEK
    except ValueError:return False


def latest_editions(html, codes):
    wanted={code.casefold():code for code in codes};result={}
    for link in BeautifulSoup(html,'html.parser').find_all('a',href=True):
        path=urlparse(link['href']).path
        match=re.fullmatch(r'/venue/([A-Za-z][A-Za-z0-9_-]*)\.(\d{4})',path)
        if match and match[1].casefold() in wanted:
            code=wanted[match[1].casefold()]
            result[code]=max(result.get(code,0),int(match[2]))
    return result


def calendar_url(venue, year):
    host={'icml':'icml.cc','iclr':'iclr.cc','neurips':'nips.cc'}.get(venue.casefold())
    if host:return f'https://{host}/Conferences/{year}/Dates'
    if venue.casefold()=='aaai':return 'https://ojs.aaai.org/index.php/AAAI/index'
    return None


def parse_calendar_month(html, venue, year):
    soup=BeautifulSoup(html,'html.parser')
    if venue.casefold()=='aaai':
        text=soup.get_text(' ',strip=True)
        # Use the edition's conference date sentence, never an issue's publication
        # date for all papers (different issues can have different dates).
        match=re.search(rf'AAAI Conference on Artificial Intelligence was held on ([^.]*\b{year}\b[^.]*)',text,re.I)
        if not match:return None
        text=match[1]
    else:
        if not soup.find(string=re.compile(rf'\b{re.escape(venue)}\s+{year}\s+Meeting Dates\b',re.I)):return None
        heading=soup.find(string=re.compile(r'^\s*(?:Main Conference|Conference Sessions)\s*$',re.I))
        if not heading:return None
        element=heading.find_parent('tr') or heading.find_parent('p') or heading.parent
        text=element.get_text(' ',strip=True)
    match=re.search(r'\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b',text,re.I)
    return f'{year}-{MONTHS[match[1][:3].lower()]:02}' if match else None


def save_calendar(source, year, month, url):
    with connect(background=True) as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM source_categories WHERE key=? AND fetch_enabled=1',(source['key'],)).fetchone():return []
        previous=db.execute('SELECT date_month,dates_ready FROM conference_editions WHERE venue=? AND year=?',(source['code'],year)).fetchone()
        # A temporary network/parse failure must not erase a previously known month.
        month=month or (previous['date_month'] if previous else None)
        ready=int(not month or bool(previous and previous['date_month']==month and previous['dates_ready']))
        db.execute('INSERT INTO conference_editions VALUES(?,?,?,?,?,?,?) ON CONFLICT(venue,year) DO UPDATE SET date_month=excluded.date_month,source_url=COALESCE(excluded.source_url,source_url),checked_at=excluded.checked_at,dates_ready=excluded.dates_ready',
                   (source['key'],source['code'],year,month,url,now(),ready))
        if ready:return []
        return [r[0] for r in db.execute("SELECT id FROM papers WHERE (venue=? COLLATE NOCASE OR venue=? COLLATE NOCASE) AND venue_year=?",(source['code'],f"{source['code']}.{year}",year))]


def refresh_dates(source, ids):
    with connect(background=True) as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM source_categories WHERE key=? AND fetch_enabled=1',(source['key'],)).fetchone():return 0
        return refresh_batch(db,ids)


async def ensure_calendar(client, source, year):
    cached=await asyncio.to_thread(one,'SELECT * FROM conference_editions WHERE venue=? AND year=?',(source['code'],year))
    if cached and cached['dates_ready'] and (cached['date_month'] or recent(cached['checked_at'])):return
    url=calendar_url(source['code'],year);month=None
    if cached and cached['date_month']:
        month=cached['date_month'];url=cached['source_url']
    elif url:
        try:
            response=await client.get(url,timeout=15)
            response.raise_for_status()
            month=await asyncio.to_thread(parse_calendar_month,response.text,source['code'],year)
        except httpx.HTTPError as error:
            event('fetch','会议月份暂未取得，保留已有日期精度',job='fetch_conf',venue=source['code'],year=year,error=type(error).__name__)
    ids=await finish_commit(save_calendar,source,year,month,url)
    for offset in range(0,len(ids),100):
        await finish_commit(refresh_dates,source,ids[offset:offset+100])
        await yield_to_web()
    await finish_commit(execute,'UPDATE conference_editions SET dates_ready=1 WHERE venue=? AND year=? AND source_key IN (SELECT key FROM source_categories WHERE fetch_enabled=1)',(source['code'],year))
    event('fetch','会议日期元数据已检查',job='fetch_conf',venue=source['code'],year=year,month=month,source=url,updated=len(ids))


async def finish_commit(function, *args):
    task=asyncio.create_task(asyncio.to_thread(function,*args))
    try:return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def digest(paper):
    # Generated Atom updated timestamps intentionally never enter this signature.
    return hashlib.sha256(dumps({k:paper[k] for k in ('source_id','title','abstract','authors','venue','venue_year','published','abs_url','pdf_url')}).encode()).hexdigest()


def reconcile_batch(papers, source):
    from .fetch import upsert_paper, arxiv_identity
    signatures={p['source_id']:digest(p) for p in papers}
    placeholders=','.join('?' for _ in signatures)
    with connect(background=True) as db:
        if not db.execute('SELECT 1 FROM source_categories WHERE key=? AND fetch_enabled=1',(source['key'],)).fetchone():return None
        cached={r['source_id']:r['digest'] for r in db.execute('SELECT source_id,digest FROM conference_entry_versions WHERE source_key=? AND source_id IN ('+placeholders+')',[source['key'],*signatures])}
        pending=[p for p in papers if cached.get(p['source_id'])!=signatures[p['source_id']]]
        if not pending:return 0,0
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM source_categories WHERE key=? AND fetch_enabled=1',(source['key'],)).fetchone():return None
        pending_ids=[p['source_id'] for p in pending]
        marks=','.join('?' for _ in pending_ids)
        retired={r[0] for r in db.execute('SELECT source_id FROM retired_paper_sources WHERE source_id IN ('+marks+')',pending_ids)}
        existing={r['source_id']:r for r in db.execute('SELECT s.source_id,p.arxiv_id,p.title,p.abstract,p.authors,p.venue,p.venue_year,p.abs_url,p.pdf_url,p.published,p.primary_category,p.categories FROM papers p JOIN paper_sources s ON s.paper_id=p.id WHERE s.source_id IN ('+marks+')',pending_ids)}
        added=changed=0;versions=[]
        for paper in pending:
            if paper['source_id'] in retired:continue
            old=existing.get(paper['source_id'])
            same=bool(old) and all(old[k]==paper[k] for k in ('title','abstract','venue','venue_year','abs_url')) and old['authors']==dumps(paper['authors']) and (old['pdf_url']==paper['pdf_url'] or not paper['pdf_url'])
            same=same and (bool(arxiv_identity(old['arxiv_id'])[0]) or (old['published']==paper['published'] and old['primary_category'] is None and old['categories']=='[]'))
            if not same:
                added+=int(upsert_paper(paper,db));changed+=1
            versions.append((source['key'],paper['source_id'],signatures[paper['source_id']]))
        db.executemany('INSERT INTO conference_entry_versions VALUES(?,?,?) ON CONFLICT(source_key,source_id) DO UPDATE SET digest=excluded.digest',versions)
        return added,changed


def save_sync(source, year, response):
    with connect(background=True) as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM source_categories WHERE key=? AND fetch_enabled=1',(source['key'],)).fetchone():
            old=db.execute('SELECT etag,modified FROM conference_sync WHERE venue=?',(source['code'],)).fetchone() if response.status_code==304 else None
            db.execute('INSERT INTO conference_sync(venue,etag,modified,year,checked_at) VALUES(?,?,?,?,?) ON CONFLICT(venue) DO UPDATE SET etag=excluded.etag,modified=excluded.modified,year=excluded.year,checked_at=excluded.checked_at',
                       (source['code'],response.headers.get('etag') or (old['etag'] if old else None),response.headers.get('last-modified') or (old['modified'] if old else None),year,now()))


async def fetch_conferences(force=True):
    from .fetch import HEADERS, parse_conference_feed
    added=processed=0;errors=[]
    sources=await asyncio.to_thread(sources_to_fetch,'venue')
    if not sources:return 0
    async with httpx.AsyncClient(timeout=90,follow_redirects=True,headers=HEADERS) as client:
        latest={}
        if not force:
            try:
                response=await client.get('https://papers.cool/',timeout=20)
                response.raise_for_status()
                latest=await asyncio.to_thread(latest_editions,response.text,[s['code'] for s in sources])
                event('fetch','已轻量检查会议届次',job='fetch_conf',editions=latest,bytes=len(response.content))
            except httpx.HTTPError as error:
                # Do not turn a failed lightweight probe into 44 MB of downloads.
                event('fetch','会议届次检查失败',level='error',job='fetch_conf',error=type(error).__name__)
                raise RuntimeError('会议届次检查失败，将在下次运行重试') from error
        for source in sources:
            venue=source['code']
            if venue not in {s['code'] for s in await asyncio.to_thread(sources_to_fetch,'venue')}:continue
            previous=await asyncio.to_thread(one,'SELECT * FROM conference_sync WHERE venue=?',(venue,))
            if not force and previous and latest.get(venue,previous['year'])==previous['year'] and recent(previous['checked_at']):
                await ensure_calendar(client,source,previous['year'])
                event('fetch','届次未变，跳过整届 Feed' if venue in latest else '首页未列出会议，按每周核对',job='fetch_conf',venue=venue,year=previous['year'],next_full_check='上次成功核对后 7 天')
                continue
            headers={}
            if previous and latest.get(venue,previous['year'])==previous['year']:
                if previous['etag']:headers['If-None-Match']=previous['etag']
                if previous['modified']:headers['If-Modified-Since']=previous['modified']
            started=time.perf_counter()
            try:
                response=await client.get(source['feed_url'],headers=headers)
                if response.status_code==304 and previous:
                    await ensure_calendar(client,source,previous['year'])
                    await finish_commit(save_sync,source,previous['year'],response)
                    event('fetch','会议来源未变化',job='fetch_conf',venue=venue,year=previous['year'])
                    continue
                response.raise_for_status()
                year,papers=await asyncio.to_thread(parse_conference_feed,response.content,venue)
                if (previous and year<previous['year']) or latest.get(venue,year)>year:
                    raise ValueError('Feed 届次落后，未覆盖同步位置')
                await ensure_calendar(client,source,year)
                changed=0;last_progress=time.monotonic()
                event('fetch','已读取最新一届会议 Feed',job='fetch_conf',venue=venue,year=year,papers=len(papers),bytes=len(response.content),seconds=round(time.perf_counter()-started,2))
                for offset in range(0,len(papers),100):
                    batch=papers[offset:offset+100]
                    result=await finish_commit(reconcile_batch,batch,source)
                    if result is None:break
                    added+=result[0];changed+=result[1];processed+=len(batch)
                    if time.monotonic()-last_progress>=.5 or offset+100>=len(papers):
                        await finish_commit(execute,"INSERT INTO source_status(name,progress) VALUES('fetch_conf',?) ON CONFLICT(name) DO UPDATE SET progress=excluded.progress",(dumps({'processed':processed}),))
                        last_progress=time.monotonic()
                    await yield_to_web(.02 if result[1] else 0)
                else:
                    await finish_commit(save_sync,source,year,response)
                    event('fetch','会议内容核对完成',job='fetch_conf',venue=venue,year=year,processed=len(papers),changed=changed,added=added,seconds=round(time.perf_counter()-started,2))
            except (httpx.HTTPError,ValueError,ET.ParseError) as error:
                errors.append(f'{venue}: {type(error).__name__}: {str(error)[:150]}')
                event('fetch','会议 Feed 同步失败',level='error',job='fetch_conf',venue=venue,error=str(error)[:200])
    if errors:raise RuntimeError('；'.join(errors))
    return added
