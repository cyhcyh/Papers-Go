import assert from 'node:assert/strict'
import {readFile} from 'node:fs/promises'
import {runInNewContext} from 'node:vm'
import ts from 'typescript'

const source=ts.transpileModule(await readFile(new URL('../src/api.ts',import.meta.url),'utf8'),{
 compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022},
}).outputText
const initial={access_token:'mock-access-old',refresh_token:'mock-refresh-old',user:{id:1,username:'simulation'}}
const renewed={...initial,access_token:'mock-access-new',refresh_token:'mock-refresh-new'}
const other={access_token:'mock-access-other',refresh_token:'mock-refresh-other',user:{id:2,username:'other'}}
const response=(status,body={ok:true})=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}})
const deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done});return {promise,resolve}}
function storage(){
 const values=new Map([['auth',JSON.stringify(initial)]])
 return {getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)}
}
function runtime(fetch,store=storage(),locks){
 const exports={},events=[]
 runInNewContext(source,{exports,localStorage:store,window:{dispatchEvent:event=>events.push(event.type)},fetch,Headers,Event,navigator:locks?{locks}:{}})
 return {...exports,events}
}
function locks(){
 let pending=Promise.resolve()
 return {request:(_name,callback)=>{
  const next=pending.then(callback)
  pending=next.catch(()=>{})
  return next
 }}
}
const fresh=init=>init.headers.get('Authorization')==='Bearer '+renewed.access_token
let count=0
async function check(name,run){await run();count++;console.log('Passed: '+name)}

await check('expired access token renews and retries once',async()=>{
 let refreshes=0,requests=0
 const client=runtime(async(url,init)=>{
  if(url==='/api/auth/refresh'){refreshes++;return response(200,renewed)}
  requests++;return response(fresh(init)?200:401)
 })
 assert.deepEqual(await client.api('/normal'),{ok:true})
 assert.equal(refreshes,1);assert.equal(requests,2)
 assert.equal(client.readAuth().access_token,renewed.access_token)
})

await check('simultaneous expired requests share one refresh',async()=>{
 const started=deferred(),finish=deferred();let refreshes=0
 const client=runtime(async(url,init)=>{
  if(url==='/api/auth/refresh'){refreshes++;started.resolve();await finish.promise;return response(200,renewed)}
  return response(fresh(init)?200:401)
 })
 const first=client.api('/first'),second=client.api('/second')
 await started.promise;finish.resolve();await Promise.all([first,second])
 assert.equal(refreshes,1);assert.ok(client.readAuth())
})

await check('a late 401 reuses credentials already renewed',async()=>{
 const finish=deferred();let refreshes=0
 const client=runtime(async(url,init)=>{
  if(url==='/api/auth/refresh'){refreshes++;return response(200,renewed)}
  if(url==='/api/slow'&&!fresh(init))await finish.promise
  return response(fresh(init)?200:401)
 })
 const first=client.api('/fast'),second=client.api('/slow')
 await first;finish.resolve();await second
 assert.equal(refreshes,1);assert.ok(client.readAuth())
})

await check('network failure preserves login and permits later recovery',async()=>{
 let refreshes=0
 const client=runtime(async(url,init)=>{
  if(url==='/api/auth/refresh'){
   if(++refreshes===1)throw new TypeError('simulated network failure')
   return response(200,renewed)
  }
  return response(fresh(init)?200:401)
 })
 await assert.rejects(client.api('/offline'),/simulated network failure/)
 assert.equal(client.readAuth().refresh_token,initial.refresh_token)
 await client.api('/recovered')
 assert.equal(refreshes,2);assert.equal(client.readAuth().access_token,renewed.access_token)
})

for(const status of [429,500,503])await check('refresh HTTP '+status+' preserves login',async()=>{
 const client=runtime(async url=>response(url==='/api/auth/refresh'?status:401))
 await assert.rejects(client.api('/temporary'),/暂时失败/)
 assert.equal(client.readAuth().refresh_token,initial.refresh_token)
 assert.equal(client.events.length,0)
})

for(const status of [401,403])await check('refresh HTTP '+status+' clears invalid credentials',async()=>{
 const client=runtime(async url=>response(url==='/api/auth/refresh'?status:401))
 await assert.rejects(client.api('/invalid'),/登录已过期/)
 assert.equal(client.readAuth(),null)
 assert.equal(client.events.length,1)
})

await check('failed request retry does not clear renewed credentials',async()=>{
 const client=runtime(async(url,init)=>{
  if(url==='/api/auth/refresh')return response(200,renewed)
  if(fresh(init))throw new TypeError('simulated retry failure')
  return response(401)
 })
 await assert.rejects(client.api('/retry'),/simulated retry failure/)
 assert.equal(client.readAuth().access_token,renewed.access_token)
})

await check('cancelled request retry preserves renewed credentials',async()=>{
 const client=runtime(async(url,init)=>{
  if(url==='/api/auth/refresh')return response(200,renewed)
  if(fresh(init))throw new DOMException('simulated cancellation','AbortError')
  return response(401)
 })
 await assert.rejects(client.api('/cancelled'),{name:'AbortError'})
 assert.equal(client.readAuth().access_token,renewed.access_token)
})

await check('malformed refresh response preserves existing login',async()=>{
 const client=runtime(async url=>url==='/api/auth/refresh'?new Response('invalid JSON',{status:200}):response(401))
 await assert.rejects(client.api('/malformed'))
 assert.equal(client.readAuth().refresh_token,initial.refresh_token)
})

await check('logout while refresh is running cannot restore login',async()=>{
 const started=deferred(),finish=deferred()
 const client=runtime(async url=>{
  if(url==='/api/auth/refresh'){started.resolve();await finish.promise;return response(200,renewed)}
  return response(401)
 })
 const pending=client.api('/logout-race')
 const rejected=assert.rejects(pending,/登录状态已变化/)
 await started.promise;client.storeAuth(null);finish.resolve();await rejected
 assert.equal(client.readAuth(),null)
})

for(const status of [200,401])await check('an old refresh HTTP '+status+' cannot replace or clear another account',async()=>{
 const started=deferred(),finish=deferred()
 const client=runtime(async url=>{
  if(url==='/api/auth/refresh'){started.resolve();await finish.promise;return response(status,renewed)}
  return response(401)
 })
 const pending=client.api('/account-race'),rejected=assert.rejects(pending)
 await started.promise;client.storeAuth(other);finish.resolve();await rejected
 assert.equal(client.readAuth().access_token,other.access_token)
})

await check('a late old-account request is not replayed under another account',async()=>{
 const started=deferred(),finish=deferred();let refreshes=0,requests=0
 const client=runtime(async url=>{
  if(url==='/api/auth/refresh'){refreshes++;return response(200,renewed)}
  requests++;started.resolve();await finish.promise;return response(401)
 })
 const pending=client.api('/old-account','POST',{action:'save'}),rejected=assert.rejects(pending,/登录状态已变化/)
 await started.promise;client.storeAuth(other);finish.resolve();await rejected
 assert.equal(refreshes,0);assert.equal(requests,1)
 assert.equal(client.readAuth().user.id,other.user.id)
})

await check('browser tabs coordinate one refresh with shared storage',async()=>{
 const started=deferred(),finish=deferred(),store=storage(),mutex=locks();let refreshes=0
 const fetch=async(url,init)=>{
  if(url==='/api/auth/refresh'){refreshes++;started.resolve();await finish.promise;return response(200,renewed)}
  return response(fresh(init)?200:401)
 }
 const first=runtime(fetch,store,mutex),second=runtime(fetch,store,mutex)
 const a=first.api('/tab-a'),b=second.api('/tab-b')
 await started.promise;finish.resolve();await Promise.all([a,b])
 assert.equal(refreshes,1)
 assert.equal(first.readAuth().access_token,renewed.access_token)
 assert.equal(second.readAuth().access_token,renewed.access_token)
})

await check('a rejected refresh cannot clear a newer same-account session',async()=>{
 const started=deferred(),finish=deferred();let requests=0
 const client=runtime(async(url,init)=>{
  if(url==='/api/auth/refresh'){started.resolve();await finish.promise;return response(401)}
  requests++;return response(fresh(init)?200:401)
 })
 const pending=client.api('/new-session')
 await started.promise;client.storeAuth(renewed);finish.resolve();await pending
 assert.equal(client.readAuth().access_token,renewed.access_token)
 assert.equal(requests,2)
})

console.log(`Authentication checks passed: ${count} scenarios.`)
