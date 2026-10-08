import {useEffect,useRef,useState} from 'react'
import {useNavigate} from 'react-router-dom'
import {Check,Mail,Trash2} from 'lucide-react'
import {api} from '../api'
import {useApp} from '../context'
import {PageTitle,Loading,Empty,ErrorBox,Modal,formatTime} from '../components/Common'
import {MathText} from '../components/MathText'
import {ContentCard} from '../components/PageContentUI'
import {NotificationTable,noticeTypes,type Notice} from '../components/NotificationTable'
import {Pagination} from '../components/ui/Pagination'
import {ReadingModal} from '../components/ReadingModal'
import type {Paper} from '../types'
import './notifications.css'

type NoticePage={items:Notice[];unread:number;total:number;offset:number;limit:number}
type Status='all'|'read'|'unread'
const PAGE_SIZE=20

export function Notifications(){
 const {auth,toast}=useApp(),navigate=useNavigate()
 const [page,setPage]=useState(1),[status,setStatus]=useState<Status>('all'),[kind,setKind]=useState(''),[revision,setRevision]=useState(0)
 const [response,setResponse]=useState<{key:string;data:NoticePage}|null>(null),[loading,setLoading]=useState(true),[error,setError]=useState('')
 const [selected,setSelected]=useState<number[]>([]),[busy,setBusy]=useState(false),working=useRef(false),selectAll=useRef<HTMLInputElement>(null)
 const [deleting,setDeleting]=useState<number[]|null>(null),[reading,setReading]=useState<{paper:Paper;level:'L2'|'L3'}|null>(null),[detail,setDetail]=useState<Notice|null>(null)
 const query=new URLSearchParams({offset:String((page-1)*PAGE_SIZE),limit:String(PAGE_SIZE),status,type:kind}).toString(),path='/notifications?'+query,key=auth?.user.id+':'+path
 const data=response?.key===key?response.data:null,items=data?.items||[],pages=Math.max(1,Math.ceil((data?.total||0)/PAGE_SIZE))
 const ids=items.filter(n=>selected.includes(n.id)).map(n=>n.id),allSelected=!!items.length&&ids.length===items.length
 const unavailable=busy||loading||!data
 useEffect(()=>{
  const controller=new AbortController();setLoading(true);setError('')
  api<NoticePage>(path,'GET',undefined,controller.signal).then(value=>{
   if(controller.signal.aborted)return
   const last=Math.max(1,Math.ceil(value.total/PAGE_SIZE))
   if(page>last){setSelected([]);setPage(last);return}
   setResponse({key,data:value})
  }).catch(e=>{if(!controller.signal.aborted)setError((e as Error).message)}).finally(()=>{if(!controller.signal.aborted)setLoading(false)})
  return()=>controller.abort()
 },[path,key,revision])
 useEffect(()=>{if(selectAll.current)selectAll.current.indeterminate=ids.length>0&&!allSelected},[ids.length,allSelected,loading,error])
 const reload=()=>setRevision(r=>r+1)
 const changePage=(next:number)=>{setSelected([]);setPage(next)}
 const changeStatus=(next:Status)=>{setSelected([]);setStatus(next);setPage(1)}
 const changeKind=(next:string)=>{setSelected([]);setKind(next);setPage(1)}
 const refresh=()=>{setSelected([]);reload();window.dispatchEvent(new Event('stats-change'))}
 const run=async(work:()=>Promise<void>)=>{
  if(working.current)return
  working.current=true;setBusy(true)
  try{await work()}catch(e){toast((e as Error).message)}finally{working.current=false;setBusy(false)}
 }
 const updateRead=(targets:number[],read:boolean)=>void run(async()=>{
  await api('/notifications/batch','PATCH',{ids:targets,read});refresh();toast(read?'已标为已读':'已标为未读')
 })
 const remove=()=>{if(!deleting)return;const targets=deleting;void run(async()=>{
  const result=await api<{deleted:number}>('/notifications/batch','DELETE',{ids:targets})
  setDeleting(null);refresh();toast(`已删除 ${result.deleted} 条通知`)
 })}
 const open=(n:Notice)=>void run(async()=>{
  await api(`/notifications/${n.id}/read`,'POST');refresh()
  if(n.paper_id)setReading({paper:await api<Paper>('/papers/'+n.paper_id),level:n.type==='collision'?'L3':'L2'})
  else if(n.type==='trend_report')navigate('/trends')
  else setDetail(n)
 })
 return <div className="notifications-center">
  <PageTitle eyebrow="NOTIFICATIONS CENTER" title="通知中心" description={`监视命中与研究预警 · ${data?.unread??(response?.key.startsWith(auth?.user.id+':')?response.data.unread:0)??0} 条未读`}/>
  <ContentCard className="notification-table-card">
   <div className="notice-filters"><div className="notice-status-filter" role="group" aria-label="通知阅读状态">{([['all','全部'],['unread','未读'],['read','已读']] as const).map(([value,label])=><button key={value} type="button" aria-pressed={status===value} className={status===value?'active':''} disabled={busy} onClick={()=>changeStatus(value)}>{label}</button>)}</div><label className="notice-type-filter"><span className="sr-only">通知类型</span><select aria-label="通知类型" value={kind} disabled={busy} onChange={e=>changeKind(e.target.value)}><option value="">全部类型</option>{Object.entries(noticeTypes).map(([value,label])=><option value={value} key={value}>{label}</option>)}</select></label></div>
   {ids.length>0&&<div className="notice-bulk-bar" aria-label="通知批量管理"><span>已选 <strong>{ids.length}</strong> 条</span><div><button type="button" disabled={unavailable} onClick={()=>updateRead(ids,true)}><Check size={14}/>标为已读</button><button type="button" disabled={unavailable} onClick={()=>updateRead(ids,false)}><Mail size={14}/>标为未读</button><button type="button" className="notice-danger" disabled={unavailable} onClick={()=>setDeleting(ids)}><Trash2 size={14}/>删除</button></div></div>}
   <div className="notice-results" aria-busy={loading}>
    {error?<ErrorBox error={error} reload={reload}/>:loading||!data?<Loading/>:items.length?<NotificationTable items={items} selected={ids} busy={unavailable} selectAll={selectAll} allSelected={allSelected} onSelectPage={checked=>setSelected(checked?items.map(n=>n.id):[])} onSelect={(id,checked)=>setSelected(previous=>checked?[...previous,id]:previous.filter(value=>value!==id))} onOpen={open} onRead={n=>updateRead([n.id],!n.read)} onDelete={n=>setDeleting([n.id])}/>:<Empty title={status==='all'&&!kind?'暂时没有新通知':'没有符合筛选条件的通知'} text={status==='all'&&!kind?'在设置中添加监视清单，关注您的关键词、作者和评测基准。':'可以切换阅读状态或通知类型查看。'}/>}
   </div>
   {data&&!error&&!loading&&<Pagination label="通知" pageSize={PAGE_SIZE} page={page} pages={pages} total={data.total} onChange={changePage}/>}
  </ContentCard>
  {deleting&&<Modal title="删除通知" className="notice-delete-dialog" onClose={()=>{if(!busy)setDeleting(null)}}><p>确定删除所选的 <strong>{deleting.length}</strong> 条通知？</p><div className="notice-dialog-actions"><button type="button" disabled={busy} onClick={()=>setDeleting(null)}>取消</button><button type="button" className="notice-danger" disabled={busy} onClick={remove}><Trash2 size={15}/>{busy?'删除中…':'确认删除'}</button></div></Modal>}
  {detail&&<Modal title="通知详情" className="notice-detail-dialog" onClose={()=>setDetail(null)}><p className="notice-detail-meta">{noticeTypes[detail.type]||detail.type} · {formatTime(detail.created_at)}</p><h3><MathText>{detail.title}</MathText></h3><p className="notice-detail-body"><MathText>{detail.body}</MathText></p></Modal>}
  {reading&&<ReadingModal paper={reading.paper} level={reading.level} onClose={()=>setReading(null)}/>}
 </div>
}
