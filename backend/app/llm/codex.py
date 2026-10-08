"""ChatGPT device authorization and direct Responses transport, independent of CLI logins."""
import asyncio
import base64
import json
import time
import uuid
from types import SimpleNamespace as NS
import httpx
from ..config import now, settings
from ..db import connect, one, execute, dumps
from .secrets import cipher
from .thinking import clean_answer

ISSUER='https://auth.openai.com'
CLIENT_ID='app_EMoamEEZ73f0CkXaXp7hrann'
BASE_URL='https://chatgpt.com/backend-api/codex'
# Initial compatibility baseline; successful catalog refreshes retain their
# verified version per connection instead of relying on this fallback forever.
CLIENT_VERSION='0.160.0'
ALLOWED_BASES={BASE_URL,'https://api.openai.com/v1'}
SCHEMA='''CREATE TABLE IF NOT EXISTS codex_auth (
 connection_id TEXT PRIMARY KEY,tokens TEXT,device TEXT,generation TEXT NOT NULL,
 status TEXT NOT NULL,updated_at TEXT NOT NULL,refresh_owner TEXT,refresh_until REAL);
'''


def seal(value):return cipher(create=True).encrypt(dumps(value).encode()).decode()
def unseal(value):return json.loads(cipher().decrypt(value.encode())) if value else {}


def claims(token):
    try:
        payload=token.split('.')[1]
        return json.loads(base64.urlsafe_b64decode(payload+'='*((-len(payload))%4)))
    except (ValueError,IndexError):return {}


def status(connection_id):
    row=one('SELECT * FROM codex_auth WHERE connection_id=?',(connection_id,))
    if not row:return {'logged_in':False,'status':'logged_out'}
    tokens=unseal(row['tokens'])
    result={'logged_in':bool(tokens.get('access_token')) and row['status']!='expired',
            'status':row['status'],'updated_at':row['updated_at']}
    device=unseal(row['device'])
    if device and device['expires_at']>time.time():
        result.update(user_code=device['user_code'],verification_url=ISSUER+'/codex/device',interval=device['interval'])
    elif row['status']=='waiting':result['status']='ready' if tokens else 'expired'
    return result


async def start_login(connection_id, client_id=CLIENT_ID):
    async with httpx.AsyncClient(timeout=20) as client:
        response=await client.post(ISSUER+'/api/accounts/deviceauth/usercode',json={'client_id':client_id})
        response.raise_for_status();data=response.json()
    if not data.get('device_auth_id') or not data.get('user_code'):raise ValueError('授权服务没有返回设备码')
    interval=max(3,int(data.get('interval',5)))
    device={**data,'client_id':client_id,'expires_at':time.time()+900,'next_poll':time.time()+interval,'interval':interval}
    generation=uuid.uuid4().hex
    with connect() as db:
        db.execute('INSERT INTO codex_auth(connection_id,device,generation,status,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(connection_id) DO UPDATE SET device=excluded.device,generation=excluded.generation,status=excluded.status,updated_at=excluded.updated_at,refresh_owner=NULL,refresh_until=NULL',
                   (connection_id,seal(device),generation,'waiting',now()))
    return {**status(connection_id),'expires_in':900}


async def poll_login(connection_id):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM codex_auth WHERE connection_id=?',(connection_id,)).fetchone()
        if not row or not row['device']:return status(connection_id)
        generation=row['generation'];device=unseal(row['device'])
        if time.time()>device['expires_at']:
            db.execute("UPDATE codex_auth SET device=NULL,status=CASE WHEN tokens IS NULL THEN 'expired' ELSE 'ready' END WHERE connection_id=?",(connection_id,))
            return {'logged_in':bool(row['tokens']),'status':'expired','message':'登录授权已超时，请重新登录'}
        if time.time()<device['next_poll']:return status(connection_id)
        device['next_poll']=time.time()+device['interval']
        db.execute('UPDATE codex_auth SET device=? WHERE connection_id=?',(seal(device),connection_id))
    async with httpx.AsyncClient(timeout=20) as client:
        response=await client.post(ISSUER+'/api/accounts/deviceauth/token',json={'device_auth_id':device['device_auth_id'],'user_code':device['user_code']})
        if response.status_code in (403,404):return status(connection_id)
        response.raise_for_status();code=response.json()
        if not code.get('authorization_code') or not code.get('code_verifier'):raise ValueError('授权服务返回了不完整的认证结果')
        result=await client.post(ISSUER+'/oauth/token',data={'grant_type':'authorization_code','code':code['authorization_code'],
                  'redirect_uri':ISSUER+'/deviceauth/callback','client_id':device['client_id'],'code_verifier':code['code_verifier']})
        result.raise_for_status();tokens=result.json()
    if not tokens.get('access_token'):raise ValueError('授权服务没有返回访问凭据')
    tokens.update(client_id=device['client_id'],expires_at=time.time()+int(tokens.get('expires_in',3600)))
    with connect() as db:
        changed=db.execute("UPDATE codex_auth SET tokens=?,device=NULL,status='ready',updated_at=?,refresh_owner=NULL,refresh_until=NULL WHERE connection_id=? AND generation=?",
                           (seal(tokens),now(),connection_id,generation)).rowcount
    if not changed:return {'logged_in':False,'status':'logged_out'}
    return status(connection_id)


def logout(connection_id):
    execute('DELETE FROM codex_auth WHERE connection_id=?',(connection_id,))
    return {'logged_in':False,'status':'logged_out'}


async def credentials(connection_id, *, force=False):
    owner=uuid.uuid4().hex
    deadline=time.monotonic()+35
    while True:
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM codex_auth WHERE connection_id=?',(connection_id,)).fetchone()
            if not row or row['status']=='expired':raise ValueError('Codex 尚未登录或授权已失效，请在模型连接页重新登录')
            tokens=unseal(row['tokens'])
            if not tokens.get('access_token'):raise ValueError('请先在模型连接页登录 Codex')
            expires=claims(tokens['access_token']).get('exp',tokens.get('expires_at',0))
            if not force and expires>time.time()+120:return tokens
            if not tokens.get('refresh_token'):raise ValueError('Codex 授权需要重新登录')
            if not row['refresh_owner'] or (row['refresh_until'] or 0)<time.time():
                db.execute('UPDATE codex_auth SET refresh_owner=?,refresh_until=? WHERE connection_id=?',(owner,time.time()+30,connection_id))
                generation=row['generation'];break
        force=False
        if time.monotonic()>deadline:raise ValueError('Codex 授权正在刷新，请稍后重试')
        await asyncio.sleep(.2)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response=await client.post(ISSUER+'/oauth/token',data={'grant_type':'refresh_token','refresh_token':tokens['refresh_token'],'client_id':tokens.get('client_id',CLIENT_ID)})
        if response.status_code in (400,401,403):
            execute("UPDATE codex_auth SET status='expired' WHERE connection_id=? AND refresh_owner=? AND generation=?",(connection_id,owner,generation))
            raise ValueError('Codex 授权已失效，请重新登录')
        response.raise_for_status();fresh=response.json()
        if not fresh.get('access_token'):raise ValueError('Codex 授权刷新未返回访问凭据')
        tokens={**tokens,**fresh,'expires_at':time.time()+int(fresh.get('expires_in',3600))}
        with connect() as db:
            changed=db.execute("UPDATE codex_auth SET tokens=?,updated_at=?,refresh_owner=NULL,refresh_until=NULL WHERE connection_id=? AND generation=? AND refresh_owner=?",(seal(tokens),now(),connection_id,generation,owner)).rowcount
        if not changed:raise ValueError('Codex 登录状态已变化，请重新发起请求')
        return tokens
    finally:
        execute('UPDATE codex_auth SET refresh_owner=NULL,refresh_until=NULL WHERE connection_id=? AND refresh_owner=?',(connection_id,owner))


def request_headers(tokens):
    headers={'Authorization':'Bearer '+tokens['access_token'],'Accept':'text/event-stream','User-Agent':'shualunwen/1.0'}
    account=claims(tokens['access_token']).get('https://api.openai.com/auth',{}).get('chatgpt_account_id')
    if account:headers['ChatGPT-Account-Id']=account
    return headers


def response_input(messages):
    instructions=[];items=[]
    for message in messages:
        role=message['role']
        if role in ('system','developer'):instructions.append(message.get('content') or '');continue
        if role=='tool':
            items.append({'type':'function_call_output','call_id':message['tool_call_id'],'output':message.get('content') or ''});continue
        if message.get('content'):items.append({'role':role,'content':message['content']})
        for tool in message.get('tool_calls') or []:
            items.append({'type':'function_call','call_id':tool['id'],'name':tool['function']['name'],'arguments':tool['function']['arguments']})
    return '\n\n'.join(instructions),items


def response_options(binding, messages, tools, json_mode):
    instructions,items=response_input(messages)
    options={'model':binding['model'],'instructions':instructions,'input':items,'store':False,'stream':True}
    if tools:options['tools']=[{'type':'function',**t['function']} for t in tools]
    caps=binding.get('control_capabilities') or {}
    effort=binding.get('reasoning_effort','auto')
    if binding.get('thinking')=='off':effort='none'
    if binding.get('thinking')=='on' and effort=='auto':
        effort=caps.get('default_reasoning_effort') or next((v for v in caps.get('reasoning_efforts',[]) if v!='none'),'auto')
    if effort!='auto':options['reasoning']={'effort':effort}
    if json_mode:
        options['instructions']+='\n仅返回所要求的 JSON 对象，不要代码块或解释。'
        if binding.get('json_schema'):options['instructions']+='\nJSON Schema: '+dumps(binding['json_schema'])
    if binding['base_url']=='https://api.openai.com/v1' and binding.get('output_tokens'):options['max_output_tokens']=binding['output_tokens']
    return options


def record_usage(binding, usage):
    usage=usage or {}
    execute('INSERT INTO llm_usage(model,input_tokens,output_tokens,created_at,feature,connection_id,usage_known,billing_source) VALUES(?,?,?,?,?,?,?,?)',
            (binding['model'],usage.get('input_tokens',0),usage.get('output_tokens',0),now(),binding.get('feature'),binding.get('id'),int(bool(usage)),'subscription'))


class CodexLLM:
    async def stream(self,messages,tools,*,json_mode=False):
        from .runtime import current_binding
        binding=current_binding();auth=await credentials(binding['id'])
        options=response_options(binding,messages,tools,json_mode)
        completed=False;usage=None;tool_indexes={};text_seen=False
        async with httpx.AsyncClient(timeout=settings().llm_timeout) as client:
            for attempt in range(2):
                async with client.stream('POST',binding['base_url']+'/responses',headers=request_headers(auth),json=options) as response:
                    if response.status_code==401 and attempt==0:
                        await response.aread()
                        auth=await credentials(binding['id'],force=True)
                        continue
                    if response.status_code>=400:
                        await response.aread()
                        raise ValueError(f'Codex 请求失败（HTTP {response.status_code}），请检查授权、模型权限或稍后重试')
                    data=[]
                    async def events():
                        async for line in response.aiter_lines():
                            if line.startswith('data:'):data.append(line[5:].lstrip())
                            elif not line and data:
                                raw='\n'.join(data);data.clear()
                                if raw!='[DONE]':yield json.loads(raw)
                        if data and '\n'.join(data)!='[DONE]':yield json.loads('\n'.join(data))
                    try:
                        async for item in events():
                            kind=item.get('type','')
                            if kind=='response.output_text.delta':
                                text_seen=True;yield NS(content=item.get('delta',''),tool_calls=None,reasoning_content=None)
                            elif kind in ('response.reasoning_summary_text.delta','response.reasoning_text.delta'):
                                yield NS(content=None,tool_calls=None,reasoning_content=item.get('delta',''))
                            elif kind=='response.output_item.added' and item.get('item',{}).get('type')=='function_call':
                                tool=item['item'];index=len(tool_indexes);tool_indexes[item.get('output_index',0)]=index
                                yield NS(content=None,reasoning_content=None,tool_calls=[NS(index=index,id=tool['call_id'],type='function',function=NS(name=tool['name'],arguments=tool.get('arguments','')))])
                            elif kind=='response.function_call_arguments.delta':
                                index=tool_indexes.get(item.get('output_index',0))
                                if index is None:raise ValueError('Codex 返回了不完整的工具调用')
                                yield NS(content=None,reasoning_content=None,tool_calls=[NS(index=index,id=None,type=None,function=NS(name=None,arguments=item.get('delta','')))])
                            elif kind=='response.completed':
                                result=item.get('response') or {};usage=result.get('usage')
                                completed=result.get('status','completed')=='completed'
                                if not text_seen and not tool_indexes:
                                    for output in result.get('output') or []:
                                        for part in output.get('content') or []:
                                            if part.get('type')=='output_text':
                                                text_seen=True;yield NS(content=part['text'],tool_calls=None,reasoning_content=None)
                            elif kind in ('error','response.failed','response.incomplete'):
                                usage=(item.get('response') or {}).get('usage')
                                raise ValueError('Codex 响应未完成，请重试或调整模型配置')
                        if not completed:raise ValueError('Codex 流式连接中断，请重试')
                        if not text_seen and not tool_indexes:raise ValueError('Codex 没有返回回答内容')
                    finally:record_usage(binding,usage)
                return

    async def complete(self,messages,*,json_mode=False,**kwargs):
        parts=[]
        async for delta in self.stream(messages,[],json_mode=json_mode):
            if delta.content:parts.append(delta.content)
        content=clean_answer(''.join(parts))
        return json.loads(content) if json_mode else content


codex=CodexLLM()
