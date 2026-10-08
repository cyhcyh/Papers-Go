import {useState} from 'react'
import {Plus,Lightbulb,Check,ArrowRight,ArrowLeft,Send,Layers3} from 'lucide-react'
import {ContentHeading} from './PageContentUI'
import {api} from '../api'
import {useLoad} from '../hooks/useLoad'
import {useApp} from '../context'
import {StandardPicker,type Standard} from './StandardPicker'
import {SourcePicker} from './SourcePicker'
import {Modal,ErrorBox} from './Common'
import {MathText} from './MathText'
import {disciplineLabel} from '../disciplines'
import '../topic-proposal-dialog.css'
import type {Category} from '../types'
export function TopicProposals(){
 const {toast}=useApp(),catalog=useLoad<Category[]>('/categories'),mine=useLoad<{id:number;name_zh:string|null;standard_key:string;status:string|null;note:string}[]>('/topic-proposals'),[open,setOpen]=useState(false),[entry,setEntry]=useState<Standard|null>(null),[keys,setKeys]=useState<string[]>([]),[note,setNote]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState(''),[step,setStep]=useState<'choose'|'details'>('choose')
 const submit=async()=>{if(!entry)return;setBusy(true);setError('');try{await api('/topic-proposals','POST',{standard_key:entry.key,category_keys:keys,note});mine.reload();setOpen(false);toast('提议已提交，等待管理员批准')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
 return <section className="panel settings-section research-card content-card topic-proposal-card"><ContentHeading icon={Lightbulb} tone="amber" title="主题提议" action={<button onClick={()=>{setEntry(null);setKeys([]);setNote('');setError('');setStep('choose');setOpen(true)}}><Plus size={15}/>提议新主题</button>}/><p className="muted">从研究方向目录选择。普通用户提交提议，管理员批准后开放使用；管理员可在主题管理中直接添加。</p>{mine.error&&<ErrorBox error={mine.error} reload={mine.reload}/>}<div className="my-proposals">{mine.data?.map(p=><div key={p.id} className="proposal-item" data-state={p.status||'deleted'}><div className="proposal-item-heading"><strong>{p.name_zh||p.standard_key}</strong><span className="tag">{({active:'已批准',proposed:'待审核',disabled:'已拒绝 / 停用',merged:'已合并'} as Record<string,string>)[p.status||'']||'主题已删除'}</span></div>{p.note&&<p>{p.note}</p>}</div>)}</div>{open&&<Modal title="提交主题提议" className="topic-proposal-modal" onClose={()=>!busy&&setOpen(false)}>
  <p className="proposal-dialog-intro">选择本站研究方向，提交后由管理员审核。</p>
  <ol className="proposal-dialog-steps" aria-label="提议步骤"><li className={step==='choose'?'active':'done'} aria-current={step==='choose'?'step':undefined}><span>{step==='details'?<Check size={14}/>:1}</span>选择研究方向</li><li className={step==='details'?'active':''} aria-current={step==='details'?'step':undefined}><span>2</span>填写提议信息</li></ol>
  <div className="proposal-dialog-body">
   <div hidden={step!=='choose'}><StandardPicker endpoint="/standards" onSelect={s=>{if(entry?.key!==s.key)setKeys([]);setEntry(s);setError('');setStep('details')}}/></div>
   {entry&&<div hidden={step!=='details'} className="proposal-dialog-details">
    <section className="proposal-selected-direction"><span className="proposal-direction-icon"><Lightbulb size={21}/></span><div><small>{disciplineLabel(entry.discipline)}</small><h3><MathText inline>{entry.name_zh}</MathText></h3>{entry.name_zh!==entry.label&&<p className="proposal-direction-english"><MathText inline>{entry.label}</MathText></p>}<p><MathText inline>{entry.description}</MathText></p></div><button type="button" disabled={busy} onClick={()=>setStep('choose')}><ArrowLeft size={14}/>重新选择</button></section>
    <fieldset className="proposal-source-fields" disabled={busy}><legend><Layers3 size={16}/>所属分类与会议<span>必选</span></legend><p>选择这个主题应展示在哪些主类下，可多选。</p>{catalog.error?<ErrorBox error={catalog.error} reload={catalog.reload}/>:<SourcePicker sources={catalog.data||[]} selected={keys} onChange={setKeys}/>}</fieldset>
    <label className="proposal-note"><span>提议说明<small>选填</small></span><textarea value={note} rows={3} maxLength={1000} onChange={e=>setNote(e.target.value)} placeholder="简述这个方向与您的研究有什么关系" disabled={busy}/><small>{note.length} / 1000</small></label>
    {error&&<p role="alert" className="error-text">{error}</p>}
   </div>}
  </div>
  <footer className="proposal-dialog-footer"><span>{step==='choose'?'从自建研究方向表中选择':`已选 ${keys.length} 个分类 / 会议`}</span><div><button type="button" disabled={busy} onClick={()=>setOpen(false)}>取消</button>{step==='choose'?entry&&<button type="button" className="primary" onClick={()=>setStep('details')}>下一步<ArrowRight size={15}/></button>:<button type="button" className="primary" disabled={busy||!keys.length} onClick={()=>void submit()}><Send size={15}/>{busy?'正在提交…':'提交提议'}</button>}</div></footer>
 </Modal>}</section>
}
