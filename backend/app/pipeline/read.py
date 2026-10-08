from .. import prompts
from ..llm import runtime as models
import asyncio
import json
import re
import time
import httpx
from pydantic import BaseModel, Field
from typing import Literal
from ..db import one, rows, execute, dumps, connect
from ..config import now, settings
from ..llm.provider import cloud
from ..llm.ollama import ollama
from .parse import ensure_fulltext, skeleton_text
from .score import cosine
from ..llm.reading_limits import reading_budget, token_upper_bound, prefix_bytes
from ..llm.reading_json import ReadingJSONStream
from ..llm.thinking import AnswerStream
from ..logs import event
from . import reading_queue,fulltext_cache,fulltext_sources


class Evidence(BaseModel):
    section: str
    quote: str


class Claim(BaseModel):
    claim: str
    evidence: Evidence
    verified: bool = False


class ReadingCard(BaseModel):
    tldr: str
    method_summary: str
    key_results: list[Claim] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    read_priority: Literal['must_read','worth_reading','skim']
    answers: 'ReadingAnswers | None' = None
    paper_kind: Literal['empirical','theoretical','mixed'] = 'mixed'


class ReadingAnswers(BaseModel):
    problem: str = Field(min_length=1,max_length=16000)
    related_work: str = Field(min_length=1,max_length=16000)
    method: str = Field(min_length=1,max_length=24000)
    evaluation: str = Field(min_length=1,max_length=24000)
    future: str = Field(min_length=1,max_length=16000)
    summary: str = Field(min_length=1,max_length=16000)


class SixQuestionCard(ReadingCard):
    answers: ReadingAnswers
    schema_version: Literal[2] = 2


def normalized(text):
    return re.sub(r'\s+',' ',text).strip()


def verify_card(card, fulltext):
    schema=SixQuestionCard if card.get('answers') is not None else ReadingCard
    validated = schema.model_validate(card).model_dump()
    text = normalized(fulltext['text'])
    anchors = fulltext.get('pages',[]) + fulltext.get('sections',[])
    for claim in validated['key_results']:
        quote = normalized(claim['evidence']['quote'])
        matched = next((a for a in anchors if quote and quote in normalized(a['text'])),None)
        claim['verified'] = bool(len(quote)>=12 and quote in text and matched)
        if claim['verified']:
            claim['evidence']['section'] = matched['section']
    return validated


async def relevant_chunks(paper, fulltext):
    chunks = []
    for s in fulltext['sections']:
        for start in range(0,len(s['text']),5000):
            chunks.append({'section':s['section'],'text':s['text'][start:start+5500]})
    if len(chunks)<=6:
        return chunks
    query = paper['title']+'\n'+paper['abstract']+'\nmethod experiments results limitations'
    vectors = await models.embed([query]+[c['text'] for c in chunks])
    ranked = sorted(range(len(chunks)),key=lambda i:cosine(vectors[0],vectors[i+1]),reverse=True)[:6]
    return [chunks[i] for i in sorted(ranked)]


async def reading_materials(fulltext,level,material_budget=None,progress=None):
    # Pages retain appendices, proofs and their locations even when heading detection fails.
    sections=fulltext.get('pages') or fulltext.get('sections') or [{'section':'全文','text':fulltext['text']}]
    feature='reading_l3' if level=='L3' else 'reading_l2'
    binding=models.selected(feature)
    context,output=reading_budget(binding,level)
    material_budget=material_budget if material_budget is not None else context-output-4000
    if material_budget<1500:
        raise ValueError('模型上下文不足，请选择更长上下文的精读模型')
    if token_upper_bound(dumps(sections))<=material_budget:
        if progress:progress(stage='generating',mode='direct')
        return sections
    text='\n\n'.join('['+s['section']+']\n'+s['text'] for s in sections)
    chunks=[]
    while text:
        part=prefix_bytes(text,material_budget-1000)
        chunks.append({'section':f'全文段落 {len(chunks)+1}','text':part})
        text=text[len(part):]
    # Bound the combined notes, including JSON escaping, so they fit the final request.
    note_budget=min(5000,(material_budget-1000)//(2*len(chunks))-100)
    if note_budget<120:
        raise ValueError('全文超过所选模型的处理容量，请使用更长上下文的模型')
    semaphore=asyncio.Semaphore(1 if binding['kind']!='cloud' else 2)
    completed=0
    if progress:progress(stage='materials',mode='chunked',completed=0,total=len(chunks))
    async def summarize(section):
        nonlocal completed
        async with semaphore:
            note=await models.complete(feature,[{'role':'system','content':prompts.get('reading_section')},
                                                {'role':'user','content':dumps(section)}])
            completed+=1
            if progress:progress(stage='materials',completed=completed,total=len(chunks))
            return {'section':section['section'],'text':prefix_bytes(str(note),note_budget)}
    tasks=[asyncio.create_task(summarize(section)) for section in chunks]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


@models.model_task
@fulltext_cache.using_fulltext
async def generate_card(paper_id, level='L2'):
    started=time.perf_counter()
    paper = one('SELECT * FROM papers WHERE id=?',(paper_id,))
    if not paper:raise ValueError('论文已删除')
    parser=ReadingJSONStream()
    state={'stage':'parsing','started_at':now(),'level':level,'answers':parser.answers,'answers_completed':0,'answers_total':6}
    last_saved=0
    def progress(force=True,**values):
        nonlocal last_saved
        state.update(values,paper_kind=parser.paper_kind,answers_completed=len(parser.completed_answers))
        if not force and time.perf_counter()-last_saved<.25:return
        execute('UPDATE reading_cards SET progress_json=? WHERE paper_id=?',(dumps(state),paper_id))
        last_saved=time.perf_counter()
    progress()
    try:
        with fulltext_sources.reporting(progress):
            fulltext = await ensure_fulltext(paper)
    except httpx.HTTPStatusError as error:
        raise RuntimeError(f'论文全文下载失败（HTTP {error.response.status_code}），请检查论文来源的访问权限或全文地址；尚未调用精读模型') from error
    except httpx.TimeoutException as error:
        raise RuntimeError('论文全文下载超时，请稍后重试。尚未调用精读模型。') from error
    except httpx.RequestError as error:
        raise RuntimeError('无法连接论文全文来源，请检查网络或稍后重试；尚未调用精读模型') from error
    schema = SixQuestionCard.model_json_schema()
    system=prompts.get('reading_card')+'\n先输出 answers，依次为 problem、related_work、method、evaluation、future、summary，再输出其他字段。只输出完整 JSON。'+('\nL3 深读：重点展开方法、证明或实验细节与适用边界。' if level=='L3' else '')+'\nschema：'+dumps(schema)
    prefix=paper['title']+'\n摘要：'+paper['abstract']+'\n全文材料：'
    feature='reading_l3' if level=='L3' else 'reading_l2'
    context,output=reading_budget(models.selected(feature),level)
    materials=await reading_materials(fulltext,level,context-output-token_upper_bound(system+prefix)-1024,progress)
    messages=[{'role':'system','content':system},{'role':'user','content':prefix+dumps(materials)}]
    progress(stage='generating')
    answer=AnswerStream();parts=[];size=0
    try:
        async with asyncio.timeout(600 if level=='L2' else 900):
            async for delta in models.stream(messages,[],feature=feature,json_mode=True,schema=schema):
                if getattr(delta,'reasoning_content',None) and not any(parser.answers.values()):
                    progress(force=False,stage='thinking')
                text=answer.feed(delta.content or '')
                if not text:continue
                parts.append(text);size+=len(text)
                if size>250000:raise ValueError('精读输出过长，请调整 Prompt 或模型设置后重试')
                had_answers=any(parser.answers.values())
                if parser.feed(text):progress(force=not had_answers,stage='generating')
    finally:
        progress()
    tail=answer.finish()
    if tail:parts.append(tail);parser.feed(tail)
    progress(stage='verifying')
    raw=''.join(parts).strip()
    if raw.startswith('```'):
        raw=raw.split('\n',1)[-1].rsplit('```',1)[0].strip()
    data=SixQuestionCard.model_validate(json.loads(raw)).model_dump()
    card = verify_card(data,fulltext)
    card['reading_level']=level
    if fulltext.get('_source'):card['fulltext_source']=fulltext['_source']
    state.update(stage='ready',answers={},answers_completed=6,elapsed_seconds=round(time.perf_counter()-started,2))
    execute("UPDATE reading_cards SET status='ready',card_json=?,progress_json=?,error=NULL,created_at=? WHERE paper_id=?", (dumps(card),dumps(state),now(),paper_id))
    event('reading','精读卡生成完成',job=feature,paper_id=paper_id,mode=state.get('mode'),seconds=round(time.perf_counter()-started,2))
    return card


_tasks = {}


async def run_card(paper_id, level='L2'):
    try:
        await generate_card(paper_id,level)
    except asyncio.CancelledError:
        execute("UPDATE reading_cards SET status='failed',error='生成已停止，可手动重试' WHERE paper_id=?",(paper_id,))
        raise
    except Exception as error:
        message='模型响应超时，精读尚未完成，请重试或调整思考模式' if 'timeout' in type(error).__name__.lower() else models.safe_error(error)[:500]
        execute("UPDATE reading_cards SET status='failed',error=? WHERE paper_id=?",(message,paper_id))
        cached=one('SELECT progress_json FROM reading_cards WHERE paper_id=?',(paper_id,))
        stage=json.loads(cached['progress_json'] or '{}').get('stage') if cached else None
        event('reading','精读卡生成失败',level='error',job='reading_l3' if level=='L3' else 'reading_l2',
              paper_id=paper_id,stage=stage,error_type=type(error).__name__,error=message)
    finally:
        _tasks.pop(paper_id,None)


def request_card(paper_id, level='L2', retry=False, regenerate=False, source='user',allow_regenerate=True):
    reading_queue.enqueue(paper_id,level,retry,regenerate,source,allow_regenerate)
    if settings().pipeline_mode=='inline':reading_queue.dispatch()
    return one('SELECT * FROM reading_cards WHERE paper_id=?',(paper_id,))


def card_response(cached):
    body={key:cached[key] for key in ('status','error','created_at')}
    if cached['card_json']:body['card']=json.loads(cached['card_json'])
    if cached.get('progress_json'):
        body['progress']=json.loads(cached['progress_json'])
    queued=reading_queue.queue_info(cached['paper_id']) if cached['status']=='pending' and cached.get('paper_id') is not None else None
    if queued:body['progress']={**body.get('progress',{}),'stage':'queued','queued_at':queued['queued_at'],'queue_ahead':queued['queue_ahead']}
    return body


async def card_events(paper_id):
    previous=None;heartbeat=time.monotonic()
    while True:
        cached=one('SELECT * FROM reading_cards WHERE paper_id=?',(paper_id,))
        if not cached:
            yield 'event: card\ndata: '+dumps({'status':'failed','error':'论文或精读卡已删除'})+'\n\n'
            return
        body=dumps(card_response(cached))
        if body!=previous:
            yield 'event: card\ndata: '+body+'\n\n'
            previous=body;heartbeat=time.monotonic()
        if cached['status']!='pending':return
        if time.monotonic()-heartbeat>=15:
            yield ': keep-alive\n\n';heartbeat=time.monotonic()
        await asyncio.sleep(.25)


async def preread():
    from ..pipeline_control import check_cancelled
    for paper in rows('SELECT id FROM papers ORDER BY quality_score DESC LIMIT 5'):
        check_cancelled()
        request_card(paper['id'],retry=True,source='preread')
        try:
            while one('SELECT status FROM reading_cards WHERE paper_id=?',(paper['id'],))['status']=='pending':
                await asyncio.sleep(.1)
        except asyncio.CancelledError:
            await reading_queue.stop_preread(paper['id'])
            raise
    failed = rows("SELECT paper_id FROM reading_cards WHERE status='failed'")
    if failed:
        raise RuntimeError(f'{len(failed)} 张精读卡生成失败，请检查云端配置及 PDF 来源')
