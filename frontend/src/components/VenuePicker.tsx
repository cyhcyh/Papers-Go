import {useState} from 'react'
import {Check,Play,Search,X} from 'lucide-react'
import {disciplines,disciplineLabel,groupSources,sourceMatches} from '../disciplines'
import {DisciplineSection} from './DisciplineSection'

export type VenueOption={code:string;label:string;discipline:string}
export type VenueDraft={code:string;label:string;discipline:string;fetch_enabled:boolean;guest_default:boolean;sort_order:number}

export function VenuePicker({venues,selected,existing,busy,onChange,onTest}:{venues:VenueOption[];selected:VenueDraft[];existing:string[];busy:boolean;onChange:(venues:VenueDraft[])=>void;onTest:(venue:VenueDraft)=>void}){
 const [search,setSearch]=useState(''),term=search.trim().toLowerCase(),added=new Set(existing.map(code=>code.toLowerCase()))
 const sources=venues.map(venue=>({...venue,key:venue.code,kind:'venue' as const}))
 const matches=sources.filter(venue=>sourceMatches(venue,term)),available=matches.filter(venue=>!added.has(venue.code.toLowerCase()))
 const selectedCodes=new Set(selected.map(venue=>venue.code))
 const append=(choices:VenueOption[])=>onChange([...selected,...choices.filter(venue=>!selectedCodes.has(venue.code)&&!added.has(venue.code.toLowerCase())).map(venue=>({code:venue.code,label:venue.code,discipline:venue.discipline,fetch_enabled:true,guest_default:true,sort_order:100}))])
 const toggle=(venue:VenueOption)=>selectedCodes.has(venue.code)?onChange(selected.filter(item=>item.code!==venue.code)):append([venue])
 const update=(code:string,patch:Partial<VenueDraft>)=>onChange(selected.map(venue=>venue.code===code?{...venue,...patch}:venue))
 return <section className="venue-picker">
  <div className="source-dialog-section-heading"><h3>选择会议</h3><label className="venue-picker-search"><Search size={16} aria-hidden="true"/><input aria-label="搜索会议名称或学科" placeholder="搜索会议或学科" value={search} disabled={busy} onChange={event=>setSearch(event.target.value)}/></label></div>
  <div className="venue-selection-tools"><span role="status">已选 {selected.length} 个会议</span><div><button type="button" disabled={busy||!available.some(venue=>!selectedCodes.has(venue.code))} onClick={()=>append(available)}>全选搜索结果</button><button type="button" disabled={busy||!selected.length} onClick={()=>onChange([])}>清空选择</button></div></div>
  <div className="venue-catalog" aria-label="可选会议">{groupSources(matches).map(group=><DisciplineSection key={group.key} label={group.label} count={group.sources.length} initialOpen forceOpen={!!term}>
   <div className="venue-picker-list">{group.sources.map(venue=>{
    const exists=added.has(venue.code.toLowerCase()),checked=selectedCodes.has(venue.code)
    return <button type="button" role="checkbox" aria-checked={exists||checked} aria-label={venue.code+' '+venue.label} key={venue.code} className={'venue-choice'+(checked?' selected':'')+(exists?' already-added':'')} disabled={busy||exists} onClick={()=>toggle(venue)}><span className="venue-choice-text"><strong>{venue.code}</strong><small>{venue.label}</small></span>{exists?<span className="venue-choice-added">已添加</span>:<span className="venue-choice-check" aria-hidden="true">{checked&&<Check size={13}/>}</span>}</button>
   })}</div>
  </DisciplineSection>)}{!matches.length&&<p className="venue-picker-empty">没有匹配的会议。</p>}</div>
  {!!selected.length&&<section className="venue-drafts" aria-label="待添加会议"><div className="source-dialog-section-heading"><h3>待添加会议</h3><span>每个会议单独配置</span></div><div className="venue-draft-list">{selected.map(venue=><article className="venue-draft" key={venue.code}>
   <header><div><strong>{venue.code}</strong><span>{disciplineLabel(venue.discipline)}</span></div><button type="button" className="icon-button" disabled={busy} aria-label={'移除 '+venue.code} onClick={()=>onChange(selected.filter(item=>item.code!==venue.code))}><X size={16}/></button></header>
   <div className="source-dialog-fields"><label>显示名称<input aria-label={venue.code+' 显示名称'} required maxLength={100} disabled={busy} value={venue.label} onChange={event=>update(venue.code,{label:event.target.value})}/></label><label>所属学科<select aria-label={venue.code+' 所属学科'} disabled={busy} value={venue.discipline} onChange={event=>update(venue.code,{discipline:event.target.value})}>{disciplines.map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label><label>排序<input aria-label={venue.code+' 排序'} type="number" min={0} max={10000} required disabled={busy} value={venue.sort_order} onChange={event=>update(venue.code,{sort_order:Number(event.target.value)})}/></label></div>
   <div className="venue-draft-options">{(['fetch_enabled','guest_default'] as const).map(key=><label className="source-dialog-option" key={key}><span><strong>{key==='fetch_enabled'?'自动抓取':'游客默认'}</strong></span><input type="checkbox" role="switch" aria-label={venue.code+' '+(key==='fetch_enabled'?'自动抓取':'游客默认')} disabled={busy} checked={venue[key]} onChange={event=>update(venue.code,{[key]:event.target.checked})}/></label>)}<button type="button" className="venue-test-button" disabled={busy} onClick={()=>onTest(venue)}><Play size={13}/>测试来源</button></div>
  </article>)}</div></section>}
 </section>
}
