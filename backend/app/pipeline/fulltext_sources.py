"""Verified public fulltexts: stored links, official proceedings, then arXiv."""
import asyncio
import json
import re
import time
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from collections import OrderedDict
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from ..config import settings, today
from ..db import rows
from ..logs import event
from .. import arxiv_client
from .fetch import conference_links, arxiv_identity, parse_atom

_indexes=OrderedDict()
_reporter=ContextVar('fulltext_progress',default=None)
OFFICIAL={'proceedings.mlr.press':'PMLR','papers.nips.cc':'NeurIPS','papers.neurips.cc':'NeurIPS',
          'proceedings.neurips.cc':'NeurIPS','ojs.aaai.org':'AAAI','openreview.net':'OpenReview'}


@contextmanager
def reporting(callback):
    token=_reporter.set(callback)
    try:yield
    finally:_reporter.reset(token)


def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFKD',value or '').casefold() if c.isalnum())


def matches(paper,title,authors):
    if normalized(paper['title'])!=normalized(title):return False
    local=paper.get('authors') or []
    if isinstance(local,str):local=json.loads(local)
    local={normalized(n) for n in local if normalized(n)}
    other={normalized(n) for n in authors if normalized(n)}
    hits=local & other
    return bool(local and len(hits)>=min(2,len(local)) and len(hits)/len(local)>=.6)


def origin(pdf_url,page_url=None,version=None):
    host=urlparse(pdf_url).hostname
    preprint=host in ('arxiv.org','export.arxiv.org')
    name='arXiv' if preprint else 'PMLR' if host=='raw.githubusercontent.com' and '/mlresearch/' in pdf_url else OFFICIAL.get(host,'原始论文来源')
    if name=='PMLR' and page_url and urlparse(page_url).hostname!='proceedings.mlr.press':page_url=None
    return {'name':name,'url':page_url or pdf_url,'pdf_url':pdf_url,'preprint':preprint,
            'version':version or ('会议正式稿' if name in ('PMLR','NeurIPS','AAAI') else '')}


async def html(client,url):
    response=await client.get(url);response.raise_for_status()
    if len(response.content)>20*1024*1024:raise ValueError('论文目录响应过大')
    return await asyncio.to_thread(BeautifulSoup,response.text,'html.parser')


async def document(client,paper,page,verified=False):
    soup=await html(client,page)
    value=lambda key:[m.get('content','') for m in soup.select(f'meta[name="{key}"]')]
    titles=value('citation_title');authors=value('citation_author')
    if titles and not matches(paper,titles[0],authors):return None
    if not titles and not verified:return None
    pdfs=value('citation_pdf_url')
    link=soup.select_one('a.obj_galley_link.pdf, a[href$=".pdf"]')
    target=pdfs[0] if pdfs else link.get('href') if link else None
    if not target:return None
    pdf=urljoin(page,target)
    if urlparse(pdf).scheme not in ('https','http'):return None
    return origin(pdf,page)


async def index(client,url,kind):
    cached=_indexes.get(url)
    if cached and cached[0]>time.monotonic():return cached[1]
    soup=await html(client,url);items=[]
    if kind=='pmlr':
        for block in soup.select('.paper'):
            title=block.select_one('.title');authors=block.select_one('.authors')
            links=block.select('a[href]');page=next((a['href'] for a in links if a.get_text(strip=True)=='abs'),None)
            pdf=next((a['href'] for a in links if 'pdf' in a.get_text(strip=True).lower()),None)
            if title and authors and page:items.append((title.get_text(' ',strip=True),authors.get_text(' ',strip=True).split(','),urljoin(url,page),urljoin(url,pdf) if pdf else None))
    elif kind=='neurips':
        for block in soup.select('li'):
            link=block.select_one('a[href*="-Abstract"]');authors=block.select_one('.paper-authors, i')
            if link and authors:items.append((link.get_text(' ',strip=True),authors.get_text(' ',strip=True).split(','),urljoin(url,link['href']),None))
    _indexes[url]=(time.monotonic()+21600,items)
    while len(_indexes)>8:_indexes.popitem(last=False)
    return items


async def indexed_paper(client,paper,url,kind):
    for title,authors,page,pdf in await index(client,url,kind):
        if matches(paper,title,authors):return origin(pdf,page) if pdf else await document(client,paper,page,True)
    return None


async def official(client,paper):
    venue=(paper.get('venue') or '').split('.')[0].casefold()
    year=int(paper.get('venue_year') or (paper.get('published') or today())[:4])
    if venue=='icml':
        soup=await html(client,'https://proceedings.mlr.press/')
        volume=None
        for block in soup.select('li'):
            if re.search(r'\bICML\s+'+str(year)+r'\b',block.get_text(' ',strip=True),re.I):
                link=block.select_one('a[href]')
                if link:volume=urljoin('https://proceedings.mlr.press/',link['href']);break
        # The series home page currently omits this published volume.
        if not volume and year==2026:volume='https://proceedings.mlr.press/v306/'
        if volume:return await indexed_paper(client,paper,volume,'pmlr')
    elif venue in ('neurips','nips'):
        return await indexed_paper(client,paper,f'https://papers.nips.cc/paper_files/paper/{year}','neurips')
    elif venue=='aaai':
        response=await client.get('https://ojs.aaai.org/index.php/AAAI/search/index',params={'query':paper['title']})
        response.raise_for_status();soup=await asyncio.to_thread(BeautifulSoup,response.text,'html.parser')
        for block in soup.select('.obj_article_summary'):
            link=block.select_one('.title a');authors=block.select_one('.authors')
            if link and authors and matches(paper,link.get_text(' ',strip=True),authors.get_text(' ',strip=True).split(',')):
                return await document(client,paper,urljoin(str(response.url),link['href']),True)
    elif venue=='iclr' and paper.get('abs_url'):
        return await document(client,paper,paper['abs_url'])
    return None


async def openalex_locations(client,paper):
    headers={'Authorization':'Bearer '+settings().openalex_api_key} if settings().openalex_api_key else {}
    response=await client.get('https://api.openalex.org/works',params={'search':paper['title'],'per-page':5,'select':'title,authorships,locations'},headers=headers)
    response.raise_for_status();output=[]
    for work in response.json().get('results',[]):
        authors=[a.get('author',{}).get('display_name','') for a in work.get('authorships',[])]
        if not matches(paper,work.get('title'),authors):continue
        for location in work.get('locations',[]):
            page,pdf=location.get('landing_page_url'),location.get('pdf_url')
            host=urlparse(page or pdf or '').hostname
            if host in OFFICIAL and host!='openreview.net':
                candidate=origin(pdf,page) if pdf else await document(client,paper,page,True)
                if candidate:output.append(candidate)
    return output


async def arxiv(client,paper):
    response=await arxiv_client.get(client,'https://export.arxiv.org/api/query',params={
        'search_query':'ti:"'+paper['title'].replace('"',' ')+'"','max_results':5})
    for record in parse_atom(response.content):
        if matches(paper,record['title'],record['authors']):
            identity=record['arxiv_id']+'v'+str(record['arxiv_version'])
            return origin('https://arxiv.org/pdf/'+identity,'https://arxiv.org/abs/'+identity,identity)
    return None


async def candidates(client,paper,resolve_pdf=True,progress=None):
    seen=set()
    def accept(candidate):
        if not candidate or candidate['pdf_url'] in seen:return False
        seen.add(candidate['pdf_url']);return True
    if paper.get('pdf_url'):
        candidate=origin(paper['pdf_url'],paper.get('abs_url'))
        if accept(candidate):yield candidate
    if not resolve_pdf:return
    if progress:progress(stage='resolving',source_name='备用全文来源')
    known=[paper.get('abs_url')]
    for row in rows('SELECT source_id FROM paper_sources WHERE paper_id=?',(paper['id'],)):
        identity,version=arxiv_identity(row['source_id'])
        if identity and row['source_id']!=paper.get('arxiv_id'):
            identity+='v'+str(version)
            candidate={**origin('https://arxiv.org/pdf/'+identity,'https://arxiv.org/abs/'+identity,identity),'check_pdf':True}
            if accept(candidate):yield candidate
        else:known.append(conference_links(row['source_id'])[0])
    # Existing official landing pages often have an updated PDF URL.
    for page in dict.fromkeys(p for p in known if p):
        if urlparse(page).hostname not in OFFICIAL:continue
        try:
            candidate=await document(client,paper,page)
            if accept(candidate):yield candidate
        except Exception as error:log_failure(paper['id'],'已有来源',error)
    try:
        candidate=await official(client,paper)
        if accept(candidate):yield candidate
    except Exception as error:log_failure(paper['id'],'会议官方论文集',error)
    try:
        for candidate in await openalex_locations(client,paper):
            if accept(candidate):yield candidate
    except Exception as error:log_failure(paper['id'],'学术元数据检索',error)
    try:
        candidate=await arxiv(client,paper)
        if accept(candidate):yield candidate
    except Exception as error:log_failure(paper['id'],'arXiv 检索',error)


def log_failure(paper_id,source,error):
    event('reading','全文备用来源尝试失败',level='warning',job='fulltext',paper_id=paper_id,
          source=source,error_type=type(error).__name__,http_status=getattr(getattr(error,'response',None),'status_code',None))
