import {useCallback,useEffect,useRef,useState} from 'react'
import {api} from '../api'
import {useApp} from '../context'
import {usePaperDwell} from '../hooks/usePaperDwell'
import {useHotkeys} from '../hooks/useHotkeys'
import {useTopicRefresh} from '../hooks/useTopicRefresh'
import {PaperCard} from './PaperCard'
import {Loading,ErrorBox,Empty,formatTime} from './Common'
import type {Paper,InteractionResult} from '../types'
import type {PaperFeedbackAction} from './PaperFeedbackButtons'

type HistoryPage={items:Paper[];next_cursor:string|null}

export function BrowsingHistory({onRead,query=''}:{onRead:(paper:Paper)=>void;query?:string}){
 const {auth,toast}=useApp(),[items,setItems]=useState<Paper[]>([]),[cursor,setCursor]=useState<string|null>(null),[loading,setLoading]=useState(true),[loadingMore,setLoadingMore]=useState(false),[error,setError]=useState(''),[active,setActive]=useState<number|null>(null),[busy,setBusy]=useState(false)
 const root=useRef<HTMLDivElement>(null),sentinel=useRef<HTMLDivElement>(null),version=useRef(0),moreLock=useRef(false),controller=useRef<AbortController|null>(null)
 const load=useCallback(async()=>{
  const id=++version.current;controller.current?.abort();const abort=new AbortController();controller.current=abort;setLoading(true);setError('');setItems([]);setCursor(null);setLoadingMore(false)
  try{const page=await api<HistoryPage>('/library?type=history&limit=20&query='+encodeURIComponent(query),'GET',undefined,abort.signal);if(id===version.current){setItems(page.items);setCursor(page.next_cursor)}}
  catch(e){if(id===version.current&&(e as Error).name!=='AbortError')setError((e as Error).message)}
  finally{if(id===version.current&&!abort.signal.aborted)setLoading(false)}
 },[auth?.user.id,query])
 useTopicRefresh(load)
 useEffect(()=>{void load();return()=>{version.current++;controller.current?.abort()}},[load])
 const more=useCallback(async()=>{
  if(!cursor||moreLock.current)return;const id=version.current;moreLock.current=true;setLoadingMore(true)
  try{const page=await api<HistoryPage>('/library?type=history&limit=20&query='+encodeURIComponent(query)+'&cursor='+encodeURIComponent(cursor));if(id===version.current){setItems(old=>[...old,...page.items.filter(p=>!old.some(previous=>previous.id===p.id))]);setCursor(page.next_cursor);setError('')}}
  catch(e){if(id===version.current)setError((e as Error).message)}
  finally{moreLock.current=false;if(id===version.current)setLoadingMore(false)}
 },[cursor,query])
 useEffect(()=>{
  if(!cursor||error||!sentinel.current)return
  const observer=new IntersectionObserver(entries=>{if(entries.some(e=>e.isIntersecting))void more()},{rootMargin:'150px'})
  observer.observe(sentinel.current);return()=>observer.disconnect()
 },[cursor,error,more])
 useEffect(()=>{
  if(loading||!root.current)return
  const cards=[...root.current.querySelectorAll<HTMLElement>('[data-history-paper]')]
  const visibleCards=new Set<HTMLElement>();let frame=0
  const choose=()=>{
   frame=0
   const height=window.innerHeight,center=height/2
   const visible=[...visibleCards].map(node=>{const box=node.getBoundingClientRect();return {id:Number(node.dataset.historyPaper),ratio:Math.max(0,Math.min(box.bottom,height)-Math.max(box.top,0))/Math.max(1,box.height),distance:Math.abs((box.top+box.bottom)/2-center)}}).filter(p=>p.ratio>=.55).sort((a,b)=>a.distance-b.distance)
   setActive(visible[0]?.id||null)
  }
  const schedule=()=>{if(!frame)frame=requestAnimationFrame(choose)}
  const observer=new IntersectionObserver(entries=>{for(const entry of entries){const node=entry.target as HTMLElement;if(entry.intersectionRatio>=.55)visibleCards.add(node);else visibleCards.delete(node)}schedule()},{threshold:[0,.55,1]});cards.forEach(card=>observer.observe(card))
  window.addEventListener('scroll',schedule,true);window.addEventListener('resize',schedule)
  return()=>{observer.disconnect();cancelAnimationFrame(frame);window.removeEventListener('scroll',schedule,true);window.removeEventListener('resize',schedule)}
 },[loading,items.length])
 usePaperDwell(!loading?active:null,'library')
 const feedback=async(paper:Paper,action:PaperFeedbackAction|'skip')=>{
  if(busy)return;setBusy(true)
  try{const result=await api<InteractionResult>('/interactions','POST',{paper_id:paper.id,action,feed_context:'library'});setItems(old=>old.map(p=>p.id===paper.id?{...p,liked:result.liked,saved:result.saved,like_count:result.like_count,save_count:result.save_count,expires_at:result.expires_at}:p));toast(action==='skip'?'已标记没兴趣':action.startsWith('remove')?'已移除':action==='save'?'已收藏':'已标记喜欢');window.dispatchEvent(new Event('stats-change'))}
  catch(e){toast((e as Error).message)}finally{setBusy(false)}
 }
 useHotkeys(key=>{if(document.querySelector('[role="dialog"]'))return;const paper=items.find(p=>p.id===active);if(!paper)return;if(key==='s')void feedback(paper,paper.saved?'remove_save':'save');if(key==='l')void feedback(paper,paper.liked?'remove_like':'like');if(key==='x')void feedback(paper,'skip');if(key==='enter')onRead(paper)})
 return <div className="paper-list" ref={root}>{loading?<Loading/>:<>{error&&<ErrorBox error={error} reload={cursor?more:load}/>} {!items.length&&!error&&<Empty title={query?"没有找到匹配的浏览记录":"还没有浏览记录"} text={query?"换个关键词，或清空搜索查看全部。":"有效停留满 5 秒，或喜欢、收藏、标记没兴趣后，可以在这里回看。"}/>}{items.map(paper=><div className="paper-list-item" data-history-paper={paper.id} key={paper.id}><p className="hint">最近浏览：{paper.last_browsed_at?formatTime(paper.last_browsed_at):'历史记录'}</p><PaperCard paper={paper} compact onRead={onRead} onFeedback={feedback} feedbackDisabled={busy}/></div>)}<div ref={sentinel}>{loadingMore?<Loading/>:cursor?<button onClick={()=>void more()}>继续加载浏览记录</button>:items.length>0?<p className="hint">已显示全部浏览记录</p>:null}</div></>}</div>
}
