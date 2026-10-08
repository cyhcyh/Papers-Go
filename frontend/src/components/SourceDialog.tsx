import {useState,type ReactNode} from 'react'
import {Boxes,CalendarDays,Check,Layers,Play,Search} from 'lucide-react'
import {Modal,Loading,ErrorBox} from './Common'
import {disciplines,disciplineLabel} from '../disciplines'
import type {Source} from './SourceManager'
import './source-dialog.css'

export type VenueOption={code:string;label:string}
type Props={
 source:Source;busy:boolean;error:string;test:string;canSave:boolean;addingCount:number
 onChange:(source:Source)=>void;onKindChange:(kind:Source['kind'])=>void
 onClose:()=>void;onSubmit:()=>void;onTest:()=>void;arxivPicker:ReactNode
 venues:VenueOption[];venuesLoading:boolean;venuesError:string;reloadVenues:()=>void;existingVenues:string[]
}

function VenuePicker({venues,selected,existing,busy,onSelect}:{venues:VenueOption[];selected:string;existing:string[];busy:boolean;onSelect:(venue:VenueOption)=>void}){
 const [search,setSearch]=useState(''),term=search.trim().toLowerCase(),added=new Set(existing.map(code=>code.toLowerCase()))
 const matches=venues.filter(venue=>(venue.code+' '+venue.label).toLowerCase().includes(term))
 return <div className="venue-picker"><div className="source-dialog-section-heading"><h3>选择会议</h3><label className="venue-picker-search"><Search size={16} aria-hidden="true"/><input aria-label="搜索会议名称" placeholder="搜索会议名称" value={search} disabled={busy} onChange={event=>setSearch(event.target.value)}/></label></div><div className="venue-picker-list" role="radiogroup" aria-label="可选会议">{matches.map(venue=>{
  const exists=added.has(venue.code.toLowerCase()),checked=selected===venue.code
  return <button type="button" role="radio" aria-checked={checked} aria-label={venue.code+' '+venue.label} key={venue.code} className={'venue-choice'+(checked?' selected':'')+(exists?' already-added':'')} disabled={busy||exists} onClick={()=>onSelect(venue)}><span className="venue-choice-text"><strong>{venue.code}</strong><small>{venue.label}</small></span>{exists?<span className="venue-choice-added">已添加</span>:<span className="venue-choice-check" aria-hidden="true">{checked&&<Check size={13}/>}</span>}</button>
 })}{!matches.length&&<p className="venue-picker-empty">没有匹配的会议。</p>}</div></div>
}

export function SourceDialog({source,busy,error,test,canSave,addingCount,onChange,onKindChange,onClose,onSubmit,onTest,arxivPicker,venues,venuesLoading,venuesError,reloadVenues,existingVenues}:Props){
 const isNewArxiv=source.kind==='arxiv'&&!source.key,isNewVenue=source.kind==='venue'&&!source.key
 const change=(patch:Partial<Source>)=>onChange({...source,...patch})
 return <Modal className="source-dialog" title={source.key?'编辑分类与来源':'添加分类与来源'} titleIcon={<span className="source-dialog-icon"><Layers size={22}/></span>} onClose={onClose}><p className="source-dialog-intro">选择分类或会议，配置抓取与游客展示。</p><form className="source-dialog-form" onSubmit={event=>{event.preventDefault();if(canSave)onSubmit()}}><div className="source-kind-switch" role="radiogroup" aria-label="来源类型">{(['arxiv','venue'] as const).map(kind=><button type="button" role="radio" key={kind} aria-checked={source.kind===kind} className={source.kind===kind?'selected':''} disabled={busy||!!source.key} onClick={()=>onKindChange(kind)}>{kind==='arxiv'?<Boxes size={17}/>:<CalendarDays size={17}/>}<span>{kind==='arxiv'?'arXiv 分类':'会议'}</span></button>)}</div><div className="source-dialog-body">
 {isNewArxiv&&<section className="source-arxiv-section"><div className="source-dialog-section-heading"><h3>选择官方分类</h3><span>可多选</span></div>{arxivPicker}<p className="source-dialog-hint">使用官方中文名称，添加后可单独编辑。</p></section>}
 {isNewVenue&&(venuesError?<ErrorBox error={venuesError} reload={reloadVenues}/>:venuesLoading?<Loading/>:<VenuePicker venues={venues} selected={source.code} existing={existingVenues} busy={busy} onSelect={venue=>change({code:venue.code,label:venue.code,feed_url:null})}/>)}
 {!!source.key&&<div className="source-dialog-identity"><span>{source.kind==='arxiv'?'分类代码':'会议代码'}</span><strong>{source.code}</strong>{source.kind==='arxiv'&&<small>{disciplineLabel(source.discipline)}</small>}</div>}
 <div className={'source-dialog-fields'+(isNewArxiv?' only-sort':'')}>
 {!isNewArxiv&&<label className="source-name-field">显示名称<input required maxLength={100} disabled={busy} value={source.label} onChange={event=>change({label:event.target.value})}/></label>}
 {source.kind==='venue'&&<label>所属学科<select disabled={busy} value={source.discipline||'Computer Science'} onChange={event=>change({discipline:event.target.value})}>{disciplines.map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label>}
 <label>排序<input type="number" min={0} max={10000} disabled={busy} value={source.sort_order} onChange={event=>change({sort_order:Number(event.target.value)})}/><small>越小越靠前</small></label></div>
 <section className="source-dialog-options" aria-label="抓取与游客展示">{(['fetch_enabled','guest_default'] as const).map(key=><label className="source-dialog-option" key={key}><span><strong>{key==='fetch_enabled'?'自动抓取':'游客默认'}</strong><small>{key==='fetch_enabled'?'定时任务抓取此来源':'向游客展示此来源论文'}</small></span><input type="checkbox" role="switch" disabled={busy} checked={source[key]} onChange={event=>change({[key]:event.target.checked})}/></label>)}</section>
 {error&&<p className="source-dialog-error" role="alert">{error}</p>}{test&&<p className="source-dialog-test" role="status"><Check size={15}/>{test}</p>}
 </div><footer className="source-dialog-footer">{!isNewArxiv&&<button type="button" className="source-test-button" disabled={busy||!source.code} onClick={onTest}><Play size={14}/>测试来源</button>}<div><button type="button" disabled={busy} onClick={onClose}>取消</button><button type="submit" className="primary" disabled={!canSave}>{busy?'正在处理…':isNewArxiv?`添加所选 ${addingCount} 个分类`:source.key?'保存更改':'添加会议'}</button></div></footer></form></Modal>
}
