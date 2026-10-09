from .. import prompts
import asyncio
import time
from ..llm import runtime as models
from ..config import settings
from ..db import rows, one, execute, dumps
from ..logs import event
from ..pipeline_control import check_cancelled
from .progress import TaskProgress
from . import paper_retries
from ..llm.ollama import ollama
from ..llm.provider import cloud
from ..interest.profile import current
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


class PaperBrief(BaseModel):
    title_zh: str = Field(min_length=1, max_length=240)
    problem: str = Field(min_length=1, max_length=300)
    contribution_result: str = Field(min_length=1, max_length=1200)

    @model_validator(mode='before')
    @classmethod
    def legacy_output(cls,value):
        if isinstance(value,dict) and 'contribution_result' not in value and value.get('contribution') and value.get('result'):
            value={**value,'contribution_result':str(value['contribution'])+' '+str(value['result'])}
        return value

    @field_validator('*', mode='before')
    @classmethod
    def clean(cls, value, info):
        if not isinstance(value, str):
            return value
        value = ' '.join(value.split())
        if not value:
            raise ValueError('解读不能为空')
        labels = {
            'title_zh': {'title_zh', '中文标题', '准确翻译的论文标题', '论文标题的中文翻译'},
            'problem': {'problem', '研究问题', '研究问题概述'},
            'contribution_result': {'contribution_result', '主要贡献和结果', '核心贡献与主要结果', '主要贡献与结果'},
        }
        if value.strip(' "\'：:。.*`') in labels[info.field_name]:
            raise ValueError('解读必须包含本文实际内容，不能是字段标签')
        return value


BRIEF_PROMPT = prompts.DEFAULTS['brief']['text']


class BriefLengthError(ValueError):
    def __init__(self, errors):
        self.fields = [{'field':error['loc'][0], 'length':len(error['input']),
                        'limit':error['ctx']['max_length']} for error in errors]
        labels = {'title_zh':'中文标题', 'problem':'研究问题', 'contribution_result':'主要贡献和结果'}
        super().__init__('速读内容超过长度限制：' + '，'.join(
            f"{labels[item['field']]} {item['length']} / {item['limit']} 字符" for item in self.fields))


def validate_brief(value):
    try:
        return PaperBrief.model_validate(value)
    except ValidationError as error:
        errors = error.errors()
        if errors and all(item['type']=='string_too_long' for item in errors):
            raise BriefLengthError(errors) from None
        raise


@paper_retries.tracked('tldr_gen')
async def generate_brief(paper):
    messages = [{'role': 'system', 'content': prompts.get('brief')},
                {'role': 'user', 'content': paper['title'] + '\n' + paper['abstract']}]
    def compact(result, error):
        if not isinstance(error, BriefLengthError):
            return None
        event('task','论文速读超长，尝试精简一次',job='tldr_gen',paper_id=paper['id'],fields=error.fields)
        return [*messages, {'role':'assistant', 'content':dumps(result)},
                {'role':'user', 'content':prompts.get('brief_compact')+'\n'+str(error)}]
    try:
        brief = await models.complete('brief', messages, json_mode=True, validate=validate_brief,
                                      schema=PaperBrief.model_json_schema(), repair_validation=compact)
    except BriefLengthError as error:
        event('task','论文速读精简后仍超长，本轮处理失败',level='error',job='tldr_gen',paper_id=paper['id'],fields=error.fields)
        raise
    value = brief.model_dump()
    check_cancelled()
    execute('UPDATE papers SET brief_json=?,tldr=? WHERE id=?',
            (dumps(value), brief.contribution_result, paper['id']))
    return value


async def tldr_gen():
    eligible=paper_retries.eligible('tldr_gen')
    snapshot=one('SELECT COUNT(*) n,COALESCE(MAX(id),0) max_id FROM papers WHERE brief_json IS NULL AND '+eligible)
    total,max_id=snapshot['n'],snapshot['max_id']
    errors=0
    concurrency=models.concurrency('brief')
    progress=TaskProgress('tldr_gen',[('brief','论文速读',total,'篇',models.selected('brief')['model'],concurrency)])
    batch=iter(())
    cursor=None
    def next_paper():
        nonlocal batch,cursor
        paper=next(batch,None)
        if paper is None:
            after='';parameters=[max_id]
            if cursor is not None:
                if cursor[0] is None:
                    after=' AND quality_score IS NULL AND id>?';parameters.append(cursor[1])
                else:
                    after=' AND (quality_score<? OR quality_score IS NULL OR (quality_score=? AND id>?))'
                    parameters.extend([cursor[0],cursor[0],cursor[1]])
            found=rows('SELECT id,title,abstract,quality_score FROM papers WHERE brief_json IS NULL AND '+eligible+' AND id<=?'+after+' ORDER BY quality_score DESC,id LIMIT 64',
                       parameters)
            if not found:return None
            cursor=(found[-1]['quality_score'],found[-1]['id'])
            batch=iter(found)
            paper=next(batch)
        return paper
    async def worker():
        nonlocal errors
        while True:
            check_cancelled()
            p=next_paper()
            if p is None:break
            progress.begin('brief',p)
            started=time.perf_counter()
            try:
                await generate_brief(p)
                progress.finish('brief',completed=1,seconds=time.perf_counter()-started,paper=p)
            except asyncio.CancelledError:raise
            except Exception as error:
                errors+=1
                progress.finish('brief',failed=1,seconds=time.perf_counter()-started,paper=p)
                event('task','论文速读本轮处理失败',level='error',job='tldr_gen',paper_id=p['id'],error_type=type(error).__name__)
    workers = [asyncio.create_task(worker()) for _ in range(min(concurrency,total))]
    try:
        await asyncio.gather(*workers)
        check_cancelled()
    finally:
        for task in workers:
            if not task.done():task.cancel()
        await asyncio.gather(*workers,return_exceptions=True)
        progress.close()
    if errors:
        raise RuntimeError(f'{errors} 篇论文速读失败，连续失败 3 轮后暂停自动处理')
    return progress.value()['completed']


async def why_you_care(user_id, paper, card):
    profile = current(user_id)
    if not profile:
        return '完成兴趣设置后可以获得个性化解读。'
    return await cloud.complete([
        {'role':'system','content':prompts.get('why_you_care')},
        {'role':'user','content':profile['content']+'\n论文：'+paper['title']+'\n精读卡：'+__import__('json').dumps(card,ensure_ascii=False)}], purpose='fast',cache_seconds=86400)
