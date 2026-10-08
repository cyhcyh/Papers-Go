import {MathText} from '../components/MathText'
import {ResearchThread} from '../components/assistant-ui/Thread'
import {useViewportPanel} from '../hooks/useViewportPanel'
import {AssistantRuntimeProvider,useExternalStoreRuntime,type ThreadMessageLike} from '@assistant-ui/react'
import {useEffect,useRef,useState} from 'react'
import {Link,useSearchParams} from 'react-router-dom'
import {Plus,FileText,MessageCircle,Check,X,Trash2,PanelLeft,FolderOpen,CheckSquare} from 'lucide-react'
import {api,streamChat} from '../api'
import {useApp} from '../context'
import {PageTitle} from '../components/Common'
import type {Message,Proposal,Paper} from '../types'

interface Session {id:number;title:string}
export function Chat(){
 const {toast}=useApp(),[params,setParams]=useSearchParams(),paperId=Number(params.get('paper'))||null
 const [sessions,setSessions]=useState<Session[]>([]),[session,setSession]=useState<number|null>(null),[messages,setMessages]=useState<Message[]>([]),[busy,setBusy]=useState(false),[error,setError]=useState(''),[paper,setPaper]=useState<Paper|null>(null),[confirming,setConfirming]=useState<string|null>(null)
 const [drawerOpen,setDrawerOpen]=useState(false),[deleteTarget,setDeleteTarget]=useState<Session[]>([]),[managing,setManaging]=useState(false),[checkedIds,setCheckedIds]=useState<number[]>([]),[deleting,setDeleting]=useState(false)
 const viewportPanel=useViewportPanel()
 const abort=useRef<AbortController|null>(null),selected=useRef<number|null>(null),streaming=useRef(false),revision=useRef(0),deleted=useRef(new Set<number>()),drawer=useRef<HTMLDialogElement>(null),deleteDialog=useRef<HTMLDialogElement>(null)
 const selectSession=(id:number|null)=>{setDrawerOpen(false);if(id!==null&&id===selected.current)return;revision.current++;selected.current=id;setSession(id);setError('');setMessages([])}
 const loadSessions=()=>api<Session[]>('/chat/sessions').then(setSessions).catch(e=>setError(e.message))
 useEffect(()=>{void loadSessions();return ()=>abort.current?.abort()},[])
 useEffect(()=>{if(paperId)api<Paper>('/papers/'+paperId).then(setPaper).catch(e=>setError(e.message));else setPaper(null)},[paperId])
 useEffect(()=>{if(!session||streaming.current)return;const version=revision.current,controller=new AbortController();api<Message[]>(`/chat/sessions/${session}/messages`,'GET',undefined,controller.signal).then(data=>{if(revision.current===version&&selected.current===session&&!streaming.current&&!deleted.current.has(session))setMessages(data)}).catch(e=>{if(e.name!=='AbortError'&&revision.current===version)setError(e.message)});return()=>controller.abort()},[session])
 useEffect(()=>{const node=drawer.current;if(drawerOpen&&!node?.open)node?.showModal();if(!drawerOpen&&node?.open)node.close()},[drawerOpen])
 useEffect(()=>{const node=deleteDialog.current;if(deleteTarget.length&&!node?.open)node?.showModal();if(!deleteTarget.length&&node?.open)node.close()},[deleteTarget])
 useEffect(()=>{const media=window.matchMedia('(min-width: 601px)'),close=()=>{if(media.matches)setDrawerOpen(false)};media.addEventListener('change',close);return ()=>media.removeEventListener('change',close)},[])
 const confirm=async(proposal:Proposal,approve:boolean)=>{setConfirming(proposal.id);try{const result=await api<{status:string}>('/chat/confirm','POST',{id:proposal.id,approve});revision.current++;setMessages(list=>list.map(m=>({...m,proposals:m.proposals?.map(p=>p.id===proposal.id?{...p,status:result.status}:p)})));toast(approve?'修改已应用':'已取消修改');window.dispatchEvent(new Event('stats-change'))}catch(e){toast((e as Error).message)}finally{setConfirming(null)}}
 const stop=async()=>{if(!session)return;try{await api(`/chat/sessions/${session}/stop`,'POST');abort.current?.abort()}catch(e){toast((e as Error).message)}}
 const removeSession=async()=>{if(!deleteTarget.length||deleting)return;setDeleting(true);try{const result=await api<{deleted:number[]}>('/chat/sessions/batch-delete','POST',{ids:deleteTarget.map(s=>s.id)});for(const id of result.deleted)deleted.current.add(id);if(selected.current!==null&&result.deleted.includes(selected.current)){abort.current?.abort();selectSession(null)}setSessions(list=>list.filter(s=>!result.deleted.includes(s.id)));setCheckedIds(list=>list.filter(id=>!result.deleted.includes(id)));setDeleteTarget([]);toast(`已删除 ${result.deleted.length} 个会话`)}catch(e){toast((e as Error).message)}finally{setDeleting(false)}}

 const send=async(content:string)=>{content=content.trim();if(!content||streaming.current)return;revision.current++;streaming.current=true;setBusy(true);setError('');let currentSession=selected.current
  try{
   if(!currentSession){const created=await api<Session>('/chat/sessions','POST',{title:content.slice(0,30)});
    // SQLite may reuse a deleted session's INTEGER PRIMARY KEY. A successful
    // creation starts a new lifetime, so its old deletion marker must expire.
    deleted.current.delete(created.id);currentSession=created.id;selected.current=created.id;setSession(created.id);await loadSessions()}
   setMessages(list=>[...list,{role:'user',content},{role:'assistant',content:'',proposals:[]}]);const controller=new AbortController();abort.current=controller
   await streamChat(`/chat/sessions/${currentSession}/messages`,{content,paper_id:paperId},(event,data)=>{
    if(controller.signal.aborted||selected.current!==currentSession||deleted.current.has(currentSession!))return
    if(event==='error')setError(data.message)
    if(event==='delta'||event==='confirm'||event==='skill')setMessages(list=>{const next=[...list],last={...next[next.length-1]};if(event==='delta')last.content+=data.text;else if(event==='skill')last.skill_events=[...(last.skill_events||[]),data];else last.proposals=[...(last.proposals||[]),data];next[next.length-1]=last;return next})
   },controller.signal)
  }catch(e){if((e as Error).name!=='AbortError')setError((e as Error).message)}finally{
   streaming.current=false;setBusy(false);abort.current=null;const version=revision.current;await loadSessions()
   if(currentSession&&!deleted.current.has(currentSession)&&selected.current===currentSession&&revision.current===version)api<Message[]>(`/chat/sessions/${currentSession}/messages`).then(data=>{if(revision.current===version&&selected.current===currentSession&&!streaming.current&&!deleted.current.has(currentSession!))setMessages(data)}).catch(()=>{})
  }
 }
 const runtime=useExternalStoreRuntime<Message>({messages,isRunning:busy,convertMessage:(m,index):ThreadMessageLike=>({id:String(m.id||`${session||'new'}-${index}`),role:m.role==='user'?'user':'assistant',content:[{type:'text',text:m.content}],metadata:{custom:{proposals:m.proposals||[],skill_events:m.skill_events||[]}},...(m.role==='assistant'?{status:busy&&index===messages.length-1?{type:'running'}:{type:'complete',reason:'stop'}}:{})}),onNew:async message=>send(message.content.filter(part=>part.type==='text').map(part=>'text' in part?part.text:'').join('')),onCancel:stop})
 const sessionList=<><div className="session-controls"><button className="full primary" disabled={busy} onClick={()=>selectSession(null)}><Plus size={15}/>新对话</button><button disabled={deleting} onClick={()=>{setManaging(!managing);setCheckedIds([])}}><FolderOpen size={15}/>{managing?'完成':'管理'}</button></div>{managing&&<div className="batch-toolbar session-batch"><label><input type="checkbox" aria-label="全选全部会话" checked={sessions.length>0&&checkedIds.length===sessions.length} onChange={e=>setCheckedIds(e.target.checked?sessions.map(s=>s.id):[])}/>全选全部（{sessions.length}项）</label><span>已选 {checkedIds.length} 项</span>{!!checkedIds.length&&<button disabled={deleting} onClick={()=>setCheckedIds([])}>取消选择</button>}<button className="danger" disabled={!checkedIds.length||deleting} onClick={()=>setDeleteTarget(sessions.filter(s=>checkedIds.includes(s.id)))}>删除</button></div>}{sessions.map(s=><div key={s.id} className={'session-row'+(session===s.id?' active':'')}>{managing&&<input type="checkbox" aria-label={'选择会话：'+s.title} checked={checkedIds.includes(s.id)} onChange={e=>setCheckedIds(list=>e.target.checked?[...list,s.id]:list.filter(id=>id!==s.id))}/>}<button disabled={busy} onClick={()=>selectSession(s.id)}><MessageCircle size={15}/><span>{s.title}</span></button>{!managing&&<button className="session-delete icon-button" aria-label={'删除会话：'+s.title} onClick={()=>setDeleteTarget([s])}><Trash2 size={14}/></button>}</div>)}</>

 return <AssistantRuntimeProvider runtime={runtime}><div className="research-chat-content"><PageTitle eyebrow="TALK WITH AGENT" title="智能助手" description="追问论文，调整方向，把线索变成下一步。"/>
  <div className="mobile-chat-toolbar"><button aria-label="打开会话列表" onClick={()=>setDrawerOpen(true)}><PanelLeft size={18}/>会话</button><span>{sessions.find(s=>s.id===session)?.title||'新对话'}</span></div>
  <div ref={viewportPanel} className="chat-layout viewport-panel"><aside className="session-list desktop-sessions research-card content-card">{sessionList}</aside><ResearchThread empty={!messages.length} running={busy} error={error} context={paper?<div className="paper-context"><FileText size={16}/><span><MathText inline>{paper.title}</MathText></span><button aria-label="清除论文上下文" onClick={()=>setParams({})}><X size={14}/></button></div>:null} skillEvent={event=><div key={event.id+event.action} className="confirm-card skill-chat-notice"><div className="confirm-label"><CheckSquare size={15}/>{({create_skill:'已创建并启用',update_skill:'技能已更新',activate_skill:'已加载技能',set_skill_enabled:event.enabled?'技能已启用':'技能已停用',propose_shared_skill:'共享提议待审核'} as Record<string,string>)[event.action]} · {event.title}</div><Link to={'/settings?tab=skills&skill='+encodeURIComponent(event.id)}>查看技能</Link></div>} proposal={p=><div key={p.id} className="confirm-card"><div className="confirm-label">{p.name==='update_interest'?'拟修改兴趣画像':'拟添加监视项'}</div>{p.name==='update_interest'?<><details><summary>查看当前画像</summary><pre>{p.before}</pre></details><pre>{String(p.arguments.patch)}</pre></>:<p>{String(p.arguments.type)}：{String(p.arguments.value)}</p>}{p.status==='pending'?<div className="confirm-actions"><button className="primary" disabled={confirming===p.id} onClick={()=>confirm(p,true)}><Check size={14}/>确认应用</button><button disabled={confirming===p.id} onClick={()=>confirm(p,false)}>取消</button></div>:<small className="muted">{p.status==='confirmed'?'已确认并应用':'已取消'}</small>}</div>}/></div>
  <dialog ref={drawer} className="session-drawer" aria-label="智能助手会话列表" onCancel={()=>setDrawerOpen(false)} onClose={()=>setDrawerOpen(false)} onClick={e=>{if(e.target===e.currentTarget)setDrawerOpen(false)}}><div className="drawer-heading"><strong>智能助手</strong><button className="icon-button" aria-label="收起会话列表" onClick={()=>setDrawerOpen(false)}><X size={18}/></button></div><aside className="session-list">{sessionList}</aside></dialog>
  <dialog ref={deleteDialog} className="delete-session-dialog" aria-labelledby="delete-session-title" onCancel={e=>{if(deleting)e.preventDefault();else setDeleteTarget([])}} onClose={()=>{if(!deleting)setDeleteTarget([])}}><h3 id="delete-session-title">删除 {deleteTarget.length} 个会话？</h3><p>{deleteTarget.map(s=>s.title).join('、')}</p><p className="muted">将停止这些会话的回复，并删除聊天记录和未确认的修改。已经确认的兴趣设置会保留。</p><div className="confirm-actions"><button disabled={deleting} onClick={()=>setDeleteTarget([])}>取消</button><button className="danger" disabled={deleting} onClick={()=>void removeSession()}>{deleting?'正在删除…':'删除会话'}</button></div></dialog>
 </div></AssistantRuntimeProvider>
}
