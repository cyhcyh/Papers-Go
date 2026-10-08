from .. import prompts
from ..llm import runtime as models
import asyncio
import json
from ..db import rows, one, execute, dumps
from ..config import now
from ..interest.profile import current
from ..llm.provider import cloud
from .tools import tool_definitions, execute_tool
from ..llm.thinking import AnswerStream, clean_answer

_active_sessions = set()
_active_tasks = {}


def sse(event,data):
    return f'event: {event}\ndata: {dumps(data)}\n\n'


async def chat_events(session_id,user_id,content,paper_id=None):
    from ..agent_skills import invocation
    with models.model_snapshot(), invocation():
        async for event in _chat_events(session_id,user_id,content,paper_id):
            yield event


async def _chat_events(session_id,user_id,content,paper_id=None):
    if not one('SELECT id FROM chat_sessions WHERE id=? AND user_id=?',(session_id,user_id)):
        _active_sessions.discard(session_id)
        return
    _active_tasks[session_id] = asyncio.current_task()
    frozen = current(user_id)
    system = prompts.get('chat')+'\n用户画像冻结快照：'+(frozen['content'] if frozen else '尚未设置')
    from .. import agent_skills as skills
    available=skills.available(user_id,limit=12)
    system+='\n'+prompts.get('skills_runtime')
    if available:
        system+='\n可用技能目录（更多可调用 list_skills 搜索）：'+dumps([{**s,'description':s['description'][:120]} for s in available])
    for active in skills.session_context(session_id,user_id):
        system+='\n已加载技能（本轮固定版本）：'+dumps(active)
    if paper_id:
        paper = one('SELECT title,abstract,brief_json,abs_url FROM papers WHERE id=?',(paper_id,))
        card = one("SELECT card_json FROM reading_cards WHERE paper_id=? AND status='ready'",(paper_id,))
        if paper:
            from .references import reference
            system += '\n当前论文上下文：'+dumps({'id':paper_id,'abstract':paper['abstract'],**reference(paper),'reading_card':json.loads(card['card_json']) if card else None})
    # Complete older turns rather than sending a tool message without its assistant call.
    history = rows('SELECT role,content FROM chat_messages WHERE session_id=? AND role IN (\'user\',\'assistant\') ORDER BY id DESC LIMIT 30',(session_id,))[::-1]
    for message in history:
        if message['role']=='assistant':
            message['content'] = clean_answer(message['content'])
    messages = [{'role':'system','content':system}]+history
    if content:
        execute("INSERT INTO chat_messages(session_id,role,content,created_at) VALUES(?,'user',?,?)",(session_id,content,now()))
        messages.append({'role':'user','content':content})
    final_text = ''
    proposals = []
    skill_events = []
    try:
        for _ in range(6):
            text = ''
            reasoning = ''
            answer = AnswerStream()
            calls = {}
            async for delta in models.stream(messages,tool_definitions()):
                reasoning += getattr(delta,'reasoning_content',None) or getattr(delta,'thinking',None) or ''
                if delta.content:
                    visible = answer.feed(delta.content)
                    if visible:
                        text+=visible
                        final_text+=visible
                        yield sse('delta',{'text':visible})
                for call in delta.tool_calls or []:
                    obj = calls.setdefault(call.index,{'id':'','type':'function','function':{'name':'','arguments':''}})
                    if call.id: obj['id']=call.id
                    if call.function and call.function.name: obj['function']['name']+=call.function.name
                    if call.function and call.function.arguments: obj['function']['arguments']+=call.function.arguments
            tail = answer.finish()
            if tail:
                text+=tail
                final_text+=tail
                yield sse('delta',{'text':tail})
            if not calls:
                if not text.strip() and not proposals and not skill_events:
                    raise ValueError('模型未返回最终正文，请重试或检查本地模型状态。')
                break
            ordered = [calls[i] for i in sorted(calls)]
            assistant = {'role':'assistant','content':text or None,'tool_calls':ordered}
            if reasoning:
                assistant['reasoning_content'] = reasoning
            messages.append(assistant)
            for call in ordered:
                try:
                    args = json.loads(call['function']['arguments'])
                    result = await execute_tool(call['function']['name'],args,user_id,session_id,frozen)
                    if isinstance(result,dict) and result.get('requires_confirmation'):
                        proposals.append(result['proposal'])
                        yield sse('confirm',result['proposal'])
                    else:
                        yield sse('tool',{'name':call['function']['name'],'result':result})
                        name=call['function']['name']
                        if name in ('create_skill','update_skill','activate_skill','set_skill_enabled','propose_shared_skill'):
                            notice={'id':args['skill_id'] if name=='propose_shared_skill' else result['id'],'title':result['title'],'action':name,'enabled':result.get('enabled'), 'revision':result.get('revision')}
                            skill_events.append(notice)
                            yield sse('skill',notice)
                except Exception as error:
                    result = {'error':models.safe_error(error)}
                messages.append({'role':'tool','tool_call_id':call['id'],'content':dumps(result)})
                execute("INSERT INTO chat_messages(session_id,role,content,tool_calls,created_at) VALUES(?,'tool',?,?,?)",(session_id,dumps(result),dumps(call),now()))
        else:
            yield sse('error',{'message':'工具调用达到本轮上限，请继续对话'})
        yield sse('done',{})
    except asyncio.CancelledError:
        raise
    except Exception as error:
        yield sse('error',{'message':models.safe_error(error)[:300]})
    finally:
        try:
            if (final_text or proposals or skill_events) and one('SELECT id FROM chat_sessions WHERE id=? AND user_id=?',(session_id,user_id)):
                stored=[*proposals,*[{'skill_event':notice} for notice in skill_events]]
                execute("INSERT INTO chat_messages(session_id,role,content,tool_calls,created_at) VALUES(?,'assistant',?,?,?)",(session_id,final_text,dumps(stored) if stored else None,now()))
        finally:
            _active_sessions.discard(session_id)
            _active_tasks.pop(session_id,None)
