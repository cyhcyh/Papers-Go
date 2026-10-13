import {useLayoutEffect,useRef,useState} from 'react'
import {createPortal} from 'react-dom'
import {Check,ChevronDown,RotateCcw,SlidersHorizontal,X} from 'lucide-react'
import type {Category} from '../types'
import './library-filter.css'

export type ShelfFilter={category:string|null;topic:number|null}
type Props={label:string;catalog:Category[];loading:boolean;error:string;onReload:()=>void;value:ShelfFilter;onApply:(value:ShelfFilter)=>void}

export function LibraryFilter(props:Props){
 const [open,setOpen]=useState(false),anchor=useRef<HTMLButtonElement>(null)
 const filtered=!!(props.value.category||props.value.topic)
 return <><button ref={anchor} type="button" className={'library-filter-trigger'+(filtered?' is-filtered':'')} aria-haspopup="dialog" aria-expanded={open} onClick={()=>setOpen(true)}><SlidersHorizontal size={16}/>筛选{filtered&&<span className="library-filter-dot"/>}</button>{open&&<FilterDialog {...props} anchor={anchor.current} onClose={()=>setOpen(false)}/>}</>
}

function FilterDialog({label,catalog,loading,error,onReload,value,onApply,anchor,onClose}:Props&{anchor:HTMLButtonElement|null;onClose:()=>void}){
 const dialog=useRef<HTMLDialogElement>(null),[draft,setDraft]=useState(value)
 const source=catalog.find(c=>c.key===draft.category)
 const topics=source?.topics.filter(t=>!t.status||t.status==='active')||[]
 const groups=[...new Set(catalog.map(c=>c.discipline_label||c.group))]
 useLayoutEffect(()=>{
  const element=dialog.current
  if(!element)return
  const place=()=>{
   const rect=anchor?.getBoundingClientRect()
   element.style.setProperty('--filter-left',`${Math.max(12,Math.min((rect?.right||window.innerWidth)-340,window.innerWidth-352))}px`)
   element.style.setProperty('--filter-top',`${Math.max(12,Math.min((rect?.bottom||80)+9,window.innerHeight-element.offsetHeight-12))}px`)
  }
  element.showModal();place()
  const previous=document.body.style.overflow;document.body.style.overflow='hidden'
  window.addEventListener('resize',place)
  return()=>{window.removeEventListener('resize',place);element.close();document.body.style.overflow=previous}
 },[anchor])
 return createPortal(<dialog ref={dialog} role="dialog" className="library-filter-dialog" aria-label={'筛选'+label} onCancel={e=>{e.preventDefault();onClose()}} onClick={e=>{
  if(e.target!==e.currentTarget)return
  const rect=e.currentTarget.getBoundingClientRect()
  if(e.clientX<rect.left||e.clientX>rect.right||e.clientY<rect.top||e.clientY>rect.bottom)onClose()
 }}>
  <div className="library-filter-handle"/>
  <header className="library-filter-header"><span className="library-filter-icon"><SlidersHorizontal size={19}/></span><div><h2>筛选{label}</h2><p>仅筛选当前标签页的论文</p></div><button type="button" className="library-filter-close" aria-label="关闭筛选" onClick={onClose}><X size={18}/></button></header>
  <form onSubmit={e=>{e.preventDefault();onApply(draft);onClose()}}>
   <div className="library-filter-fields">
    {error&&<p className="library-filter-error">分类加载失败 <button type="button" onClick={onReload}>重新加载</button></p>}
    <label>分类和会议<div className="library-filter-select"><select aria-label="分类和会议" disabled={loading||!!error} value={draft.category||''} onChange={e=>setDraft({category:e.target.value||null,topic:null})}><option value="">{loading?'正在加载…':'全部分类和会议'}</option>{groups.map(group=><optgroup key={group} label={group}>{catalog.filter(c=>(c.discipline_label||c.group)===group).map(c=><option key={c.key} value={c.key}>{c.code}{c.label&&c.label!==c.code?' · '+c.label:''}</option>)}</optgroup>)}</select><ChevronDown size={15}/></div></label>
    <label>主题<div className="library-filter-select"><select aria-label="主题" disabled={!source||loading||!!error} value={draft.topic||''} onChange={e=>setDraft(old=>({...old,topic:e.target.value?Number(e.target.value):null}))}><option value="">{!source?'请先选择分类和会议':topics.length?'全部主题':'此分类暂无主题'}</option>{topics.map(t=><option key={t.id} value={t.id}>{t.name_zh||t.name_en}</option>)}</select><ChevronDown size={15}/></div></label>
   </div>
   <footer className="library-filter-footer"><button type="button" onClick={()=>setDraft({category:null,topic:null})}><RotateCcw size={15}/>重置</button><button type="submit" className="library-filter-apply" disabled={loading||!!error}><Check size={16}/>应用筛选</button></footer>
  </form>
 </dialog>,document.body)
}
