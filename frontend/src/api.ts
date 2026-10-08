import type {User} from './types'
export type Auth = {access_token:string;refresh_token:string;user:User;admin_entry?:string}
export class ApiError extends Error {
 constructor(message:string,public detail:unknown){super(message);this.name='ApiError'}
}
export function readAuth():Auth|null {try{return JSON.parse(localStorage.getItem('auth')||'null')}catch{return null}}
export function storeAuth(auth:Auth|null) {if(auth){const {admin_entry,...session}=auth;localStorage.setItem('auth',JSON.stringify(session))}else localStorage.removeItem('auth');window.dispatchEvent(new Event('auth-change'))}
let refreshRequest:Promise<Auth>|null = null
function activeSession(expected:Auth):Auth {
 const current=readAuth()
 if(!current||current.user.id!==expected.user.id)throw new Error('登录状态已变化，请重新操作')
 return current
}
async function refreshSession(expired:Auth):Promise<Auth> {
 const current=activeSession(expired)
 if(current.access_token!==expired.access_token)return current
 if(!refreshRequest){
  const renew=async()=>{
   const session=activeSession(expired)
   if(session.access_token!==expired.access_token)return session
   const response=await fetch('/api/auth/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({refresh_token:session.refresh_token})})
   if(!response.ok){
    const latest=readAuth()
    if(latest?.user.id===session.user.id&&latest.refresh_token!==session.refresh_token)return latest
    if(response.status===401||response.status===403){
     if(latest?.access_token===session.access_token&&latest.refresh_token===session.refresh_token)storeAuth(null)
     throw new Error('登录已过期，请重新登录')
    }
    throw new Error('登录续期暂时失败，请稍后重试')
   }
   const next:Auth=await response.json()
   const latest=activeSession(session)
   if(latest.refresh_token!==session.refresh_token)return latest
   storeAuth(next)
   return next
  }
  // The refresh token is single-use; coordinate both requests and browser tabs.
  const operation=async()=>typeof navigator!=='undefined'&&navigator.locks
   ?await navigator.locks.request('shualunwen-auth-refresh',renew):await renew()
  refreshRequest=operation().finally(()=>{refreshRequest=null})
 }
 return refreshRequest
}
async function request(path:string,init:RequestInit={}) {
  const auth = readAuth()
  const headers = new Headers(init.headers)
  if(init.body)headers.set('Content-Type','application/json')
  if(auth)headers.set('Authorization',`Bearer ${auth.access_token}`)
  let response = await fetch('/api'+path,{...init,headers})
  if(response.status===401 && auth && !path.startsWith('/auth/')) {
    await refreshSession(auth)
    const next=activeSession(auth);headers.set('Authorization',`Bearer ${next.access_token}`)
    response=await fetch('/api'+path,{...init,headers})
  }
  if(!response.ok) {
    const data=await response.json().catch(()=>({detail:'网络请求失败'}))
    throw new ApiError(typeof data.detail==='string'?data.detail:typeof data.detail?.message==='string'?data.detail.message:'输入格式不正确，请检查后重试',data.detail)
  }
  return response
}
export async function api<T=unknown>(path:string,method='GET',body?:unknown,signal?:AbortSignal):Promise<T> {
 const value=await (await request(path,{method,body:body===undefined?undefined:JSON.stringify(body),signal})).json()
 if(path==='/interactions'&&method==='POST'&&typeof value.like_count==='number'&&typeof value.save_count==='number')window.dispatchEvent(new CustomEvent('paper-feedback',{detail:value}))
 if(method!=='GET'&&(['/profile','/profile/init','/profile/rollback'].includes(path)||(path==='/chat/confirm'&&value.version)))window.dispatchEvent(new Event('profile-change'))
 return value
}
export async function streamChat(path:string,body:unknown,onEvent:(event:string,data:any)=>void,signal?:AbortSignal) {
  const response=await request(path,{method:'POST',body:JSON.stringify(body),signal})
  if(!response.body)throw new Error('服务器未返回流式内容')
  const reader=response.body.getReader(),decoder=new TextDecoder();let buffer=''
  const frame=(text:string)=>{const event=text.split('\n').find(line=>line.startsWith('event:'))?.slice(6).trim()||'message';const data=text.split('\n').filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n');if(data)onEvent(event,JSON.parse(data))}
  try{
    while(true){const {done,value}=await reader.read();buffer+=done?decoder.decode():decoder.decode(value,{stream:true});const frames=buffer.replace(/\r\n/g,'\n').split('\n\n');buffer=frames.pop()||'';for(const text of frames)frame(text);if(done){if(buffer.trim())frame(buffer);break}}
  }finally{reader.releaseLock()}
}
