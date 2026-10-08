import {useInterestUpdate} from '../hooks/useInterestUpdate'
import {useCallback,useEffect,useRef,useState} from 'react'
import {Undo2,RefreshCw,X,ArrowDown,ArrowUp,Sparkles} from 'lucide-react'
import {api} from '../api'
import {useApp} from '../context'
import {useHotkeys} from '../hooks/useHotkeys'
import {usePaperDwell} from '../hooks/usePaperDwell'
import {useTopicRefresh} from '../hooks/useTopicRefresh'
import {PaperCard} from '../components/PaperCard'
import {Loading,ErrorBox,Empty,Modal} from '../components/Common'
import {ReadingModal} from '../components/ReadingModal'
import {Recommendations} from '../components/Recommendations'
import type {Paper,InteractionResult} from '../types'

type Feedback='skip'|'like'|'save'|'remove_like'|'remove_save'
const context='today'
export function Feed(){
 const {toast,auth,requireLogin}=useApp(),[items,setItems]=useState<Paper[]>([]),[loading,setLoading]=useState(true),[error,setError]=useState(''),[hasMore,setHasMore]=useState(false),[moreLoading,setMoreLoading]=useState(false),[active,setActive]=useState(0),[reading,setReading]=useState<Paper|null>(null),[suggestions,setSuggestions]=useState(false),[busy,setBusy]=useState<number|null>(null),[undo,setUndo]=useState<{id:number;paper:Paper;index:number;expires:number}|null>(null),[mobile,setMobile]=useState(()=>matchMedia('(max-width:600px)').matches),[twoColumns,setTwoColumns]=useState(()=>matchMedia('(min-width:1351px), (min-width:761px) and (max-width:1150px)').matches)
 const profileUpdate=useInterestUpdate(auth?'/profile':null),interestStatus=profileUpdate.data?.update?.status
 const scroll=useRef<HTMLDivElement>(null),sentinel=useRef<HTMLDivElement>(null),preferred=useRef<number|null>(null),lock=useRef(false),moreLock=useRef(false)
 const requestVersion=useRef(0),requestController=useRef<AbortController|null>(null)
 useEffect(()=>{const query=matchMedia('(max-width:600px)'),columns=matchMedia('(min-width:1351px), (min-width:761px) and (max-width:1150px)'),update=()=>{setMobile(query.matches);setTwoColumns(columns.matches)};query.addEventListener('change',update);columns.addEventListener('change',update);return()=>{query.removeEventListener('change',update);columns.removeEventListener('change',update)}},[])
 const load=useCallback(async()=>{const version=++requestVersion.current;requestController.current?.abort();const controller=new AbortController();requestController.current=controller;setLoading(true);setError('');try{const data=await api<{items:Paper[];total:number;personalization?:{has_profile:boolean;profile_ready:boolean;pending_paper_vectors:number}}>(`/feed/${context}?limit=20`,'GET',undefined,controller.signal);if(version!==requestVersion.current)return;setItems(data.items);setHasMore(data.total>data.items.length);setActive(data.items[0]?.id||0);setUndo(null);preferred.current=null;scroll.current?.scrollTo({top:0})}catch(e){if((e as Error).name!=='AbortError'&&version===requestVersion.current)setError((e as Error).message)}finally{if(version===requestVersion.current&&!controller.signal.aborted)setLoading(false)}},[context,auth?.user.id])
 useTopicRefresh(load)
 useEffect(()=>{void load();const update=()=>{void load()};window.addEventListener('profile-change',update);return()=>{requestController.current?.abort();requestVersion.current++;window.removeEventListener('profile-change',update)}},[load])
 const loadMore=useCallback(async()=>{if(moreLock.current)return;const version=requestVersion.current;moreLock.current=true;setMoreLoading(true);try{const exclude=items.map(p=>p.id).join(',');const data=await api<{items:Paper[];total:number}>(`/feed/${context}?limit=20&exclude=${exclude}`);if(version!==requestVersion.current)return;setItems(list=>[...list,...data.items.filter(p=>!list.some(old=>old.id===p.id))]);setHasMore(data.total>data.items.length)}catch(e){if(version===requestVersion.current)toast((e as Error).message)}finally{moreLock.current=false;setMoreLoading(false)}},[context,items,toast])
 const choosePaper=useCallback(()=>{
  const root=scroll.current;if(!root)return;const rect=root.getBoundingClientRect(),center=(rect.top+rect.bottom)/2
  const candidates=[...root.querySelectorAll<HTMLElement>('[data-paper]')].map(node=>{const bounds=node.getBoundingClientRect(),visible=Math.max(0,Math.min(bounds.bottom,rect.bottom)-Math.max(bounds.top,rect.top));return {id:Number(node.dataset.paper),ratio:visible/Math.max(1,bounds.height),distance:Math.abs((bounds.top+bounds.bottom)/2-center)}}).filter(p=>p.ratio>=.55).sort((a,b)=>a.distance-b.distance)
  const chosen=candidates.find(p=>p.id===preferred.current)||candidates[0];setActive(chosen?.id||0)
 },[])
 const focusPaper=(id:number)=>{preferred.current=id;choosePaper()}
 const dwell=usePaperDwell(!loading&&!reading&&!suggestions&&active?active:null,context)
 useEffect(()=>{
  if(loading||!items.length)return;const root=scroll.current;if(!root)return
  const update=()=>{preferred.current=null;choosePaper()};update()
  root.addEventListener('scroll',update,{passive:true});window.addEventListener('resize',update)
  return()=>{root.removeEventListener('scroll',update);window.removeEventListener('resize',update)}
 },[items.length,loading,mobile,twoColumns,choosePaper])
 useEffect(()=>{if(!hasMore||loading||!sentinel.current)return;const observer=new IntersectionObserver(entries=>{if(entries.some(e=>e.isIntersecting))void loadMore()},{root:scroll.current,rootMargin:'150px'});observer.observe(sentinel.current);return()=>observer.disconnect()},[hasMore,loading,loadMore,mobile])
 useEffect(()=>{if(undo){const timer=setTimeout(()=>setUndo(null),Math.max(0,undo.expires-Date.now()));return()=>clearTimeout(timer)}},[undo])
 const act=async(paper:Paper,action:Feedback)=>{if(lock.current||reading)return;if(!requireLogin('保存阅读反馈',()=>{void act(paper,action)}))return;lock.current=true;setBusy(paper.id);const index=items.findIndex(p=>p.id===paper.id);try{const event=await api<InteractionResult>('/interactions','POST',{paper_id:paper.id,action,dwell_ms:dwell(paper.id),feed_context:context});setItems(list=>action==='skip'?list.filter(p=>p.id!==paper.id):list.map(p=>p.id===paper.id?{...p,liked:event.liked,saved:event.saved,like_count:event.like_count,save_count:event.save_count,expires_at:event.expires_at}:p));if(['skip','like','save'].includes(action)&&event.id!==null)setUndo({id:event.id,paper,index,expires:Date.now()+2800});else setUndo(null);window.dispatchEvent(new Event('stats-change'))}catch(e){toast((e as Error).message)}finally{lock.current=false;setBusy(null)}}
 const undoLast=async()=>{if(!undo||lock.current)return;lock.current=true;try{const event=await api<InteractionResult>('/interactions','POST',{action:'undo',target_id:undo.id});setItems(list=>{const next=list.filter(p=>p.id!==undo.paper.id);next.splice(undo.index,0,{...undo.paper,liked:event.liked,saved:event.saved,like_count:event.like_count,save_count:event.save_count,expires_at:event.expires_at});return next});setUndo(null);window.dispatchEvent(new Event('stats-change'))}catch(e){toast((e as Error).message)}finally{lock.current=false}}
 const read=(paper:Paper)=>{setReading(paper);if(auth)void api('/interactions','POST',{paper_id:paper.id,action:'expand',feed_context:context}).catch(e=>toast(e.message))}
 const move=(delta:number)=>{const index=items.findIndex(p=>p.id===active),next=items[Math.min(items.length-1,Math.max(0,index+delta))];if(next){setActive(next.id);const node=scroll.current?.querySelector<HTMLElement>(`[data-paper="${next.id}"]`);node?.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion:reduce)').matches?'auto':'smooth',block:'start'})}}
 useHotkeys(key=>{if(reading||suggestions)return;const paper=items.find(p=>p.id===active);if(key==='u')void undoLast();if(!paper)return;if(key==='x')void act(paper,'skip');if(key==='l')void act(paper,paper.liked?'remove_like':'like');if(key==='s')void act(paper,paper.saved?'remove_save':'save');if(key==='enter')read(paper)})
 return <div className="feed-page"><header className="feed-toolbar feed-toolbar-unified" aria-label="信息流操作"><div className="feed-tools"><button className="mobile-suggestions icon-button" aria-label="为您推荐" onClick={()=>setSuggestions(true)}><Sparkles size={18}/></button><button className="icon-button" aria-label="刷新信息流" onClick={load}><RefreshCw size={17}/></button></div></header>
 {(interestStatus==='queued'||interestStatus==='processing')&&<p className="hint interest-feed-status" role="status">新兴趣已保存，推荐正在后台更新。</p>}{interestStatus==='failed'&&<p className="hint interest-feed-status" role="status">兴趣更新失败，已保留原推荐。请到个人设置重新保存。</p>}
 {error?<ErrorBox error={error} reload={load}/>:loading?<Loading/>:<div className="feed-scroll" ref={scroll} role="region" aria-label="论文信息流滚动区域" tabIndex={0}><div className="feed-grid">{(twoColumns?[0,1]:[0]).map(column=><div className="feed-column" key={column}>{items.map((paper,index)=>({paper,index})).filter(item=>!twoColumns||(item.index+Math.floor(item.index/2))%2===column).map(({paper,index})=><section className={'feed-item '+(active===paper.id?'active-paper':'')} data-paper={paper.id} key={paper.id} style={{order:index}} onMouseEnter={()=>focusPaper(paper.id)} onMouseLeave={()=>{if(preferred.current===paper.id){preferred.current=null;choosePaper()}}} onFocus={()=>focusPaper(paper.id)}>
  <PaperCard paper={paper} onRead={read} onFeedback={act} feedbackDisabled={busy!==null} extra={<button disabled={busy!==null} className="feedback-button dismiss" aria-label="没兴趣" onClick={()=>act(paper,'skip')}><X size={16}/><span>没兴趣</span></button>}/>
  <div className="mobile-page-controls"><span>{index+1} / {items.length}</span><button className="icon-button" aria-label="上一篇论文" disabled={!index} onClick={()=>move(-1)}><ArrowUp size={15}/></button><button className="icon-button" aria-label="下一篇论文" disabled={index===items.length-1} onClick={()=>move(1)}><ArrowDown size={15}/></button></div>
 </section>)}</div>)}</div><div className="feed-end" ref={sentinel}>{moreLoading?<Loading/>:hasMore?<button onClick={loadMore}>继续加载</button>:<Empty title="符合您兴趣的未读论文已看完" text="新论文入库后会继续推荐，喜欢或收藏的论文可以在「我的书架」继续阅读。"/>}</div></div>}
 {undo&&<button className="undo-toast" onClick={undoLast}><Undo2 size={15}/>撤销上一操作 <kbd>U</kbd></button>}{reading&&<ReadingModal paper={reading} onClose={()=>setReading(null)}/>} {suggestions&&<Modal title="为您推荐" onClose={()=>setSuggestions(false)}><Recommendations/></Modal>}</div>
}
