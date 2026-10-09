"""Paper-confirmed OpenAlex authors; metrics are cached off the request path."""
import asyncio
import html
import json
import math
import re
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher

import httpx

from ..config import settings, now, today
from ..db import connect, rows, one, execute, dumps
from ..logs import event
from ..pipeline_control import check_cancelled

API = 'https://api.openalex.org'
HIGHLY_CITED_CAP = 20  # A small auxiliary signal saturates after sustained influence.
NOT_FOUND_LIMIT = 3
NOT_FOUND_RETRY_HOURS = 24


def initialize(db):
    # Keep legacy author profiles (also used by author watchlists) intact.
    db.execute('''CREATE TABLE IF NOT EXISTS author_query_failures (
        author_id TEXT PRIMARY KEY, failure_count INTEGER NOT NULL CHECK(failure_count>0),
        next_retry_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS author_impact_cache (
        author_id TEXT PRIMARY KEY, name TEXT NOT NULL,
        highly_cited_count INTEGER NOT NULL CHECK(highly_cited_count>=0),
        first_year INTEGER NOT NULL, last_year INTEGER NOT NULL, fetched_at TEXT NOT NULL)''')
    if 'validated' not in {r['name'] for r in db.execute('PRAGMA table_info(author_impact_cache)')}:
        db.execute('ALTER TABLE author_impact_cache ADD COLUMN validated INTEGER NOT NULL DEFAULT 0')
        db.execute('UPDATE author_impact_cache SET validated=1 WHERE highly_cited_count>0')
        db.execute("UPDATE author_impact_cache SET fetched_at='1970-01-01' WHERE highly_cited_count=0")
    db.execute('''CREATE TABLE IF NOT EXISTS author_identity_aliases (
        fragment_id TEXT PRIMARY KEY,author_id TEXT NOT NULL,evidence_work_id TEXT NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS idx_author_links_author ON paper_author_links(author_id,paper_id)')
    if 'author_impact_known' not in {r['name'] for r in db.execute('PRAGMA table_info(papers)')}:
        db.execute('ALTER TABLE papers ADD COLUMN author_impact_known INTEGER NOT NULL DEFAULT 0 CHECK(author_impact_known IN (0,1))')
        db.execute('''UPDATE papers SET author_impact_known=1 WHERE author_impact>0 OR (
            EXISTS(SELECT 1 FROM paper_author_links l WHERE l.paper_id=papers.id AND l.confidence>=.98)
            AND (SELECT COUNT(*) FROM paper_author_links l WHERE l.paper_id=papers.id AND l.confidence>=.98)>=json_array_length(authors)
            AND NOT EXISTS(SELECT 1 FROM paper_author_links l LEFT JOIN author_impact_cache c ON c.author_id=l.author_id
                WHERE l.paper_id=papers.id AND l.confidence>=.98 AND (c.author_id IS NULL OR c.validated=0)))''')
    db.execute('''UPDATE papers SET author_impact_known=0 WHERE author_impact=0 AND author_impact_known=1
        AND EXISTS(SELECT 1 FROM paper_author_links l JOIN author_impact_cache c ON c.author_id=l.author_id
                   WHERE l.paper_id=papers.id AND c.validated=0)''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS author_impact_known_reset AFTER UPDATE OF title,authors ON papers
        WHEN NEW.title!=OLD.title OR NEW.authors!=OLD.authors BEGIN
        UPDATE papers SET author_impact_known=0 WHERE id=NEW.id; END''')
    if not db.execute("SELECT 1 FROM app_migrations WHERE name='author-match-v2'").fetchone():
        # Retry previous misses once after changing title retrieval; keep confirmed IDs.
        db.execute("UPDATE author_work_matches SET checked_at=NULL WHERE status IN ('not_found','error')")
        db.execute("INSERT INTO app_migrations VALUES('author-match-v2',?)", (now(),))
    saved = db.execute("SELECT value FROM app_settings WHERE name='task_center'").fetchone()
    if saved:
        stored = json.loads(saved['value'])
        author = stored.get('advanced', {}).get('author_impact', {})
        if 'daily_credits' in author:
            del author['daily_credits']
            db.execute("UPDATE app_settings SET value=?,updated_at=? WHERE name='task_center'", (dumps(stored), now()))
    db.execute("DELETE FROM app_settings WHERE name='openalex_usage'")


def normalized(text):
    text = unicodedata.normalize('NFKD', text or '').casefold()
    return ''.join(c for c in text if c.isalnum())


def title_text(text):
    text = html.unescape(text or '')
    text = re.sub(r'\\(?:mathrm|mathbf|mathit|mathsf|mathcal|textrm|text|operatorname)\s*\{([^{}]*)\}', r'\1', text)
    # Formatting markers are not part of a paper's title; keep formula identifiers.
    return re.sub(r'[^\w\s]', ' ', text, flags=re.UNICODE).replace('_', ' ')


def same_name(local, remote):
    if normalized(local) == normalized(remote):
        return True
    def parts(name):
        name = unicodedata.normalize('NFKD', name or '').casefold()
        return re.findall(r'[^\W\d_]+', ''.join(c for c in name if not unicodedata.combining(c)))
    left, right = parts(local), parts(remote)
    if len(left) < 2 or len(right) < 2 or left[-1] != right[-1]:
        return False
    # Only abbreviated given names may expand; different full given names conflict.
    short, long = sorted((left[:-1], right[:-1]), key=len)
    return all(a == b or min(len(a), len(b)) == 1 and a[0] == b[0]
               for a, b in zip(short, long))


def influence(highly_cited_count):
    """All fields use OpenAlex's same-subfield/year/type top-10% indicator."""
    count = max(0, min(HIGHLY_CITED_CAP, int(highly_cited_count)))
    return round(100 * math.log1p(count) / math.log1p(HIGHLY_CITED_CAP), 2)


def confirmed_authors(paper, work):
    """Require the paper's title and corroborating authors, never a name search."""
    title, other = normalized(title_text(paper['title'])), normalized(title_text(work.get('title')))
    if not title or SequenceMatcher(None, title, other).ratio() < .98:
        return []
    names = json.loads(paper.get('authors') or '[]')
    local = list(dict.fromkeys(n for n in names if normalized(n)))
    authors = [a.get('author', {}) for a in work.get('authorships', [])]
    matched = []
    hits = set()
    for name in local:
        candidates = {a['id']: a for a in authors if same_name(name, a.get('display_name'))
                      and re.fullmatch(r'https://openalex\.org/A\d+', a.get('id') or '')}
        if len(candidates) == 1:
            author = next(iter(candidates.values()))
            if author['id'] not in {a['id'] for a in matched}:
                matched.append(author)
                hits.add(normalized(name))
    if len(hits) < min(2, len(local)) or not local or len(hits)/len(local) < .6:
        return []
    arxiv = (paper.get('arxiv_id') or '').split('v')[0].casefold()
    urls = [work.get('doi') or ''] + [l.get('landing_page_url') or '' for l in work.get('locations', [])]
    identified = bool(arxiv and any(re.search(r'(?:/abs/|/pdf/|arxiv\.)'+re.escape(arxiv)+r'(?:v\d+|\.pdf)?(?:$|[?#])', u.casefold()) for u in urls))
    paper_year = int((paper.get('published') or today())[:4])
    if not identified and (work.get('publication_year') is None or abs(int(work['publication_year'])-paper_year)>2):
        return []
    return [{'author_id': a['id'].rsplit('/',1)[-1], 'name':a['display_name'],
             'confidence':1.0 if identified else .98} for a in matched]


def apply_impact(paper_id, db=None):
    if db is None:
        with connect() as connection:
            return apply_impact(paper_id, connection)
    paper = db.execute('SELECT author_impact,author_impact_known,authors FROM papers WHERE id=?', (paper_id,)).fetchone()
    if paper is None:
        return 0
    metrics = db.execute('''SELECT c.highly_cited_count,c.validated FROM paper_author_links l
        LEFT JOIN author_impact_cache c ON c.author_id=l.author_id
        WHERE l.paper_id=? AND l.confidence>=.98''', (paper_id,)).fetchall()
    # A stopped/failed refresh never replaces an old result with an incomplete one.
    available = [m for m in metrics if m['highly_cited_count'] is not None and m['validated']]
    if not available:
        if not paper['author_impact'] and paper['author_impact_known']:
            db.execute('UPDATE papers SET author_impact_known=0 WHERE id=?', (paper_id,))
        return paper['author_impact']
    impact = max(influence(m['highly_cited_count']) for m in available)
    author_count = len({normalized(n) for n in json.loads(paper['authors'] or '[]') if normalized(n)})
    complete = bool(author_count and len(available)==len(metrics) and len(metrics)>=author_count)
    if not complete:
        impact = max(paper['author_impact'],impact)
    if not complete and impact == 0:
        return paper['author_impact']
    from .quality import recompute
    if paper['author_impact']!=impact or not paper['author_impact_known']:
        db.execute('UPDATE papers SET author_impact=?,author_impact_known=1 WHERE id=?',(impact,paper_id))
    recompute(db,paper_id)
    return impact


class RateLimited(RuntimeError):
    pass


class OpenAlex:
    def __init__(self, client):
        self.client = client
        self.name_candidates = {}

    async def get(self, path, **params):
        check_cancelled()
        await asyncio.sleep(.2)
        check_cancelled()
        response = await self.client.get(API+path, params=params)
        check_cancelled()
        if response.status_code==429:
            raise RateLimited('OpenAlex 请求额度暂不可用；已保存进度，下次任务继续')
        response.raise_for_status()
        return response.json()

    async def match(self, paper):
        data = await self.get('/works', filter='title.search:'+title_text(paper['title']), **{'per-page':20},
                              select='id,title,publication_year,authorships,locations,doi')
        candidates = [(w, confirmed_authors(paper,w)) for w in data.get('results', [])]
        candidates = [(w,a) for w,a in candidates if a and re.fullmatch(r'https://openalex\.org/W\d+',w.get('id') or '')]
        if not candidates:
            return None, []
        # Two different author identities for the same title are ambiguous.
        identities = {tuple(sorted(a['author_id'] for a in authors)) for _,authors in candidates}
        if len(identities)>1:
            return None, []
        work, authors = max(candidates, key=lambda item:len(item[1]))
        return work['id'].rsplit('/',1)[-1], authors

    async def highly_cited_count(self, author_id, year):
        data = await self.get('/works', filter=','.join([
            'authorships.author.id:'+author_id,
            'from_publication_date:'+str(year-9)+'-01-01',
            'to_publication_date:'+today(),
            'citation_normalized_percentile.is_in_top_10_percent:true']),
            per_page=1, select='id')
        count = data.get('meta', {}).get('count')
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError('OpenAlex 未返回有效的高被引论文数量')
        return count

    async def historical_identity(self, name, paper):
        peers = {normalized(n) for n in json.loads(paper['authors'] or '[]')
                 if not same_name(n,name) and len(n.split())>1 and all(len(part)>1 for part in n.split())}
        if not peers:
            return None
        key = normalized(name)
        if key not in self.name_candidates:
            data = await self.get('/authors',search=name,per_page=100,
                                  select='id,display_name,cited_by_count')
            self.name_candidates[key] = {a['id'].rsplit('/',1)[-1] for a in data.get('results',[])
                if normalized(a.get('display_name'))==key and a.get('cited_by_count',0)>0
                and re.fullmatch(r'https://openalex\.org/A\d+',a.get('id') or '')}
        candidates = self.name_candidates[key]
        if not candidates:
            return None
        data = await self.get('/works',filter='authorships.author.id:'+'|'.join(sorted(candidates)),
                              search=title_text(paper['title']),per_page=100,select='id,authorships')
        evidence = {}
        for work in data.get('results',[]):
            authors = [a.get('author',{}) for a in work.get('authorships',[])]
            if not {normalized(a.get('display_name')) for a in authors}&peers:
                continue
            for a in authors:
                aid = (a.get('id') or '').rsplit('/',1)[-1]
                if aid in candidates and normalized(a.get('display_name'))==key:
                    evidence[aid] = work.get('id','').rsplit('/',1)[-1]
        if len(evidence)==1:
            aid,work_id = next(iter(evidence.items()))
            if re.fullmatch(r'W\d+',work_id):return aid,work_id
        return None


def link_identity(fragment, author_id, evidence_work_id):
    """Persist a corroborated alias once; all papers then share the canonical cache."""
    if fragment == author_id:
        return
    with connect() as db:
        linked = db.execute('SELECT paper_id,name,confidence FROM paper_author_links WHERE author_id=?',(fragment,)).fetchall()
        for paper in linked:
            db.execute('INSERT OR IGNORE INTO paper_author_links VALUES(?,?,?,?)',
                       (paper['paper_id'],author_id,paper['name'],paper['confidence']))
        db.execute('DELETE FROM paper_author_links WHERE author_id=?',(fragment,))
        db.execute('INSERT OR REPLACE INTO author_identity_aliases VALUES(?,?,?)',(fragment,author_id,evidence_work_id))


def pending_papers(config, *, count=False):
    year = date.fromisoformat(today()).year
    success_cutoff=(datetime.now(timezone.utc)-timedelta(days=config['cache_days'])).isoformat()
    retry_cutoff=(datetime.now(timezone.utc)-timedelta(days=7)).isoformat()
    columns = 'COUNT(*) AS pending' if count else 'p.id,p.title,p.authors,p.published,p.arxiv_id,m.work_id,m.status'
    stale='''(c.author_id IS NULL OR c.fetched_at<CASE WHEN c.validated=1 THEN :success ELSE :retry END
              OR c.first_year!=:first_year OR c.last_year!=:last_year)'''
    blocked='(f.failure_count>=:failure_limit OR f.next_retry_at>:stamp)'
    # Cooling/paused authors must not cause empty five-second continuation batches.
    # A paper stays eligible while any of its other authors still needs a query.
    sql='SELECT '+columns+'''
        FROM papers p LEFT JOIN author_work_matches m ON m.paper_id=p.id
        WHERE (m.paper_id IS NULL OR m.checked_at IS NULL OR m.checked_at<CASE WHEN m.status='matched' THEN :success ELSE :retry END
        OR (m.status='matched' AND EXISTS(SELECT 1 FROM paper_author_links l
            LEFT JOIN author_impact_cache c ON c.author_id=l.author_id WHERE l.paper_id=p.id
            AND '''+stale+''')))
        AND (NOT EXISTS(SELECT 1 FROM paper_author_links l JOIN author_query_failures f ON f.author_id=l.author_id
                WHERE l.paper_id=p.id AND '''+blocked+''')
            OR EXISTS(SELECT 1 FROM paper_author_links l
                LEFT JOIN author_impact_cache c ON c.author_id=l.author_id
                LEFT JOIN author_query_failures f ON f.author_id=l.author_id
                WHERE l.paper_id=p.id AND '''+stale+''' AND (f.author_id IS NULL OR NOT '''+blocked+''')))'''
    args={'success':success_cutoff,'retry':retry_cutoff,'first_year':year-9,'last_year':year,
          'failure_limit':NOT_FOUND_LIMIT,'stamp':now()}
    if count:
        return one(sql,args)['pending']
    sql+=''' ORDER BY CASE WHEN :priority!='' AND p.id IN (SELECT paper_id FROM paper_categories WHERE category_key=:category) THEN 0 ELSE 1 END,
            COALESCE(m.checked_at,''),p.published DESC,p.id DESC LIMIT :batch_size'''
    return rows(sql,{**args,'priority':config['priority_category'],
                     'category':'arxiv:'+config['priority_category'].casefold(),'batch_size':config['batch_size']})


def retry_author(author_id):
    failure=one('SELECT failure_count,next_retry_at FROM author_query_failures WHERE author_id=?',(author_id,))
    return not failure or failure['failure_count']<NOT_FOUND_LIMIT and failure['next_retry_at']<=now()


def defer_missing_author(author_id, paper_id):
    check_cancelled()
    retry_at=(datetime.now(timezone.utc)+timedelta(hours=NOT_FOUND_RETRY_HOURS)).isoformat()
    execute('''INSERT INTO author_query_failures VALUES(?,1,?,?) ON CONFLICT(author_id) DO UPDATE SET
        failure_count=MIN(author_query_failures.failure_count+1,?),
        next_retry_at=excluded.next_retry_at,updated_at=excluded.updated_at''',
        (author_id,retry_at,now(),NOT_FOUND_LIMIT))
    failure=one('SELECT failure_count FROM author_query_failures WHERE author_id=?',(author_id,))
    event('author','OpenAlex 作者记录不存在，已暂停自动重查' if failure['failure_count']>=NOT_FOUND_LIMIT
          else 'OpenAlex 作者记录不存在，24 小时后再查',level='warning',job='author_impact',
          service='OpenAlex',path='/authors/'+author_id,status_code=404,author_id=author_id,
          paper_id=paper_id,failure_count=failure['failure_count'])


async def author_impact():
    cfg=settings()
    from ..task_settings import configuration
    config=configuration()['advanced']['author_impact']
    year = date.fromisoformat(today()).year
    success_cutoff=(datetime.now(timezone.utc)-timedelta(days=config['cache_days'])).isoformat()
    retry_cutoff=(datetime.now(timezone.utc)-timedelta(days=7)).isoformat()
    pending=pending_papers(config)
    headers={'Authorization':'Bearer '+config['openalex_api_key']} if config['openalex_api_key'] else {}
    started=time.monotonic();processed=matched=authors_updated=0
    remaining=pending_papers(config,count=True)
    def publish(final=False):
        nonlocal remaining
        if final:
            remaining=pending_papers(config,count=True)
        execute('UPDATE source_status SET progress=? WHERE name=?',
                (dumps({'processed':processed,'matched':matched,'authors_updated':authors_updated,
                        'pending':remaining if final else max(0,remaining-processed)}),'author_impact'))
    publish()
    async with httpx.AsyncClient(timeout=20,headers=headers) as client:
        source=OpenAlex(client)
        for paper in pending:
            check_cancelled()
            if time.monotonic()-started>=cfg.author_impact_seconds:
                break
            current_author=None
            try:
                if paper['work_id']:
                    authors=rows('SELECT author_id,name,confidence FROM paper_author_links WHERE paper_id=?',(paper['id'],))
                    work_id=paper['work_id']
                else:
                    work_id,authors=await source.match(paper)
                    with connect() as db:
                        db.execute('INSERT INTO author_work_matches(paper_id,work_id,status,checked_at) VALUES(?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET work_id=excluded.work_id,status=excluded.status,checked_at=excluded.checked_at',
                                   (paper['id'],work_id,'pending' if authors else 'not_found',None if authors else now()))
                        for a in authors:
                            db.execute('INSERT OR REPLACE INTO paper_author_links(paper_id,author_id,name,confidence) VALUES(?,?,?,?)',
                                       (paper['id'],a['author_id'],a['name'],a['confidence']))
                if authors:
                    for a in authors:
                        fragment = a['author_id']
                        alias = one('SELECT author_id,evidence_work_id FROM author_identity_aliases WHERE fragment_id=?',(fragment,))
                        if alias:
                            a['author_id'] = alias['author_id']
                            link_identity(fragment,a['author_id'],alias['evidence_work_id'])
                        current_author=a['author_id']
                        if one('SELECT 1 FROM author_impact_cache WHERE author_id=? AND fetched_at>=CASE WHEN validated=1 THEN ? ELSE ? END AND first_year=? AND last_year=?',
                               (a['author_id'],success_cutoff,retry_cutoff,year-9,year)):
                            continue
                        if not retry_author(a['author_id']):
                            continue
                        count = await source.highly_cited_count(a['author_id'], year)
                        validated = count>0
                        if not validated:
                            try:
                                profile = await source.get('/authors/'+a['author_id'])
                            except httpx.HTTPStatusError as error:
                                if error.response.status_code!=404:
                                    raise
                                defer_missing_author(a['author_id'],paper['id'])
                                continue
                            validated = profile.get('cited_by_count',0)>0 or any(
                                item.get('year',year)<year and item.get('works_count',0)>0
                                for item in profile.get('counts_by_year',[]))
                            if not validated:
                                identity = await source.historical_identity(a['name'],paper)
                                if identity:
                                    aid,evidence = identity
                                    link_identity(a['author_id'],aid,evidence)
                                    a['author_id'] = aid
                                    current_author=aid
                                    cached = one('SELECT highly_cited_count FROM author_impact_cache WHERE author_id=? AND validated=1 AND fetched_at>=? AND first_year=? AND last_year=?',
                                                 (aid,success_cutoff,year-9,year))
                                    count = cached['highly_cited_count'] if cached else await source.highly_cited_count(aid,year)
                                    validated = True
                        check_cancelled()
                        with connect() as db:
                            db.execute('DELETE FROM author_query_failures WHERE author_id=?',(a['author_id'],))
                            db.execute('INSERT INTO author_impact_cache(author_id,name,highly_cited_count,first_year,last_year,fetched_at,validated) VALUES(?,?,?,?,?,?,?) ON CONFLICT(author_id) DO UPDATE SET name=excluded.name,highly_cited_count=excluded.highly_cited_count,first_year=excluded.first_year,last_year=excluded.last_year,fetched_at=excluded.fetched_at,validated=excluded.validated',
                                       (a['author_id'],a['name'],count,year-9,year,now(),int(validated)))
                            linked = db.execute('SELECT paper_id FROM paper_author_links WHERE author_id=?', (a['author_id'],)).fetchall()
                            for linked_paper in linked:
                                apply_impact(linked_paper['paper_id'], db)
                        authors_updated += 1
                    apply_impact(paper['id'])
                    execute("UPDATE author_work_matches SET status='matched',checked_at=? WHERE paper_id=?",(now(),paper['id']))
                    matched+=1
                processed+=1
                publish()
            except RateLimited:
                publish(final=True)
                event('author','作者数据达到接口额度，保留进度',level='warning',job='author_impact',processed=processed,matched=matched,authors_updated=authors_updated)
                raise
            except (httpx.HTTPError,ValueError,KeyError,TypeError) as error:
                # Keep the unfinished paper eligible for the one delayed retry.
                execute("INSERT INTO author_work_matches(paper_id,status,checked_at) VALUES(?,'error',NULL) ON CONFLICT(paper_id) DO UPDATE SET status='error',checked_at=NULL",(paper['id'],))
                publish(final=True)
                response=getattr(error,'response',None)
                status=response.status_code if response is not None else None
                request=getattr(error,'request',None)
                event('author','作者数据查询失败',level='warning',job='author_impact',paper_id=paper['id'],
                      error_type=type(error).__name__,service='OpenAlex',author_id=current_author,
                      status_code=status,path=request.url.path if request is not None else None)
                if status:
                    raise RuntimeError(f'OpenAlex 作者数据请求失败（HTTP {status}），请检查 OpenAlex 服务和认证配置') from error
                raise
    publish(final=True)
    event('author','作者影响力缓存更新完成',job='author_impact',processed=processed,matched=matched,authors_updated=authors_updated)
    return processed
