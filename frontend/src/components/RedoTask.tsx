import {useEffect,useState} from 'react'
import {RotateCcw,Play} from 'lucide-react'
import {api} from '../api'
import {useLoad} from '../hooks/useLoad'
import {useApp} from '../context'
import {Modal,ErrorBox} from './Common'
import {SourcePicker} from './SourcePicker'
import type {Category} from '../types'

export type RedoRun={id:number;status:string;total:number;completed:number;failed:number;options:{mode?:'retry';components:string[];category_key:string;from_date:string|null;to_date:string|null}}
type Preview={papers:number;profiles:number;components:string[];operations:number}
export function RedoTask({name,label,disabled,latest,onStarted,allowNew=true}:{name:string;label:string;disabled:boolean;latest?:RedoRun|null;onStarted:()=>void;allowNew?:boolean}){
 const {toast}=useApp(),[open,setOpen]=useState(false),[category,setCategory]=useState(''),[from,setFrom]=useState(''),[to,setTo]=useState(''),[component,setComponent]=useState('both'),[preview,setPreview]=useState<Preview|null>(null),[loading,setLoading]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('')
 const vectors=name==='build_vectors',includePapers=!vectors||component!=='profile_embedding'
 const catalog=useLoad<Category[]>(open&&includePapers?'/categories':null)
 const body={category_key:includePapers?category:'',from_date:includePapers?(from||null):null,to_date:includePapers?(to||null):null,components:vectors?(component==='both'?['embedding','profile_embedding']:[component]):[]},signature=JSON.stringify(body)
 useEffect(()=>{if(!open)return;setPreview(null);setError('');setLoading(true);const abort=new AbortController();const timer=setTimeout(()=>{api<Preview>('/admin/jobs/'+name+'/redo/preview','POST',JSON.parse(signature),abort.signal).then(setPreview).catch(e=>{if(!abort.signal.aborted)setError((e as Error).message)}).finally(()=>{if(!abort.signal.aborted)setLoading(false)})},250);return()=>{clearTimeout(timer);abort.abort()}},[open,signature,name])
 const start=async(resume=false)=>{setBusy(true);setError('');try{await api(resume?`/admin/jobs/${name}/redo/${latest!.id}/resume`:`/admin/jobs/${name}/redo`,'POST',resume?undefined:body);setOpen(false);onStarted();toast(resume?'已继续未完成的重做项':'重做任务已加入队列')}catch(e){setError((e as Error).message);toast((e as Error).message)}finally{setBusy(false)}}
 const resumable=latest&&['stopped','failed'].includes(latest.status)&&latest.completed<latest.total
 const operation=latest?.options.mode==='retry'?'重试':'重做'
 return <>
  <div className="redo-task-actions">{allowNew&&<button disabled={disabled||busy} onClick={()=>{setCategory('');setFrom('');setTo('');setComponent('both');setOpen(true)}}><RotateCcw size={13}/>全部重做</button>}{resumable&&<button disabled={disabled||busy} onClick={()=>void start(true)}><Play size={13}/>继续{operation}</button>}</div>
  {latest&&<p className="redo-latest">上次{operation}：{({queued:'排队中',running:'运行中',stopped:'已停止',failed:'有失败项',completed:'已完成'} as Record<string,string>)[latest.status]} · {latest.completed} / {latest.total} 项{latest.failed?' · 失败 '+latest.failed+' 项':''}</p>}
  {open&&<Modal title={label+' · 全部重做'} onClose={()=>!busy&&setOpen(false)}>
   <p className="muted">包含以前已经完成的结果。新结果生成成功后才替换旧结果。停止后，可以继续处理未完成的部分。</p>
   <fieldset className="redo-options" disabled={busy}>
    {vectors&&<label>重做内容<select value={component} onChange={e=>setComponent(e.target.value)}><option value="both">论文向量、兴趣向量都重做</option><option value="embedding">只重做论文向量</option><option value="profile_embedding">只重做兴趣向量</option></select></label>}
    {includePapers&&<>
     <div className="topic-source-fields"><p>论文范围</p><button type="button" aria-pressed={!category} onClick={()=>setCategory('')}>全部分类与会议</button><SourcePicker multiple={false} sources={catalog.data||[]} selected={category?[category]:[]} onChange={keys=>setCategory(keys[0]||'')}/></div>
     <div className="redo-date-range"><label>发表开始日期<input type="date" value={from} onChange={e=>setFrom(e.target.value)}/></label><label>发表结束日期<input type="date" value={to} onChange={e=>setTo(e.target.value)}/></label></div>
    </>}
   </fieldset>
   {vectors&&component!=='embedding'&&<p className="hint">兴趣向量只处理全站用户当前生效的画像；论文范围筛选只影响论文向量。</p>}
   {includePapers&&catalog.error&&<ErrorBox error={catalog.error} reload={catalog.reload}/>}
   <div className="redo-preview" role="status">{loading?'正在统计范围…':preview?<>{preview.papers>0&&<strong>{preview.papers.toLocaleString()} 篇论文</strong>}{preview.profiles>0&&<strong>{preview.profiles.toLocaleString()} 份当前兴趣画像</strong>}<span>共 {preview.operations.toLocaleString()} 项处理</span></>:'请选择有效范围'}</div>
   <p className="hint">按当前功能分配使用模型；云端调用会计入 token 用量。日常“现在运行”{vectors?'只补缺失的论文向量与当前兴趣向量':'只补未完成的论文'}。</p>
   {vectors&&<p className="hint">更换向量模型时，两种向量必须一起重建，请在模型配置页操作。</p>}
   {error&&<ErrorBox error={error}/>}
   <button className="primary full" disabled={disabled||busy||loading||!preview?.operations||!!error||(includePapers&&!!catalog.error)} onClick={()=>void start()}>{busy?'正在安排…':'确认开始重做'}</button>
  </Modal>}
 </>
}
