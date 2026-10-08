import {useEffect,useRef,useState} from 'react'
import {Plus,Pencil,Trash2,GitMerge,Check,X,PauseCircle,Clock3,Folder,Archive,Sparkles,BookOpen} from 'lucide-react'
import {api} from '../api'
import {useLoad} from '../hooks/useLoad'
import {useApp} from '../context'
import {CategoryTree} from './CategoryTree'
import {SourcePicker} from './SourcePicker'
import {disciplines,disciplineLabel,sourceDiscipline,sourceMatches,topicMatches,selectAll} from '../disciplines'
import {TreeDrawer} from './TreeDrawer'
import {Pagination} from './ui/Pagination'
import {useViewportPanel} from '../hooks/useViewportPanel'
import {notifyTopicsChanged} from '../hooks/useTopicRefresh'
import {MathText} from './MathText'
import {ErrorBox,Loading,Modal} from './Common'
import type {Category,Topic} from '../types'

function ProposalOrigin({topic}:{topic:Topic}){
 if(topic.status!=='proposed')return null
 const isNew=topic.created_by==='agent'&&!topic.standard_key
 const existing=!!topic.standard_key
 const label=isNew?'Agent 新拟':existing?(topic.created_by==='user'?'用户提议 · 表中已有方向':'表中已有方向'):'来源未标记'
 const explanation=isNew?'Agent 新取的主题名称，批准后加入自建研究方向表。':existing?'从自建研究方向表选取，没有新拟主题名称。':'该记录没有足够的来源信息，无法确认是否由 Agent 新拟。'
 return <span className={'proposal-origin '+(isNew?'new':existing?'existing':'unknown')} title={explanation}>{isNew?<Sparkles size={12}/>:<BookOpen size={12}/>}<span>{label}</span></span>
}

export function TopicManager({topics,reload,loading,error}:{topics:Topic[]|null;reload:()=>void;loading:boolean;error:string}){
 const {toast}=useApp(),catalog=useLoad<Category[]>('/admin/categories')
 const [category,setCategory]=useState<string|null>(null),[topicId,setTopicId]=useState<number|null>(null),[filter,setFilter]=useState('all'),[editing,setEditing]=useState<Partial<Topic>|null>(null),[merging,setMerging]=useState<Topic|null>(null),[target,setTarget]=useState(''),[busy,setBusy]=useState(false)
 useEffect(()=>{catalog.reload()},[topics,catalog.reload])
 const [checkedIds,setCheckedIds]=useState<number[]>([]),[batchErrors,setBatchErrors]=useState<{id:number;error:string}[]>([])
 const viewportPanel=useViewportPanel(),content=useRef<HTMLElement>(null),[page,setPage]=useState(1),[search,setSearch]=useState('')
 const archive=topics?.filter(t=>['legacy','disabled','merged'].includes(t.status||''))||[]
 const all=topics?.filter(t=>t.status==='active'||t.status==='proposed')||[],selected=topics?.find(t=>t.id===topicId),source=catalog.data?.find(c=>c.key===category)
 const assigned=new Set(catalog.data?.flatMap(c=>c.topics.map(t=>t.id)))
 const unassigned=all.filter(t=>!assigned.has(t.id)),proposed=all.filter(t=>t.status==='proposed')
 const scopedList=filter==='archive'?archive:filter==='proposed'?proposed:filter==='unassigned'?unassigned:source?source.topics.map(t=>all.find(original=>original.id===t.id)!).filter(Boolean):all
 const list=scopedList.filter(t=>!search.trim()||topicMatches(t,search)||(catalog.data||[]).some(c=>t.category_keys?.includes(c.key)&&sourceMatches(c,search)))
 const emptyMessage=search.trim()?'没有匹配的主题。':filter==='archive'?'暂无已停用主题。':filter==='unassigned'?'暂无未归类主题。':filter==='proposed'?'暂无待审核的主题提议。':source?'该分类下暂无主题。':'暂无主题。'
 const pages=Math.max(1,Math.ceil(list.length/20)),currentPage=Math.min(page,pages),pageItems=list.slice((currentPage-1)*20,currentPage*20)
 useEffect(()=>{setPage(1)},[category,filter,search])
 useEffect(()=>{setPage(p=>Math.min(p,pages))},[pages])
 const changePage=(next:number)=>{setPage(next);content.current?.scrollTo({top:0})}
 const submissions=useLoad<{username:string;note:string;created_at:string}[]>(selected?.status==='proposed'?`/admin/topics/${selected.id}/proposals`:null)
 const pending=useLoad<{id:number;title:string;confidence:number;reason?:string;evidence?:string}[]>(selected?.status==='proposed'?`/admin/topics/${selected.id}/pending`:null)
 useEffect(()=>{if(topics)setCheckedIds(list=>list.filter(id=>topics.some(t=>t.id===id)))},[topics])
 const refresh=()=>{reload();catalog.reload();notifyTopicsChanged()}
 const run=async(work:()=>Promise<unknown>,message:string)=>{setBusy(true);try{await work();refresh();if(message)toast(message)}catch(e){toast((e as Error).message)}finally{setBusy(false)}}
 const select=(key:string|null,id:number|null)=>{setCategory(key);setTopicId(id);setFilter('all')}
 const create=()=>setEditing({name_zh:'',name_en:'',parent_id:null,status:'active',category_keys:category?[category]:[],discipline:source?sourceDiscipline(source):'Computer Science',description:''})
 const remove=(t:Topic,permanent:boolean)=>{
  const description=permanent?`彻底删除「${t.name_zh}」？其禁用记录也会清除，之后 Agent 可以重新提议这个主题。论文、喜欢和收藏会保留。`:`停用「${t.name_zh}」？相关论文的主题标签会移除并重新分类，论文、喜欢和收藏会保留。`
  if(!window.confirm(description))return
  void run(async()=>{await api('/admin/topics/'+t.id+(permanent?'?permanent=true':''),'DELETE');if(topicId===t.id)setTopicId(null)},permanent?'主题已彻底删除，可以重新提议':'主题已停用，论文已保留')
 }
 const selectedTopics=(topics||[]).filter(t=>checkedIds.includes(t.id))
 const eligible=(action:string)=>selectedTopics.filter(t=>action==='approve'||action==='reject'?t.status==='proposed':action==='disable'?t.status==='active':action==='restore'?t.status==='disabled':['disabled','legacy','merged'].includes(t.status||''))
 const batchLabels:Record<string,string>={approve:'批准',reject:'拒绝',disable:'停用',restore:'恢复',delete:'彻底删除'}
 const batch=async(action:string)=>{const ids=eligible(action).map(t=>t.id);if(!ids.length)return;const detail=action==='delete'?'清除主题及禁用记录，之后 Agent 可重新提议。论文、喜欢和收藏保留。':action==='disable'||action==='reject'?'移除这些主题的论文标签及兴趣引用，论文保留。':'启用主题并自动关联等待批准的论文。';if(!window.confirm(`${batchLabels[action]} ${ids.length} 个主题？${detail}`))return;await run(async()=>{const result=await api<{completed:number[];failed:{id:number;error:string}[]}>('/admin/topics/batch','POST',{ids,action});setCheckedIds(list=>list.filter(id=>!result.completed.includes(id)));setBatchErrors(result.failed);toast(`已${batchLabels[action]} ${result.completed.length} 项${result.failed.length?`，${result.failed.length} 项失败`:''}`)},'')}
 const batchToolbar=!selected&&<><div className="batch-toolbar"><label><input type="checkbox" aria-label="全选当前页主题" disabled={busy} checked={pageItems.length>0&&pageItems.every(t=>checkedIds.includes(t.id))} onChange={e=>setCheckedIds(list=>e.target.checked?[...new Set([...list,...pageItems.map(t=>t.id)])]:list.filter(id=>!pageItems.some(t=>t.id===id)))}/>全选当前页</label><button disabled={busy||!list.length} onClick={()=>setCheckedIds(ids=>selectAll(ids,list.map(t=>t.id)))}>全选全部（{list.length}项）</button><span>已选 {checkedIds.length} 项</span>{Object.entries(batchLabels).map(([action,label])=><button key={action} hidden={!eligible(action).length} className={action==='delete'?'danger':''} disabled={busy||!eligible(action).length} onClick={()=>void batch(action)}>{label} {eligible(action).length||''}</button>)}{!!checkedIds.length&&<button disabled={busy} onClick={()=>setCheckedIds([])}>取消选择</button>}</div>{!!batchErrors.length&&<div role="alert" className="batch-errors">{batchErrors.map(e=><p key={e.id}>{topics?.find(t=>t.id===e.id)?.name_zh||`主题 ${e.id}`}：{e.error}</p>)}</div>}</>
 const actions=(t:Topic)=>{
  const archived=['legacy','disabled','merged'].includes(t.status||'')
  return <div className="table-actions topic-actions">
   {t.status==='proposed'&&<><button title="批准" disabled={busy} aria-label={'接受主题'+t.name_zh} onClick={()=>run(()=>api('/admin/topics/'+t.id,'PATCH',{...t,status:'active'}),'主题已接受')}><Check size={15}/>批准</button><button title="拒绝并停用" disabled={busy} aria-label={'拒绝主题'+t.name_zh} onClick={()=>run(()=>api('/admin/topics/'+t.id,'DELETE'),'主题提议已拒绝')}><X size={15}/>拒绝</button></>}
   {t.status!=='merged'&&<><button title="编辑" disabled={busy} aria-label={'编辑主题'+t.name_zh} onClick={()=>setEditing(t)}><Pencil size={15}/>编辑</button><button title="合并" disabled={busy} aria-label={'合并主题'+t.name_zh} onClick={()=>{setMerging(t);setTarget('')}}><GitMerge size={15}/>合并</button></>}
   {archived?<button title="彻底删除，允许重新提议" disabled={busy} aria-label={'彻底删除主题'+t.name_zh} onClick={()=>remove(t,true)}><Trash2 size={15}/>删除</button>:t.status==='active'&&<button title="停用" disabled={busy} aria-label={'停用主题'+t.name_zh} onClick={()=>remove(t,false)}><PauseCircle size={15}/>停用</button>}
   {t.status==='disabled'&&<button disabled={busy} onClick={()=>{if(!t.discipline||!t.description){setEditing({...t,status:'active'});return}void run(()=>api('/admin/topics/'+t.id,'PATCH',{...t,status:'active'}),'主题已恢复')}}>恢复</button>}
  </div>
 }
 const heading=selected?selected.name_zh:filter==='archive'?'已停用主题':filter==='proposed'?'待审核提议':filter==='unassigned'?'未归类主题':source?source.code+' 下的主题':'全部主题'
 const tree=<>{catalog.error?<ErrorBox error={catalog.error} reload={catalog.reload}/>:<CategoryTree catalog={catalog.data||[]} category={category} topic={topicId} onSelect={select} allLabel="全部主题" allCount={all.length} controlsVariant="buttons" search={search} onSearchChange={setSearch}/>}<div className="topic-tree-extra"><button className={filter==='proposed'?'active':''} onClick={()=>{select(null,null);setFilter('proposed')}}><Clock3 size={16}/>待审核提议 <small>{proposed.length}</small></button><button className={filter==='unassigned'?'active':''} onClick={()=>{select(null,null);setFilter('unassigned')}}><Folder size={16}/>未归类主题 <small>{unassigned.length}</small></button><button className={filter==='archive'?'active':''} onClick={()=>{select(null,null);setFilter('archive')}}><Archive size={16}/>已停用主题 <small>{archive.length}</small></button></div></>
 return <div className="admin-management topics-management"><div className="management-page-tools"><div><h1 className="management-page-title">主题管理</h1><p>管理自建研究方向，按分类浏览、审核与发布主题。</p></div></div>{error?<ErrorBox error={error} reload={reload}/>:loading&&!topics?<Loading/>:<><TreeDrawer title="主题分类" label={heading}>{tree}</TreeDrawer><div ref={viewportPanel} className="topic-manager-layout viewport-panel"><aside className="panel topic-manager-tree desktop-topic-tree">{tree}</aside>
  <section ref={content} className="topic-manager-content management-list-card"><div className="section-heading"><h3>{heading}{!selected&&<small>{list.length}</small>}</h3><button className="primary" disabled={busy} onClick={()=>create()}><Plus size={15}/>添加主题</button></div>
   {batchToolbar}{selected?<div className="panel topic-details"><p className="muted"><MathText inline>{selected.name_en}</MathText></p><p className="standard-code">{disciplineLabel(selected.discipline)}{selected.standard_key?' · '+selected.standard_key:' · 新方向提议'}</p><ProposalOrigin topic={selected}/><p className="hint"><MathText>{selected.description||'请编辑并补充主题描述。'}</MathText></p>{selected.proposal_reason&&<p className="hint">提议依据：<MathText inline>{selected.proposal_reason}</MathText></p>}<p>已归类 {selected.paper_count??0} 篇 · 等待主题批准 {selected.pending_count??0} 篇</p><p>7 日喜欢率：{Math.round((selected.like_rate||0)*100)}%</p><p className="topic-membership">展示于：{catalog.data?.filter(c=>c.topics.some(t=>t.id===selected.id)).map(c=>c.code).join('、')||'未归类'}</p><p className="hint">同一主题在各分类和会议中共用名称与描述。</p>{actions(selected)}{selected.status==='proposed'&&<div className="proposal-papers">{!!submissions.data?.length&&<><h4>用户提议</h4>{submissions.data.map((p,i)=><p key={i}><strong>{p.username}</strong>：{p.note||'未填写说明'}</p>)}</>}<h4>触发提议的论文</h4>{pending.data?.map(p=><p key={p.id}><MathText inline>{p.title}</MathText><small> 模型置信度 {Math.round(p.confidence*100)}%</small>{p.reason&&<span className="hint"><MathText>{p.reason}</MathText></span>}{p.evidence&&<span className="hint"><MathText>{p.evidence}</MathText></span>}</p>)}</div>}</div>:<><div className="panel topic-table-panel"><table className="topic-table"><thead><tr><th className="selection-column">选择</th><th>主题</th><th>论文数</th><th>状态</th><th>操作</th></tr></thead><tbody>{pageItems.map(t=><tr key={t.id} className={checkedIds.includes(t.id)?"row-selected":""}><td className="selection-column"><input type="checkbox" aria-label={'选择主题：'+t.name_zh} checked={checkedIds.includes(t.id)} onChange={e=>setCheckedIds(list=>e.target.checked?[...list,t.id]:list.filter(id=>id!==t.id))}/></td><td><button className="topic-detail-link" onClick={()=>setTopicId(t.id)}><MathText inline>{t.name_zh}</MathText></button><small>{t.discipline?disciplineLabel(t.discipline):'未设置学科'}</small><small className="topic-english"><MathText inline>{t.name_en}</MathText></small><ProposalOrigin topic={t}/></td><td data-label="论文数">{t.paper_count??0}{t.status==='proposed'?<small>待批准 {t.pending_count??0} 篇</small>:<small>7 日喜欢率 {Math.round((t.like_rate||0)*100)}%</small>}</td><td data-label="状态"><span className={"management-status topic-status "+(t.status||"active")}>{{active:"正常",proposed:"待审核",disabled:"已停用",merged:"已合并",legacy:"旧主题"}[t.status||"active"]||t.status}</span></td><td>{actions(t)}</td></tr>)}</tbody></table>{!list.length&&<p className="tree-empty">{emptyMessage}</p>}</div><Pagination page={currentPage} pages={pages} total={list.length} onChange={changePage}/></>}
  </section>
 </div></>}

 {editing&&<Modal title={editing.id?'编辑主题':'新增主题'} onClose={()=>setEditing(null)}><form className="research-area-form" onSubmit={e=>{e.preventDefault();void run(async()=>{await api('/admin/topics'+(editing.id?'/'+editing.id:''),editing.id?'PATCH':'POST',editing);setEditing(null)},'主题已保存')}}>
  {editing.standard_key&&<p className="hint">方向 ID：{editing.standard_key}（保持不变）</p>}
  <label>中文名称<input value={editing.name_zh||''} maxLength={100} onChange={e=>setEditing(p=>({...p,name_zh:e.target.value}))} required/></label>
  <label>英文名称<input value={editing.name_en||''} maxLength={200} onChange={e=>setEditing(p=>({...p,name_en:e.target.value}))} required/></label>
  <label>学科<select value={editing.discipline||''} onChange={e=>setEditing(p=>({...p,discipline:e.target.value}))} required><option value="">请选择学科</option>{disciplines.map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label>
  <label>方向描述（英文）<textarea value={editing.description||''} maxLength={1200} rows={3} onChange={e=>setEditing(p=>({...p,description:e.target.value}))} placeholder="简述这个方向研究的问题与范围" required/></label>
  <fieldset className="topic-source-fields"><legend>所属主类</legend><p className="hint">按学科选择，可同时勾选多个 arXiv 分类和会议。</p><SourcePicker sources={catalog.data||[]} selected={editing.category_keys||[]} onChange={keys=>setEditing(p=>({...p,category_keys:keys}))}/></fieldset>
  <button className="primary" disabled={busy||!editing.discipline||!editing.category_keys?.length}>保存</button></form></Modal>}
 {merging&&<Modal title={'合并「'+merging.name_zh+'」'} onClose={()=>setMerging(null)}><p className="muted">关联论文、所属主类和用户的主题选择将迁入目标主题。</p><select className="full" value={target} onChange={e=>setTarget(e.target.value)}><option value="">选择目标主题</option>{all.filter(t=>t.status==='active'&&t.id!==merging.id).map(t=><option key={t.id} value={t.id}>{t.name_zh}</option>)}</select><button className="primary" disabled={!target||busy} onClick={()=>run(async()=>{await api('/admin/topics/'+merging.id+'/merge','POST',{target_id:Number(target)});setMerging(null);if(topicId===merging.id)setTopicId(Number(target))},'主题已合并')}>确认合并</button></Modal>}
 </div>
}
