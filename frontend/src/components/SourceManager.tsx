import {useEffect,useRef,useState} from 'react'
import {Plus,Search} from 'lucide-react'
import {api} from '../api'
import {useLoad} from '../hooks/useLoad'
import {notifyTopicsChanged} from '../hooks/useTopicRefresh'
import {useApp} from '../context'
import {Modal,Loading,ErrorBox} from './Common'
import {Pagination} from './ui/Pagination'
import {SourcePicker} from './SourcePicker'
import {SourceTable} from './SourceTable'
import {SourceDeletionStatus,type SourceDeletionJob} from './SourceDeletionStatus'
import {sourceMatches,selectAll,groupSources} from '../disciplines'
import {SourceDialog,type VenueOption} from './SourceDialog'
export type Source={key:string;kind:'arxiv'|'venue';code:string;label:string;enabled:boolean;fetch_enabled:boolean;guest_default:boolean;sort_order:number;feed_url:string|null;standard_system:string|null;discipline?:string;label_en?:string;label_zh?:string;paper_count?:number;deletion_id?:number|null}
type BatchImpact={keys:string[];sources:{key:string;code:string;label:string}[];delete_papers:number;keep_papers:number;confirmation:string}
const blank:Source={key:'',kind:'arxiv',code:'',label:'',enabled:true,fetch_enabled:true,guest_default:true,sort_order:100,feed_url:null,standard_system:null,discipline:'Computer Science'}
export function SourceManager(){
 const result=useLoad<Source[]>('/admin/source-categories'),official=useLoad<{code:string;label:string;label_zh:string;group:string}[]>('/admin/source-categories/catalog'),{toast}=useApp(),[editing,setEditing]=useState<Source|null>(null),[busy,setBusy]=useState(false),[test,setTest]=useState(''),[error,setError]=useState(''),[deleting,setDeleting]=useState<{key:string;code:string;label:string;delete_papers:number;keep_papers:number}|null>(null),[confirmation,setConfirmation]=useState(''),[selectedKeys,setSelectedKeys]=useState<string[]>([]),[page,setPage]=useState(1),[pageSize,setPageSize]=useState(10),[listSearch,setListSearch]=useState(''),[kind,setKind]=useState('all'),[batchDeleting,setBatchDeleting]=useState<BatchImpact|null>(null),[batchConfirmation,setBatchConfirmation]=useState(''),[addingCodes,setAddingCodes]=useState<string[]>([])
 const deletions=useLoad<SourceDeletionJob[]>('/admin/source-categories/deletions'),previousJobs=useRef<Map<number,string>>(new Map())
 const deletionPending=!!deletions.data?.some(job=>job.status!=='done')
 useEffect(()=>{
  if(!deletions.data)return
  for(const job of deletions.data){
   const previous=previousJobs.current.get(job.id)
   if(previous&&previous!==job.status&&(job.status==='done'||job.status==='failed')){
    result.reload();notifyTopicsChanged();toast(job.status==='done'?`分类删除完成，清理 ${job.deleted_papers} 篇论文`:'后台删除未完成，已保留进度，请查看任务提示')
   }
  }
  previousJobs.current=new Map(deletions.data.map(job=>[job.id,job.status]))
 },[deletions.data,result.reload,toast])
 useEffect(()=>{
  const active=deletions.data?.some(job=>job.status==='queued'||job.status==='running')
  if(!active)return
  const refresh=()=>{if(document.visibilityState==='visible')deletions.reload()}
  const timer=setInterval(refresh,3000)
  document.addEventListener('visibilitychange',refresh)
  return()=>{clearInterval(timer);document.removeEventListener('visibilitychange',refresh)}
 },[deletions.data,deletions.reload])
 const submitted=(job:SourceDeletionJob)=>{deletions.setData(list=>[job,...(list||[]).filter(item=>item.id!==job.id)]);setSelectedKeys([]);result.reload();notifyTopicsChanged();toast('已提交后台删除，关闭页面后仍会继续')}
 const retryDeletion=(id:number)=>void run(async()=>{const job=await api<SourceDeletionJob>(`/admin/source-categories/deletions/${id}/retry`,'POST');submitted(job)})
 const run=async(work:()=>Promise<void>)=>{setBusy(true);setError('');try{await work()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
 const save=async(s:Source)=>{await api('/admin/source-categories'+(s.key?'/'+encodeURIComponent(s.key):''),s.key?'PATCH':'POST',s);result.reload();notifyTopicsChanged();toast('分类与来源配置已保存')}
 const filtered=(result.data||[]).filter(source=>(kind==='all'||source.kind===kind)&&sourceMatches(source,listSearch))
 const ordered=groupSources(filtered).flatMap(group=>group.sources)
 const pages=Math.max(1,Math.ceil(filtered.length/pageSize)),currentPage=Math.min(page,pages),pageItems=ordered.slice((currentPage-1)*pageSize,currentPage*pageSize)
 useEffect(()=>{setPage(1)},[listSearch,kind,pageSize])
 useEffect(()=>{if(result.data)setSelectedKeys(keys=>keys.filter(key=>result.data!.some(source=>source.key===key&&!source.deletion_id)))},[result.data])
 const selectPage=(checked:boolean)=>setSelectedKeys(keys=>checked?[...new Set([...keys,...pageItems.filter(source=>!source.deletion_id).map(source=>source.key)])]:keys.filter(key=>!pageItems.some(source=>source.key===key)))
 const batchUpdate=(field:'fetch_enabled'|'guest_default',value:boolean)=>void run(async()=>{await api('/admin/source-categories/batch','PATCH',{keys:selectedKeys,[field]:value});result.reload();notifyTopicsChanged();toast(`已更新 ${selectedKeys.length} 个分类`)})
 const previewBatch=()=>void run(async()=>{setBatchDeleting(await api<BatchImpact>('/admin/source-categories/batch/impact','POST',{keys:selectedKeys}));setBatchConfirmation('')})
 const venues=useLoad<VenueOption[]>(editing?.kind==='venue'&&!editing.key?'/admin/source-categories/venues':null)
 const isNewArxiv=editing?.kind==='arxiv'&&!editing.key
 const existingCodes=(result.data||[]).filter(source=>source.kind==='arxiv').map(source=>source.code)
 useEffect(()=>{if(result.data)setAddingCodes(codes=>codes.filter(code=>!result.data!.some(source=>source.kind==='arxiv'&&source.code===code)))},[result.data])
 const editSource=(source:Source)=>{setEditing({...source,enabled:!!source.enabled,fetch_enabled:!!source.fetch_enabled,guest_default:!!source.guest_default});setTest('');setError('')}
 const deletePreview=(source:Source)=>void run(async()=>{setDeleting(await api<{key:string;code:string;label:string;delete_papers:number;keep_papers:number}>('/admin/source-categories/'+encodeURIComponent(source.key)+'/impact'));setConfirmation('')})
 const saveArxivBatch=async(source:Source)=>{
  const added=await api<{created:string[];skipped:string[]}>('/admin/source-categories/arxiv/batch','POST',{codes:addingCodes,fetch_enabled:source.fetch_enabled,guest_default:source.guest_default,sort_order:source.sort_order})
  result.reload();notifyTopicsChanged();toast(`已添加 ${added.created.length} 个分类${added.skipped.length?`，跳过 ${added.skipped.length} 个已有分类`:''}`)
 }

 return <div className="admin-management source-management"><div className="management-page-tools"><div><h1 className="management-page-title">分类与来源</h1><p>管理学科分类与数据来源，配置自动抓取与默认展示。</p></div><button onClick={()=>{setEditing({...blank});setAddingCodes([]);setTest('');setError('')}}><Plus size={15}/>添加分类 / 会议</button></div>{result.error?<ErrorBox error={result.error} reload={result.reload}/>:result.loading&&!result.data?<Loading/> :<><SourceDeletionStatus jobs={deletions.data||[]} onRetry={retryDeletion}/>{deletions.error&&<p className="error-text" role="alert">无法读取删除进度：{deletions.error}</p>}<div className="management-list-card"><div className="source-list-controls"><label><span className="management-filter-label">搜索分类</span><span className="management-search-control"><Search size={17} aria-hidden="true"/><input value={listSearch} onChange={e=>setListSearch(e.target.value)} placeholder="搜索分类代码或名称"/></span></label><label><span className="management-filter-label">类型</span><select value={kind} onChange={e=>setKind(e.target.value)}><option value="all">全部类型</option><option value="arxiv">arXiv</option><option value="venue">会议</option></select></label><label><span className="management-filter-label">每页</span><select value={pageSize} onChange={e=>setPageSize(Number(e.target.value))}>{[10,20,50].map(n=><option key={n} value={n}>{n} 条</option>)}</select></label></div><div className="batch-toolbar source-batch-toolbar"><button disabled={busy||!pageItems.some(source=>!source.deletion_id)} onClick={()=>selectPage(true)}>全选当前页</button><span>已选 {selectedKeys.length} 项</span><button hidden={!selectedKeys.length} disabled={busy||!selectedKeys.length} onClick={()=>batchUpdate('fetch_enabled',true)}>开启抓取</button><button hidden={!selectedKeys.length} disabled={busy||!selectedKeys.length} onClick={()=>batchUpdate('fetch_enabled',false)}>关闭抓取</button><button hidden={!selectedKeys.length} disabled={busy||!selectedKeys.length} onClick={()=>batchUpdate('guest_default',true)}>加入游客默认</button><button hidden={!selectedKeys.length} disabled={busy||!selectedKeys.length} onClick={()=>batchUpdate('guest_default',false)}>移出游客默认</button><button className="danger" hidden={!selectedKeys.length} disabled={busy||deletionPending||!selectedKeys.length} onClick={previewBatch}>批量删除</button><button disabled={busy||!filtered.length} onClick={()=>setSelectedKeys(keys=>selectAll(keys,filtered.filter(source=>!source.deletion_id).map(source=>source.key)))}>全选全部（{filtered.length}项）</button>{!!selectedKeys.length&&<button disabled={busy} onClick={()=>setSelectedKeys([])}>取消选择</button>}</div><SourceTable sources={filtered} pageItems={pageItems} selected={selectedKeys} busy={busy} deletionPending={deletionPending} search={listSearch} onSelectPage={selectPage} onSelect={(key,checked)=>setSelectedKeys(keys=>checked?selectAll(keys,[key]):keys.filter(k=>k!==key))} onEdit={editSource} onDelete={deletePreview} onConfig={(source,key,checked)=>void run(()=>save({...source,[key]:checked}))}/><Pagination label="分类与来源" pageSize={pageSize} page={currentPage} pages={pages} total={filtered.length} onChange={setPage}/><details className="management-help"><summary>抓取与删除说明</summary><p>主类直接包含主题。自动抓取控制流水线同步，游客默认控制游客信息流。删除分类会清理只属于该分类的论文，跨分类或跨会议论文会保留。</p></details></div></>}{error&&!editing&&<p role="alert" className="error-text">{error}</p>}
 {editing&&<SourceDialog source={editing} busy={busy} error={error} test={test} addingCount={addingCodes.length}
  canSave={!busy&&!result.error&&(isNewArxiv?!result.loading&&!official.error&&!!addingCodes.length:!!editing.code&&(!editing.key&&editing.kind==='venue'?!venues.loading&&!venues.error:true))}
  onChange={source=>{setEditing(source);setTest('')}}
  onKindChange={kind=>{setEditing({...blank,kind,standard_system:null});setAddingCodes([]);setTest('');setError('')}}
  onClose={()=>!busy&&setEditing(null)}
  onSubmit={()=>void run(async()=>{if(isNewArxiv)await saveArxivBatch(editing);else await save(editing);setEditing(null)})}
  onTest={()=>void run(async()=>{const r=await api<{year?:number;count?:number}>('/admin/source-categories/test','POST',editing);setTest(editing.kind==='arxiv'?'官方分类代码有效':`来源校验通过：${r.year} 年，${r.count} 篇论文`)})}
  venues={venues.data||[]} venuesLoading={venues.loading} venuesError={venues.error} reloadVenues={venues.reload}
  existingVenues={(result.data||[]).filter(source=>source.kind==='venue').map(source=>source.code)}
  arxivPicker={official.error?<ErrorBox error={official.error} reload={official.reload}/>:official.loading&&!official.data?<Loading/>:<SourcePicker selectGroups label="搜索官方分类" sources={(official.data||[]).map(item=>({key:item.code,kind:'arxiv',code:item.code,label:item.label_zh,label_en:item.label,discipline:item.group}))} selected={addingCodes} disabledKeys={existingCodes} disabled={busy||result.loading||!!result.error} onChange={setAddingCodes}/>}
 />}{deleting&&<Modal title={'删除分类 '+deleting.code} onClose={()=>!busy&&setDeleting(null)}><p>将提交后台删除任务，清理该分类和独属于它的论文。提交后可关闭页面，此操作无法恢复。</p><dl className="deletion-counts"><div><dt>将删除的论文</dt><dd>{deleting.delete_papers}</dd></div><div><dt>仍属于其他分类 / 会议，保留</dt><dd>{deleting.keep_papers}</dd></div></dl><p className="hint">删除论文也会清理其向量、解读、PDF 缓存、收藏与阅读关联。共享论文继续按其余已配置分类或会议展示。主题保留原有状态，没有所属主类的主题会进入“未归类主题”，后续论文可继续复用。</p><label>输入分类代码确认<input value={confirmation} onChange={e=>setConfirmation(e.target.value)} placeholder={deleting.code} autoComplete="off"/></label>{error&&<p role="alert" className="error-text">{error}</p>}<button className="danger" disabled={busy||confirmation!==deleting.code} onClick={()=>void run(async()=>{const job=await api<SourceDeletionJob>('/admin/source-categories/'+encodeURIComponent(deleting.key),'DELETE',{confirm_code:confirmation,delete_papers:deleting.delete_papers,keep_papers:deleting.keep_papers});setDeleting(null);submitted(job)})}>{busy?'正在提交…':'提交后台删除'}</button></Modal>}{batchDeleting&&<Modal title={'批量删除 '+batchDeleting.keys.length+' 个分类'} onClose={()=>!busy&&setBatchDeleting(null)}><p>将删除以下分类，并清理只属于这些分类的论文。仍属于未删除分类或会议的论文保留。此操作无法恢复。</p><p className="batch-source-codes">{batchDeleting.sources.map(source=>source.code).join('、')}</p><dl className="deletion-counts"><div><dt>将删除的论文（已去重）</dt><dd>{batchDeleting.delete_papers}</dd></div><div><dt>保留的共享论文</dt><dd>{batchDeleting.keep_papers}</dd></div></dl><p className="hint">论文的向量、解读、PDF 缓存及个人关联一起清理。主题保留原有状态，没有所属主类的主题会进入“未归类主题”，后续论文可继续复用。</p><label>输入“{batchDeleting.confirmation}”确认<input autoComplete="off" value={batchConfirmation} onChange={e=>setBatchConfirmation(e.target.value)}/></label>{error&&<p className="error-text" role="alert">{error}</p>}<button className="danger" disabled={busy||batchConfirmation!==batchDeleting.confirmation} onClick={()=>void run(async()=>{const job=await api<SourceDeletionJob>('/admin/source-categories/batch','DELETE',{keys:batchDeleting.keys,confirm_text:batchConfirmation,delete_papers:batchDeleting.delete_papers,keep_papers:batchDeleting.keep_papers});setBatchDeleting(null);submitted(job)})}>{busy?'正在提交…':'提交后台删除'}</button></Modal>}</div>
}
