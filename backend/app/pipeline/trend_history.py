"""Small shared caches of older public metadata used only as trend evidence."""
import asyncio
import json
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

import httpx
from bs4 import BeautifulSoup

from ..config import now, today
from .. import arxiv_client
from ..db import dumps, execute, one
from ..source_catalog import sources_to_fetch
from .fetch import HEADERS, parse_atom

_lock = asyncio.Lock()
ALIASES = {
    'Memory and long context':['long context','memory transformer','agent memory'],
    'Language models':['language model'],
    'Continual learning':['continual learning','catastrophic forgetting'],
    'Agents':['language agent','autonomous agent'],
    'Graph coloring and Ramsey theory':['graph coloring','Ramsey'],
    'Tools and planning':['tool use','agent planning'],
    'Reasoning and alignment':['language model reasoning','language model alignment'],
    'Efficient training and inference':['efficient inference','model compression'],
}
# Starting references, checked against the official arXiv abstract pages.
# Facts and categories are always retrieved and validated; IDs are only lookup hints.
BACKGROUND_IDS = {
    'Memory and long context':['2203.08913','2307.03172'],
    'Language models':['1706.03762','2203.08913'],
    'Continual learning':['1612.00796'],
    'Graph coloring and Ramsey theory':['2303.09521'],
    'math.CO':['2303.09521'],
    'cs.LG':['1612.00796','1706.03762'],
    'cs.CL':['1706.03762','2307.03172'],
    'ai':['2203.08913','1706.03762'],
}


def parse_abstract_page(content, ident):
    soup = BeautifulSoup(content,'html.parser')
    def meta(name):
        item = soup.select_one(f'meta[name="{name}"]')
        return item.get('content','') if item else ''
    if meta('citation_arxiv_id')!=ident:
        raise ValueError('历史论文标识不匹配')
    published = date.fromisoformat(meta('citation_date').replace('/','-')).isoformat()
    subjects,abstract = soup.select_one('.subjects'),soup.select_one('blockquote.abstract')
    if not subjects or not abstract or not meta('citation_title'):
        raise ValueError('历史论文元数据不完整')
    descriptor = abstract.select_one('.descriptor')
    if descriptor:
        descriptor.decompose()
    return {'arxiv_id':ident,'title':meta('citation_title'),'published':published,
            'categories':re.findall(r'\b(?:cs|math|stat)\.[A-Za-z]+\b',subjects.get_text(' ',strip=True)),
            'abstract':abstract.get_text(' ',strip=True),'abs_url':'https://arxiv.org/abs/'+ident}


def search_spec(label, category, topic):
    codes = [category[6:]] if category and category.startswith('arxiv:') else [s['code'] for s in sources_to_fetch('arxiv') if s['code'].startswith(('cs.','stat.'))]
    if topic:
        codes = [key[6:] for key in json.loads(topic['category_keys'] or '[]') if key.startswith('arxiv:') and (not category or not category.startswith('arxiv:') or key==category)]
    if not codes:
        return None
    query = '('+' OR '.join('cat:'+code for code in sorted(set(codes)))+')'
    if topic:
        phrases = ALIASES.get(topic['name_en'],topic['name_en'].split(' and '))
        phrases = [re.sub(r'[^a-zA-Z0-9 -]','',p).strip() for p in phrases]
        query += ' AND ('+' OR '.join('all:"'+p+'"' for p in phrases if p)+')'
    background = topic['name_en'] if topic else category[6:] if category and category.startswith('arxiv:') else 'ai'
    return {'label':label,'key':query,'codes':codes,'query':query,'background_ids':BACKGROUND_IDS.get(background,[])}


async def references(spec):
    end = date.fromisoformat(today())-timedelta(days=90)
    start = end-timedelta(days=10*365)
    async with _lock:
        cached = one('SELECT * FROM trend_reference_cache WHERE scope_key=?',(spec['key'],))
        if cached:
            age = datetime.fromisoformat(now())-datetime.fromisoformat(cached['fetched_at'])
            if age < (timedelta(hours=6) if cached['error'] else timedelta(days=7)):
                return json.loads(cached['papers'])
        error = None
        papers = json.loads(cached['papers']) if cached else []
        try:
            async with httpx.AsyncClient(timeout=15,follow_redirects=True,headers=HEADERS) as client:
                candidates = []
                for ident in spec.get('background_ids',[]):
                    response = await arxiv_client.get(client,'https://arxiv.org/abs/'+ident)
                    response.raise_for_status()
                    candidates.append(parse_abstract_page(response.text,ident))
                if not candidates:
                    response = await arxiv_client.get(client,'https://export.arxiv.org/api/query',params={
                        'search_query':spec['query']+f' AND submittedDate:[{start:%Y%m%d}0000 TO {end:%Y%m%d}2359]',
                        'sortBy':'relevance','sortOrder':'descending','start':0,'max_results':4})
                    response.raise_for_status()
                    candidates = parse_atom(response.text)
            papers = [{'id':'arxiv:'+p['arxiv_id'],'title':p['title'],'published':p['published'],
                       'sources':['arxiv:'+code for code in p['categories']],
                       'abstract':p['abstract'][:1600],'url':p['abs_url'],'historical':True}
                      for p in candidates if p['title'] and p['abstract'] and start.isoformat()<=p['published']<=end.isoformat()
                      and set(spec['codes']) & set(p['categories'])][:2]
        except (httpx.HTTPError,ET.ParseError,ValueError) as exc:
            error = type(exc).__name__
        execute('INSERT INTO trend_reference_cache VALUES(?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET papers=excluded.papers,fetched_at=excluded.fetched_at,error=excluded.error',
                (spec['key'],dumps(papers),now(),error))
        return papers


async def supplement(materials, specs):
    present = {p['id'] for p in materials['papers']}
    older = []
    for spec in specs[:4]:
        for reference in await references(spec):
            if reference['id'] not in present:
                item = {**reference,'directions':[spec['label']]}
                materials['papers'].append(item)
                present.add(item['id'])
                older.append(item)
    materials['historical_evidence'] = {'available':bool(older),'papers':len(older),
        'from':min((p['published'] for p in older),default=None),
        'through':max((p['published'] for p in older),default=None)}
