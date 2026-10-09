from .. import prompts
from ..llm import runtime as models
import json
import asyncio
import time
from ..db import rows, one, connect, execute, pack, dumps, validate_vectors
from .quality import discipline, recompute, VERSION
from . import fulltext_cache
from .fulltext_cache import using_fulltext
from pydantic import BaseModel, Field, AliasChoices, model_validator, ConfigDict
from ..config import settings, now
import re
from ..logs import event
from ..pipeline_control import check_cancelled
from .progress import TaskProgress
from ..interest.profile import CURRENT_PROFILE_IDS
from . import paper_retries


class QualityAssessment(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    summary: str = Field(min_length=1, max_length=400)
    contribution: float | None = Field(ge=0, le=100, strict=True, validation_alias=AliasChoices('contribution','novelty'))
    evidence_insufficient: bool

    @model_validator(mode='after')
    def unknown_contribution(self):
        if self.contribution is None:
            self.evidence_insufficient = True
        return self


def quality_material(paper, cached=None):
    label, key = discipline(paper)
    parts = []
    pattern = r'theorem|proof|lemma|proposition|main result|conclusion|limitation' if label=='数学' else r'method|experiment|evaluat|result|conclusion|limitation|theor|proof'
    if cached:
        for section in cached.get('sections', []):
            if re.search(pattern, section.get('section',''), re.I) and section.get('text','').strip():
                parts.append({'section':section['section'], 'text':section['text'][:2000]})
                if len(parts)==3:break
        if not parts:
            text = cached.get('text','')
            for match in re.finditer(r'^\s*(?:\d+(?:\.\d+)*[. ]+)?(?:Theorem|Lemma|Proof|Method\w*|Experiment\w*|Results|Conclusion\w*)\b', text, re.I|re.M):
                parts.append({'section':'缓存全文片段', 'text':text[match.start():match.start()+2000]})
                if len(parts)==3:break
    return key, {'discipline':label, 'title':paper['title'], 'abstract':(paper.get('abstract') or '')[:5000],
                 'material_level':'cached_excerpts' if parts else 'abstract', 'excerpts':parts}


@using_fulltext
@paper_retries.tracked('assess_quality')
async def score_paper(p):
    if not prompts.skill_enabled('quality'):raise RuntimeError('质量评分技能已停用，原结果保留')
    key, material = quality_material(p, fulltext_cache.get(p['id']))
    def validate(raw):
        assessment = QualityAssessment.model_validate(raw).model_dump()
        if material['material_level']=='abstract':
            assessment['evidence_insufficient']=True
        binding = models.current_binding() or models.selected('quality')
        return {**assessment, 'discipline':material['discipline'], 'material_level':material['material_level'],
                'model':binding['model'], 'provider':binding['kind'], 'prompt_id':key,
                'prompt_version':prompts.revision(key), 'formula_version':VERSION, 'generated_at':now()}
    result = await models.complete('quality', [{'role':'system','content':prompts.get(key)},
        {'role':'user','content':dumps(material)}], json_mode=True, validate=validate,
        schema=QualityAssessment.model_json_schema(by_alias=False))
    QualityAssessment.model_validate(result)
    if 'formula_version' not in result:result=validate(result)
    if material['material_level']=='abstract':result={**result,'evidence_insufficient':True}
    check_cancelled()
    with connect() as db:
        db.execute('UPDATE papers SET skeleton=?,scored=1 WHERE id=?', (dumps(result),p['id']))
        recompute(db,p['id'])


@paper_retries.tracked('build_vectors')
async def embed_paper(paper):
    from ..llm.embedding_queue import background
    from ..llm.vector_rebuild import _database_step
    from ..db import set_paper_vector
    async with background():vectors=await models.embed([paper['title']+'\n'+(paper['abstract'] or '')])
    validate_vectors(vectors,1)
    check_cancelled()
    await _database_step(set_paper_vector,paper['id'],vectors[0])


@models.model_task
async def build_vectors():
    from ..llm import vector_rebuild
    if vector_rebuild.pending():return await vector_rebuild.run()
    errors = []
    max_id=one('SELECT COALESCE(MAX(id),0) n FROM papers')['n']
    profiles=rows(f"SELECT * FROM interest_profile WHERE (embedding_parts IS NULL OR (embedding IS NULL AND embedding_parts!='[]')) AND id IN ({CURRENT_PROFILE_IDS})")
    eligible=paper_retries.eligible('build_vectors')
    embedding_total=one('SELECT COUNT(*) n FROM papers WHERE id<=? AND embedding IS NULL AND '+eligible,(max_id,))['n']
    progress=TaskProgress('build_vectors',[
        ('embedding','论文向量',embedding_total,'篇',models.selected('embedding')['model'],models.embedding_parallelism(background=True)),
        ('profile_embedding','当前兴趣向量',len(profiles),'份',models.selected('embedding')['model'],1)])
    try:
        last_id=0
        progress.begin('embedding')
        async def embed_batch(batch):
            from ..llm.embedding_queue import background
            async with background():return await models.embed([p['title']+'\n'+(p['abstract'] or '') for p in batch])
        while batch := rows('SELECT id,title,abstract FROM papers WHERE id>? AND id<=? AND embedding IS NULL AND '+eligible+' ORDER BY id LIMIT ?',(last_id,max_id,32*models.embedding_parallelism(background=True))):
            check_cancelled()
            last_id=batch[-1]['id']
            started=time.perf_counter();progress.begin('embedding',batch[0])
            try:
                chunks=[batch[i:i+32] for i in range(0,len(batch),32)]
                tasks=[asyncio.create_task(embed_batch(chunk)) for chunk in chunks]
                try:results=await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        if not task.done():task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
                vectors=[vector for result in results for vector in result]
                validate_vectors(vectors,len(batch))
                from ..vector_store import set_many
                from ..llm.vector_rebuild import _database_step
                check_cancelled()
                await _database_step(set_many,batch,[pack(vector) for vector in vectors])
                paper_retries.succeeded('build_vectors',[p['id'] for p in batch])
                progress.finish('embedding',completed=len(batch),seconds=time.perf_counter()-started,paper=batch[0])
                from ..background_load import yield_to_web
                await yield_to_web()
            except asyncio.CancelledError:raise
            except Exception as error:
                check_cancelled()
                paper_retries.failed('build_vectors',[p['id'] for p in batch])
                errors.extend([type(error).__name__]*len(batch))
                progress.finish('embedding',failed=len(batch),seconds=time.perf_counter()-started,paper=batch[0])
                event('task','论文向量批次失败',level='error',job='build_vectors',count=len(batch),error_type=type(error).__name__)
        progress.begin('profile_embedding')
        for profile in profiles:
            check_cancelled()
            from ..interest.profile import profile_embedding,embedding_inputs,save_current_embedding
            if not one(f'SELECT id FROM interest_profile WHERE id=? AND id IN ({CURRENT_PROFILE_IDS})',(profile['id'],)):
                progress.finish('profile_embedding',completed=1)
                continue
            started=time.perf_counter()
            try:
                from ..llm.embedding_queue import background
                async with background():
                    vector=await profile_embedding(profile['content'])
                if vector is None and embedding_inputs(profile['content']):raise ValueError('兴趣向量生成失败')
                if vector is not None and len(vector)!=settings().embedding_dim*4:raise ValueError('兴趣向量维度不匹配')
                check_cancelled()
                save_current_embedding(profile,vector)
                progress.finish('profile_embedding',completed=1,seconds=time.perf_counter()-started)
            except asyncio.CancelledError:raise
            except Exception as error:
                errors.append(type(error).__name__)
                progress.finish('profile_embedding',failed=1,seconds=time.perf_counter()-started)
        check_cancelled()
    finally:
        progress.close()
    if errors:
        raise RuntimeError(f'{len(errors)} 项向量生成失败；{errors[0]}')
    return progress.value()['completed']


@models.model_task
async def assess_quality():
    if not prompts.skill_enabled('quality'): return 0
    errors=[]
    max_id=one('SELECT COALESCE(MAX(id),0) n FROM papers')['n']
    eligible=paper_retries.eligible('assess_quality')
    total=one('SELECT COUNT(*) n FROM papers WHERE id<=? AND scored=0 AND '+eligible,(max_id,))['n']
    concurrency=models.concurrency('quality')
    progress=TaskProgress('assess_quality',[
        ('quality','论文质量评估',total,'篇',models.selected('quality')['model'],concurrency)])
    try:
        last_id=0
        progress.begin('quality')
        async def quality_worker(pending):
            for paper in pending:
                check_cancelled()
                started=time.perf_counter();progress.begin('quality',paper)
                try:
                    await score_paper(paper)
                    progress.finish('quality',completed=1,seconds=time.perf_counter()-started,paper=paper)
                except asyncio.CancelledError:raise
                except Exception as error:
                    errors.append(type(error).__name__)
                    progress.finish('quality',failed=1,seconds=time.perf_counter()-started,paper=paper)
                    event('task','论文质量评估失败',level='error',job='assess_quality',paper_id=paper['id'],error_type=type(error).__name__)
        while batch := rows('SELECT id,title,abstract,primary_category,venue,venue_rank FROM papers WHERE id>? AND id<=? AND scored=0 AND '+eligible+' ORDER BY id LIMIT 32',(last_id,max_id)):
            check_cancelled()
            last_id=batch[-1]['id']
            pending=iter(batch)
            workers=[asyncio.create_task(quality_worker(pending)) for _ in range(concurrency)]
            try:
                await asyncio.gather(*workers)
                check_cancelled()
            finally:
                for worker in workers:
                    if not worker.done():worker.cancel()
                await asyncio.gather(*workers,return_exceptions=True)
    finally:
        progress.close()
    if errors:
        raise RuntimeError(f'{len(errors)} 篇论文质量评估失败；{errors[0]}')
    return progress.value()['completed']
