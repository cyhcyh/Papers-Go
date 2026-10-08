from .. import prompts
import json
import secrets
from datetime import datetime, timezone
from fastapi import HTTPException
from ..db import rows, one, execute, connect, dumps
from ..config import now
from ..interest.profile import current, put_profile, profile_embedding
from ..pipeline.read import request_card
from ..api.profile import Watch, add_watch


def tool(name,description,properties,required):
    return {'type':'function','function':{'name':name,'description':description,'parameters':{
        'type':'object','properties':properties,'required':required,'additionalProperties':False}}}


TOOLS = [
 tool('update_interest','',{'patch':{'type':'string'},'topic_ids':{'type':'array','items':{'type':'integer'}}},['patch']),
 tool('add_watch','',{'type':{'type':'string','enum':['keyword','benchmark','author','topic']},'value':{'type':'string'}},['type','value']),
 tool('search_papers','',{'query':{'type':'string'}},['query']),
 tool('read_now','',{'paper_id':{'type':'integer'}},['paper_id']),
 tool('show_profile','',{},[]),
 tool('list_skills','',{'query':{'type':'string'},'include_disabled':{'type':'boolean'}},[]),
 tool('get_skill','',{'skill_id':{'type':'string'}},['skill_id']),
 tool('activate_skill','',{'skill_id':{'type':'string'}},['skill_id']),
 tool('read_skill_resource','',{'skill_id':{'type':'string'},'path':{'type':'string'}},['skill_id','path']),
 tool('create_skill','',{'name':{'type':'string'},'title':{'type':'string'},'description':{'type':'string'},'text':{'type':'string'},'allowed_tools':{'type':'array','items':{'type':'string'}},'resources':{'type':'object','additionalProperties':{'type':'string'}}},['name','title','description','text']),
 tool('update_skill','',{'skill_id':{'type':'string'},'name':{'type':'string'},'title':{'type':'string'},'description':{'type':'string'},'text':{'type':'string'},'resources':{'type':'object','additionalProperties':{'type':'string'}},'allowed_tools':{'type':'array','items':{'type':'string'}},'expected_revision':{'type':'integer'}},['skill_id','name','title','description','text','expected_revision']),
 tool('set_skill_enabled','',{'skill_id':{'type':'string'},'enabled':{'type':'boolean'}},['skill_id','enabled']),
 tool('propose_shared_skill','',{'skill_id':{'type':'string'}},['skill_id']),
]


async def execute_tool(name,args,user_id,session_id,frozen_profile):
    definition = next((t['function'] for t in TOOLS if t['function']['name']==name),None)
    if not definition:
        raise ValueError('未知工具')
    if not one('SELECT id FROM chat_sessions WHERE id=? AND user_id=?',(session_id,user_id)):
        raise HTTPException(404,'会话不存在')
    if not isinstance(args,dict) or not set(args)<=definition['parameters']['properties'].keys():
        raise ValueError('工具参数无效，用户身份由登录信息决定')
    if name in ('list_skills','get_skill','activate_skill','read_skill_resource','create_skill','update_skill','set_skill_enabled','propose_shared_skill'):
        from .. import agent_skills as skills
        if name=='list_skills':
            include_disabled=args.get('include_disabled',False)
            if not isinstance(include_disabled,bool):raise ValueError('查询停用技能的标志必须为布尔值')
            return skills.available(user_id,str(args.get('query','')),include_disabled=include_disabled)
        if name=='get_skill':return skills.inspect(args['skill_id'],user_id)
        if name=='activate_skill':return skills.activate(args['skill_id'],user_id,session_id)
        if name=='read_skill_resource':return skills.resource(args['skill_id'],args['path'],user_id,session_id)
        if name=='propose_shared_skill':return skills.propose(args['skill_id'],user_id)
        if name=='set_skill_enabled':
            if not isinstance(args['enabled'],bool):raise ValueError('启用状态必须为布尔值')
            result=skills.set_enabled(args['skill_id'],args['enabled'],user_id)
        else:
            payload={k:v for k,v in args.items() if k!='skill_id'}
            if name=='update_skill':
                original=skills.detail(args['skill_id'],user_id)
                for key in ('resources','allowed_tools'):
                    if key not in payload:payload[key]=original[key]
            body=skills.SkillEdit.model_validate(payload)
            result=skills.create(body,user_id) if name=='create_skill' else skills.save(args['skill_id'],body,user_id)
        return {k:result[k] for k in ('id','name','title','enabled','revision')}
    if name in ('update_interest','add_watch'):
        if name=='update_interest':
            patch = args.get('patch')
            if not isinstance(patch,str) or not patch.strip() or len(patch)>12000:
                raise ValueError('画像修改内容无效')
            if 'topic_ids' in args:
                valid = {r['id'] for r in rows("SELECT id FROM topics WHERE status='active'")}
                if not isinstance(args['topic_ids'],list) or not set(args['topic_ids'])<=valid:
                    raise ValueError('主题选择无效')
        else:
            Watch.model_validate(args)
        ident = secrets.token_urlsafe(24)
        execute('INSERT INTO pending_tools(id,user_id,session_id,name,arguments,profile_version,created_at) VALUES(?,?,?,?,?,?,?)',
            (ident,user_id,session_id,name,dumps(args),frozen_profile['version'] if frozen_profile else 0,now()))
        preview = {'id':ident,'name':name,'arguments':args,'status':'pending',
                   'before':frozen_profile['content'] if name=='update_interest' and frozen_profile else ''}
        return {'requires_confirmation':True,'proposal':preview}
    if name=='show_profile':
        return {'content':frozen_profile['content'] if frozen_profile else '尚未设置画像'}
    if name=='search_papers':
        query = str(args.get('query',''))[:200]
        from ..paper_index import search_clause
        from .references import reference
        clause,parameters=search_clause(query)
        found=rows('SELECT p.id,p.title,p.abstract,p.tldr,p.published,p.brief_json,p.abs_url FROM papers p WHERE '+clause+' ORDER BY quality_score DESC LIMIT 10',parameters)
        return [{**{k:p[k] for k in ('id','abstract','tldr','published')},**reference(p)} for p in found]
    if name=='read_now':
        ident = int(args['paper_id'])
        paper=one('SELECT title,brief_json,abs_url FROM papers WHERE id=?',(ident,))
        if not paper: raise ValueError('论文不存在')
        from .references import reference
        user=one('SELECT is_admin FROM users WHERE id=?',(user_id,))
        cached=one('SELECT card_json FROM reading_cards WHERE paper_id=?',(ident,))
        is_admin=bool(user and user['is_admin'])
        card = request_card(ident,retry=is_admin or not (cached and cached['card_json']),allow_regenerate=is_admin)
        return {'paper_id':ident,**reference(paper),'status':card['status'],'card':json.loads(card['card_json']) if card['card_json'] else None}
    raise ValueError('未知工具')


async def confirm_tool(ident,user_id,approve):
    pending = one('SELECT * FROM pending_tools WHERE id=? AND user_id=?',(ident,user_id))
    if not pending: raise HTTPException(404,'确认操作不存在')
    if pending['status']!='pending': raise HTTPException(409,'该操作已处理')
    if (datetime.now(timezone.utc)-datetime.fromisoformat(pending['created_at'])).total_seconds()>86400:
        raise HTTPException(409,'确认操作已过期，请重新提出')
    args = json.loads(pending['arguments'])
    vector = await profile_embedding(args['patch']) if approve and pending['name']=='update_interest' else None
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        item = db.execute("SELECT * FROM pending_tools WHERE id=? AND user_id=? AND status='pending'",(ident,user_id)).fetchone()
        if not item: raise HTTPException(409,'该操作已处理')
        if approve:
            if item['name']=='update_interest':
                profile = db.execute('SELECT * FROM interest_profile WHERE user_id=? ORDER BY version DESC LIMIT 1',(user_id,)).fetchone()
                if (profile['version'] if profile else 0)!=item['profile_version']:
                    raise HTTPException(409,'画像已更新，请让助手根据最新画像重新提出修改')
                structured = json.loads(profile['structured']) if profile else {}
                if 'topic_ids' in args:
                    structured['topic_ids']=args['topic_ids']
                    structured.pop('category_selection',None)
                result = put_profile(user_id,args['patch'],structured,'chat_confirmed',vector,db)
            else:
                validated = Watch.model_validate(args)
                result = add_watch(user_id,validated.type,validated.value,db)
        else:
            result = {'rejected':True}
        status = 'confirmed' if approve else 'rejected'
        db.execute('UPDATE pending_tools SET status=? WHERE id=?',(status,ident))
        db.execute("INSERT INTO chat_messages(session_id,role,content,created_at) VALUES(?,'assistant',?,?)",(item['session_id'],'已确认并应用。' if approve else '已取消这项修改。',now()))
    return {'status':status,**result}


def tool_definitions():
    return [{**item,'function':{**item['function'],'description':prompts.get('tool_'+item['function']['name'])}} for item in TOOLS]
