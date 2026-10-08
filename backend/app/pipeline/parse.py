import json
import re
import asyncio
import uuid
import httpx
import pymupdf
from ..config import settings
from .fetch import HEADERS
from . import fulltext_cache,fulltext_sources
from ..logs import event


def structure_pdf(path):
    with pymupdf.open(path) as doc:
        pages = [{'section': f'page {i+1}', 'page': i+1, 'text': page.get_text(sort=True)} for i,page in enumerate(doc)]
    sections = []
    heading = 'Introduction'
    buffer = []
    pattern = re.compile(r'^\s*(?:\d+(?:\.\d+)*\s+)?(?:Abstract|Introduction|Related Work|Method\w*|Experiment\w*|Results|Discussion|Conclusion\w*|Limitations|References)\s*$', re.I)
    for p in pages:
        for line in p['text'].splitlines():
            if pattern.match(line):
                if buffer:
                    sections.append({'section': heading, 'text': '\n'.join(buffer)})
                heading, buffer = line.strip(), []
            else:
                buffer.append(line)
    if buffer:
        sections.append({'section': heading, 'text': '\n'.join(buffer)})
    return {'pages': pages, 'sections': sections,
            'text': '\n'.join(p['text'] for p in pages)}


async def ensure_fulltext(paper, resolve_pdf=True):
    cached=fulltext_cache.get(paper['id'])
    if cached:
        if not cached.get('_source') and (paper.get('pdf_url') or paper.get('abs_url')):
            cached['_source']=fulltext_sources.origin(paper.get('pdf_url') or paper['abs_url'],paper.get('abs_url'))
        return cached
    folder=settings().data_dir/'pdf';folder.mkdir(parents=True,exist_ok=True)
    failures=[];progress=fulltext_sources._reporter.get()
    try:
        async with asyncio.timeout(180):
            async with httpx.AsyncClient(timeout=httpx.Timeout(30,connect=10),follow_redirects=True,headers=HEADERS) as client:
                async for candidate in fulltext_sources.candidates(client,paper,resolve_pdf,progress):
                    temporary=folder/f"{paper['id']}.{uuid.uuid4().hex}.part"
                    try:
                        if progress:progress(stage='parsing',source_name=candidate['name'])
                        async with client.stream('GET',candidate['pdf_url']) as response:
                            response.raise_for_status();size=0;prefix=b''
                            with temporary.open('wb') as handle:
                                async for chunk in response.aiter_bytes():
                                    size+=len(chunk);prefix=(prefix+chunk)[:1024]
                                    if size>50*1024*1024:raise ValueError('PDF 大于 50 MB')
                                    handle.write(chunk)
                        if b'%PDF-' not in prefix:raise ValueError('下载内容不是 PDF')
                        data=await asyncio.to_thread(structure_pdf,temporary)
                        if not data['text'].strip():raise ValueError('PDF 未包含可提取文本')
                        if candidate.get('check_pdf'):
                            front=fulltext_sources.normalized('\n'.join(p['text'] for p in data['pages'][:2]))
                            names=json.loads(paper['authors']) if isinstance(paper.get('authors'),str) else paper.get('authors',[])
                            names=[fulltext_sources.normalized(n) for n in names if n]
                            if fulltext_sources.normalized(paper['title']) not in front or not names or sum(n in front for n in names)<max(min(2,len(names)),len(names)*.6):
                                raise ValueError('备用 PDF 的标题或作者无法核对')
                        data['_source']={k:v for k,v in candidate.items() if k!='check_pdf'}
                        fulltext_cache.store(paper['id'],data)
                        event('reading','全文下载与解析完成',job='fulltext',paper_id=paper['id'],source=candidate['name'],version=candidate['version'],preprint=candidate['preprint'],download_bytes=size)
                        return data
                    except (httpx.HTTPError,ValueError,RuntimeError) as error:
                        status=getattr(getattr(error,'response',None),'status_code',None)
                        failures.append(candidate['name']+(f'（HTTP {status}）' if status else '（下载或解析失败）'))
                        fulltext_sources.log_failure(paper['id'],candidate['name'],error)
                        if progress:progress(stage='resolving',source_name='备用全文来源')
                    finally:temporary.unlink(missing_ok=True)
    except TimeoutError as error:
        raise RuntimeError('论文全文查找与下载超过 3 分钟，请稍后重试；尚未调用精读模型') from error
    detail='、'.join(dict.fromkeys(failures)) or ('未找到标题与作者一致的公开全文' if resolve_pdf else '没有可用的全文地址')
    raise RuntimeError('论文全文下载失败：'+detail+'；尚未调用精读模型')


def skeleton_text(paper, fulltext=None):
    if not fulltext:
        return paper['abstract']
    parts = [paper['abstract']]
    for section in fulltext.get('sections',[]):
        name = section['section'].casefold()
        if 'introduction' in name:
            parts.append(section['text'][-3500:])
        elif 'conclusion' in name or 'limitation' in name:
            parts.append(section['text'][:4000])
    parts.extend(re.findall(r'(?:Figure|Table)\s+\d+[^\n]*(?:\n[^\n]+){0,2}', fulltext['text'])[:20])
    return '\n\n'.join(parts)[:18000]
