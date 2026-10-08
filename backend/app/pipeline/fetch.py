import asyncio
import re
import time
import json
from contextlib import nullcontext
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from .. import arxiv_client
from urllib.parse import urlparse, unquote, quote
from ..source_catalog import registry, sources_to_fetch, DEFAULT_VENUES
import httpx
from bs4 import BeautifulSoup
from ..config import settings, now, today
from ..db import connect, one, rows, dumps, execute
from ..logs import event

ARXIV = re.compile(r'(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v(\d+))?')
HEADERS = {'User-Agent': 'Shualunwen/1.0 (local academic paper discovery)'}

class SourceRemoved(Exception):pass


def arxiv_identity(value):
    match = ARXIV.search(value or '')
    return (match[1], int(match[2] or 1)) if match else (None, 1)


def parse_atom(content):
    root = ET.fromstring(content)
    if root.tag != '{http://www.w3.org/2005/Atom}feed':
        raise ValueError('arXiv 返回的内容不是 Atom 论文列表')
    ns = {'a': 'http://www.w3.org/2005/Atom', 'x': 'http://arxiv.org/schemas/atom'}
    output = []
    for entry in root.findall('a:entry', ns):
        identity, version = arxiv_identity(entry.findtext('a:id', '', ns))
        if not identity:
            raise ValueError('arXiv 返回无效条目：' + entry.findtext('a:summary', '', ns)[:200])
        links = entry.findall('a:link', ns)
        category = entry.find('x:primary_category', ns)
        output.append({'arxiv_id': identity, 'arxiv_version': version,
           'title': ' '.join(entry.findtext('a:title', '', ns).split()),
           'abstract': ' '.join(entry.findtext('a:summary', '', ns).split()),
           'authors': [a.findtext('a:name', '', ns) for a in entry.findall('a:author', ns)],
           'published': entry.findtext('a:published', '', ns)[:10],
           'primary_category': category.get('term') if category is not None else 'cs.AI',
           'categories': [c.get('term') for c in entry.findall('a:category', ns) if c.get('term')],
           'abs_url': 'https://arxiv.org/abs/' + identity,
           'pdf_url': next((l.get('href') for l in links if l.get('title') == 'pdf'), 'https://arxiv.org/pdf/' + identity)})
    return output


def normalized_title(title):
    return re.sub(r'\W+', '', title.casefold())


def upsert_paper(paper, db=None):
    own_connection=db is None
    with (connect() if db is None else nullcontext(db)) as db:
        if own_connection:db.execute('BEGIN IMMEDIATE')
        fields = 'id,arxiv_id,arxiv_version,title,abstract,authors,categories,primary_category,venue,venue_rank,venue_year,published,abs_url,pdf_url'
        source_id = paper.get('source_id')
        identities = list({identity for identity in (source_id, paper['arxiv_id']) if identity})
        if identities and db.execute('SELECT 1 FROM retired_paper_sources WHERE source_id IN ('+
                                      ','.join('?' for _ in identities)+') LIMIT 1',identities).fetchone():
            return False
        existing = None
        identity_source = source_id or paper['arxiv_id']
        existing = db.execute('SELECT '+','.join('p.'+f for f in fields.split(','))+' FROM papers p JOIN paper_sources s ON s.paper_id=p.id WHERE s.source_id=?', (identity_source,)).fetchone()
        if not existing:
            existing = db.execute('SELECT '+fields+' FROM papers WHERE arxiv_id=?', (paper['arxiv_id'],)).fetchone()
        if not existing and source_id:
            # Older HTML imports used <configured venue.year>:<entry id>.
            token = unquote(urlparse(source_id).path.rsplit('/', 1)[-1])
            years = [paper['venue_year'], paper['venue_year']-1, paper['venue_year']+1]
            legacy = [f'{venue}.{year}:{token}' for venue in set(DEFAULT_VENUES+[s['code'] for s in registry() if s['kind']=='venue']) for year in years]
            existing = db.execute('SELECT '+fields+' FROM papers WHERE arxiv_id IN ('+','.join('?' for _ in legacy)+') LIMIT 1', legacy).fetchone()
        title_key = normalized_title(paper['title'])
        if not existing and len(title_key)>4:
            existing = db.execute('SELECT '+fields+' FROM papers WHERE title_key=? ORDER BY CASE WHEN venue IS NULL THEN 0 ELSE 1 END,id LIMIT 1', (title_key,)).fetchone()
        created = existing is None
        if existing:
            ident = existing['id']
            newer = paper.get('arxiv_version', 1)>existing['arxiv_version']
            content_changed = paper['title']!=existing['title'] or paper.get('abstract','')!=existing['abstract']
            repair = bool(source_id and content_changed)
            changed_scope = bool(paper.get('venue') and paper['venue']!=(existing['venue'] or '').split('.')[0])
            if newer or repair:
                # Briefs use only title and abstract; a manuscript-only revision keeps them valid.
                db.execute("UPDATE papers SET title=?,title_key=?,abstract=?,authors=?,arxiv_version=MAX(arxiv_version,?),scored=0,fulltext=NULL,pdf_path=NULL,skeleton=NULL,tldr=CASE WHEN ? THEN NULL ELSE tldr END,brief_json=CASE WHEN ? THEN NULL ELSE brief_json END,embedding=NULL WHERE id=?",
                           (paper['title'],title_key,paper.get('abstract',''),dumps(paper.get('authors',[])),paper.get('arxiv_version',1),content_changed,content_changed,ident))
                from ..vector_store import active
                if not active(db):db.execute('DELETE FROM papers_vec WHERE paper_id=?',(ident,))
                db.execute('DELETE FROM reading_cards WHERE paper_id=?',(ident,))
            if newer or repair or changed_scope:
                db.execute("UPDATE papers SET classified=0,classification_state='pending' WHERE id=?",(ident,))
                db.execute('DELETE FROM paper_topics WHERE paper_id=?',(ident,))
                db.execute('DELETE FROM topic_pending_papers WHERE paper_id=?',(ident,))
            if paper.get('categories') and dumps(paper['categories'])!=existing['categories']:
                db.execute('UPDATE papers SET categories=? WHERE id=?',(dumps(paper['categories']),ident))
            if paper.get('venue'):
                rank = paper.get('venue_rank')
                if changed_scope or paper['venue']!=existing['venue'] or rank!=existing['venue_rank']:
                    db.execute('UPDATE papers SET venue=?,venue_rank=?,scored=0 WHERE id=?',(paper['venue'],rank,ident))
                # A feed's generated timestamp is not the paper's publication date.
                real_arxiv = bool(arxiv_identity(existing['arxiv_id'])[0])
                metadata = {'venue_year':paper.get('venue_year'),'abs_url':paper.get('abs_url'),
                            'pdf_url':paper.get('pdf_url') or existing['pdf_url'],'authors':dumps(paper.get('authors',[]))}
                if not real_arxiv:
                    metadata.update(primary_category=None,categories='[]',published=paper.get('published'))
                updates = {key:value for key,value in metadata.items() if value!=existing[key]}
                if updates:
                    db.execute('UPDATE papers SET '+','.join(key+'=?' for key in updates)+' WHERE id=?',(*updates.values(),ident))
        else:
            sequence=db.execute("SELECT value FROM app_settings WHERE name='last_paper_id'").fetchone()
            ident=max(int(sequence[0]) if sequence else 0,db.execute('SELECT COALESCE(MAX(id),0) FROM papers').fetchone()[0])+1
            db.execute('INSERT INTO papers(id,arxiv_id,arxiv_version,title,title_key,authors,abstract,venue,venue_year,venue_rank,primary_category,published,abs_url,pdf_url,created_at,ingested_date,categories) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (ident,paper['arxiv_id'],paper.get('arxiv_version',1),paper['title'],title_key,dumps(paper.get('authors',[])),paper.get('abstract',''),paper.get('venue'),paper.get('venue_year'),paper.get('venue_rank'),paper.get('primary_category'),paper.get('published',today()),paper.get('abs_url'),paper.get('pdf_url'),now(),today(),dumps(paper.get('categories',[]))))
            db.execute("INSERT INTO app_settings VALUES('last_paper_id',?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",(str(ident),now()))
        if source_id:
            db.execute('INSERT INTO paper_sources(source_id,paper_id,venue,year) VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET paper_id=excluded.paper_id,venue=excluded.venue,year=excluded.year WHERE paper_sources.paper_id IS NOT excluded.paper_id OR paper_sources.venue IS NOT excluded.venue OR paper_sources.year IS NOT excluded.year',
                       (source_id,ident,paper['venue'],paper['venue_year']))
        else:
            db.execute('INSERT INTO paper_sources(source_id,paper_id) VALUES(?,?) ON CONFLICT(source_id) DO UPDATE SET paper_id=excluded.paper_id WHERE paper_sources.paper_id IS NOT excluded.paper_id', (identity_source,ident))
        return created


async def fetch_arxiv(limit=None):
    added, failures = 0, []
    categories = [s['code'] for s in sources_to_fetch('arxiv')]
    if not categories:
        return 0
    if limit is not None and limit <= 0:
        raise ValueError('试运行数量必须大于 0')
    cutoff = datetime.fromisoformat(now()).astimezone(timezone.utc).replace(second=0, microsecond=0)
    # API search data refreshes at US Eastern midnight, including DST changes.
    cycle = cutoff.astimezone(ZoneInfo('America/New_York')).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    processed, stored, blocked = set(), set(), set()

    def live(codes):
        placeholders = ','.join('?' for _ in codes)
        return {r['code'] for r in rows('SELECT code FROM source_categories WHERE enabled=1 AND fetch_enabled=1 AND key IN ('+placeholders+')', ['arxiv:'+c for c in codes])} if codes else set()

    def progress():
        execute("INSERT INTO source_status(name,progress,added) VALUES('fetch_arxiv',?,?) ON CONFLICT(name) DO UPDATE SET progress=excluded.progress,added=excluded.added",(dumps({'processed':len(processed)}),added))

    def store(papers, codes):
        nonlocal added
        matched = {c:[] for c in codes}
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            enabled = {r['code'] for r in db.execute('SELECT code FROM source_categories WHERE enabled=1 AND fetch_enabled=1 AND key IN ('+','.join('?' for _ in codes)+')', ['arxiv:'+c for c in codes])}
            if not enabled:
                raise SourceRemoved()
            for paper in papers:
                memberships = enabled & set(paper['categories']+[paper['primary_category']])
                if not memberships:
                    continue
                for code in memberships:
                    matched[code].append(paper['arxiv_id'])
                if paper['arxiv_id'] not in stored:
                    added += int(upsert_paper(paper,db))
                    stored.add(paper['arxiv_id'])
                processed.add(paper['arxiv_id'])
        progress()
        return matched

    progress()
    execute('DELETE FROM arxiv_window_checks WHERE window_end<?',((cutoff-timedelta(days=8)).isoformat(),))
    windows = {}
    if limit is None:
        for category in categories:
            cursor = one('SELECT * FROM arxiv_cursors WHERE category=?', (category,))
            if cursor is None:
                baseline = (cutoff-timedelta(days=7)).replace(hour=0, minute=0)
                with connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if not db.execute('SELECT 1 FROM source_categories WHERE key=?',('arxiv:'+category,)).fetchone():
                        continue
                    db.execute('INSERT INTO arxiv_cursors VALUES(?,?,?)', (category, baseline.isoformat(), baseline.isoformat()))
                start_time = baseline
            else:
                baseline = datetime.fromisoformat(cursor['first_sync_from'])
                start_time = max(baseline, (datetime.fromisoformat(cursor['synced_through'])-timedelta(days=7)).replace(hour=0, minute=0))
            while start_time < cutoff:
                end_time = min((start_time+timedelta(days=1)).replace(hour=0,minute=0), cutoff)
                windows.setdefault((start_time,end_time),[]).append(category)
                start_time = end_time
    else:
        # Explicit trials retain category coverage, without advancing full-sync cursors.
        budgets = [limit//len(categories)+int(i<limit%len(categories)) for i in range(len(categories))]
        windows = {(None, i):[c] for i,(c,budget) in enumerate(zip(categories,budgets)) if budget}

    async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers=HEADERS) as client:
        async def page(query, start, size):
            started = time.perf_counter()
            event('source','请求 arXiv 论文列表',job='fetch_arxiv',query=query,offset=start,page_size=size)
            try:
                response = await arxiv_client.get(client,'https://export.arxiv.org/api/query',retries=2,params={
                    'search_query':query,'sortBy':'submittedDate','sortOrder':'ascending' if limit is None else 'descending',
                    'start':start,'max_results':size})
            except httpx.TimeoutException:
                if size <= 100:
                    raise
                event('source','arXiv 大页超时，改用 100 篇重试',job='fetch_arxiv',query=query,offset=start)
                return await page(query,start,100)
            papers = parse_atom(response.text)
            root = ET.fromstring(response.text)
            ns = '{http://a9.com/-/spec/opensearch/1.1/}'
            total_text = root.findtext(ns+'totalResults')
            total = int(total_text) if total_text is not None else None
            offset = root.findtext(ns+'startIndex')
            if offset is not None and int(offset) != start:
                raise ValueError('arXiv 返回了错误的分页位置')
            if not papers and total is not None and start < total:
                raise ValueError('arXiv 分页尚未完成却返回空页')
            event('source','arXiv 返回并校验完成',job='fetch_arxiv',query=query,offset=start,returned=len(papers),total_results=total,seconds=round(time.perf_counter()-started,2),page_size=size)
            return papers,total,size

        for (start_time,end_time), planned in sorted(windows.items()):
            codes = sorted(live(planned)-blocked)
            if not codes:
                continue
            query_codes = []
            if limit is None:
                for code in codes:
                    cached = one('SELECT * FROM arxiv_window_checks WHERE category=? AND window_start=? AND window_end=?',(code,start_time.isoformat(),end_time.isoformat()))
                    if cached and datetime.fromisoformat(cached['checked_at']) >= cycle:
                        processed.update(json.loads(cached['identities']))
                        progress()
                        event('source','复用本次数据更新周期已完成的窗口',job='fetch_arxiv',category=code,window_start=start_time.isoformat(),window_end=end_time.isoformat())
                    else:
                        query_codes.append(code)
            else:
                query_codes = codes
            if not query_codes:
                continue
            query = '('+' OR '.join('cat:'+c for c in query_codes)+')'
            if limit is None:
                query += f' AND submittedDate:[{start_time:%Y%m%d%H%M} TO {end_time:%Y%m%d%H%M}]'
                budget = None
            else:
                query = 'cat:'+query_codes[0]
                budget = budgets[end_time]
            identities = {c:[] for c in query_codes}
            start, size = 0, min(500,budget) if budget else 500
            try:
                while True:
                    if not live(query_codes):
                        raise SourceRemoved()
                    papers,total,size = await page(query,start,min(size,budget-start) if budget else size)
                    matched = store(papers,query_codes)
                    for code, ids in matched.items():
                        identities[code].extend(ids)
                    start += len(papers)
                    if not papers or (budget and start>=budget) or (total is not None and start>=total) or (total is None and len(papers)<size):
                        break
                if limit is None:
                    with connect() as db:
                        db.execute('BEGIN IMMEDIATE')
                        for code in query_codes:
                            if not db.execute('SELECT 1 FROM source_categories WHERE key=? AND enabled=1 AND fetch_enabled=1',('arxiv:'+code,)).fetchone():
                                continue
                            db.execute('UPDATE arxiv_cursors SET synced_through=MAX(synced_through,?) WHERE category=?',(end_time.isoformat(),code))
                            db.execute('INSERT INTO arxiv_window_checks VALUES(?,?,?,?,?) ON CONFLICT(category,window_start,window_end) DO UPDATE SET checked_at=excluded.checked_at,identities=excluded.identities',
                                (code,start_time.isoformat(),end_time.isoformat(),now(),dumps(list(dict.fromkeys(identities[code])))))
                    event('source','分类时间窗口处理完成',job='fetch_arxiv',categories=query_codes,window_start=start_time.isoformat(),window_end=end_time.isoformat(),processed=len(processed),added=added)
            except SourceRemoved:
                event('source','分类已删除或关闭自动抓取，跳过余下分页',job='fetch_arxiv',categories=query_codes)
            except (httpx.HTTPError, ET.ParseError, ValueError) as error:
                failures.append(','.join(query_codes)+': '+str(error)[:200])
                blocked.update(query_codes)
                event('source','arXiv 分类抓取失败',level='error',job='fetch_arxiv',categories=query_codes,error_type=type(error).__name__,error=str(error)[:200],processed=len(processed))
                # A rate-limited service must not be probed repeatedly by other date windows.
                if isinstance(error,httpx.HTTPStatusError) and error.response.status_code in (403,429,503):
                    break
    if failures:
        raise RuntimeError('; '.join(failures))
    if limit is None:
        from .arxiv_daily import sync_latest
        await sync_latest()
    return added


def conference_code(source):
    code = source.strip().split('.')[0]
    return next((s['code'] for s in registry() if s['kind']=='venue' and s['code'].casefold()==code.casefold()), None)


def conference_links(source_id):
    token = unquote(urlparse(source_id).path.rsplit('/',1)[-1])
    if token.endswith('@OpenReview'):
        ident = quote(token[:-11],safe='')
        return 'https://openreview.net/forum?id='+ident, 'https://openreview.net/pdf?id='+ident
    if token.endswith('@AAAI') and token[:-5].isdigit():
        return 'https://ojs.aaai.org/index.php/AAAI/article/view/'+token[:-5], None
    if token.endswith('@PMLR'):
        path = token[:-5].strip('/')
        if '/' in path:
            volume, ident = path.rsplit('/',1)
            ident = ident.removesuffix('.html')
            return f'https://proceedings.mlr.press/{volume}/{ident}.html', f'https://proceedings.mlr.press/{volume}/{ident}/{ident}.pdf'
    return source_id, None


def parse_conference_feed(content, venue):
    root=ET.fromstring(content)
    ns={'a':'http://www.w3.org/2005/Atom'}
    if root.tag!='{http://www.w3.org/2005/Atom}feed':
        raise ValueError('顶会来源没有返回 Atom feed')
    title=root.findtext('a:title','',ns)
    match=re.fullmatch(re.escape(venue)+r'\.(\d{4})',title.strip(),re.I)
    if not match: raise ValueError('无法确认会议及最新届次：'+title)
    year=int(match[1]); output=[]
    for entry in root.findall('a:entry',ns):
        source_id=entry.findtext('a:id','',ns).strip()
        paper_title=' '.join(entry.findtext('a:title','',ns).split())
        if not paper_title or re.fullmatch(r'#?\s*\d+',paper_title):
            raise ValueError('Feed 中存在无效论文标题：'+paper_title)
        parsed=urlparse(source_id)
        if parsed.hostname!='papers.cool' or not parsed.path.startswith('/venue/'):
            raise ValueError('会议条目缺少稳定来源 ID')
        abstract=entry.findtext('a:summary','',ns)
        if entry.find('a:summary',ns) is not None and entry.find('a:summary',ns).get('type') in ('html','xhtml'):
            abstract=BeautifulSoup(abstract,'html.parser').get_text(' ',strip=True)
        abs_url,pdf_url=conference_links(source_id)
        published=entry.findtext('a:published','',ns)[:10] or None
        if published:
            datetime.strptime(published,'%Y-%m-%d')
        output.append({'arxiv_id':source_id,'source_id':source_id,'title':paper_title,
                       'abstract':' '.join(abstract.split()),'authors':[a.findtext('a:name','',ns) for a in entry.findall('a:author',ns)],
                       'venue':venue,'venue_year':year,'venue_rank':None,'primary_category':None,'categories':[],
                       'published':published,'abs_url':abs_url,'pdf_url':pdf_url})
    if not output: raise ValueError('会议 Feed 为空，未更新同步位置')
    return year,output


def ingest_conference_batch(papers,source_key=None):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if source_key and not db.execute('SELECT 1 FROM source_categories WHERE key=? AND fetch_enabled=1',(source_key,)).fetchone():return None
        return sum(int(upsert_paper(p,db)) for p in papers)


async def fetch_conf(force=True):
    from .conferences import fetch_conferences
    return await fetch_conferences(force=force)
