import {useLayoutEffect,useRef,useState} from 'react'
import {createPortal} from 'react-dom'
import {Check,RotateCcw,SlidersHorizontal,X} from 'lucide-react'
import type {Category,Topic} from '../types'
import {SearchSelect} from './ui/SearchSelect'
import './library-filter.css'

export type ShelfFilter={categories:string[];topics:number[]}
type Props={label:string;catalog:Category[];loading:boolean;error:string;onReload:()=>void;topics:Topic[];topicsLoading:boolean;topicsError:string;onTopicsReload:()=>void;onOpen:()=>void;value:ShelfFilter;onApply:(value:ShelfFilter)=>void}

export function LibraryFilter(props:Props){
 const [open,setOpen]=useState(false),anchor=useRef<HTMLButtonElement>(null)
 const filtered=!!(props.value.categories.length||props.value.topics.length)
 return <><button ref={anchor} type="button" className={'library-filter-trigger'+(filtered?' is-filtered':'')} aria-haspopup="dialog" aria-expanded={open} onClick={()=>{props.onOpen();setOpen(true)}}><SlidersHorizontal size={16}/>筛选{filtered&&<span className="library-filter-dot"/>}</button>{open&&<FilterDialog {...props} anchor={anchor.current} onClose={()=>setOpen(false)}/>}</>
}

function FilterDialog({label,catalog,loading,error,onReload,topics,topicsLoading,topicsError,onTopicsReload,value,onApply,anchor,onClose}:Props&{anchor:HTMLButtonElement|null;onClose:()=>void}){
 const dialog=useRef<HTMLDialogElement>(null),[draft,setDraft]=useState(value)
 useLayoutEffect(()=>{
  const element=dialog.current
  if(!element)return
  const place=()=>{
   const rect=anchor?.getBoundingClientRect()
   element.style.setProperty('--filter-left',`${Math.max(12,Math.min((rect?.right||window.innerWidth)-400,window.innerWidth-412))}px`)
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
    <div><SearchSelect label="分类和会议" multiple loading={loading} placeholder="全部分类和会议" searchPlaceholder="搜索分类代码、会议或名称" options={catalog.map(c=>({value:c.key,label:c.code,description:c.label===c.code?undefined:c.label,keywords:[c.label_en,c.label_zh,c.discipline,c.discipline_label].join(' '),group:c.discipline_label||c.group}))} selected={draft.categories} onChange={categories=>setDraft(old=>({...old,categories}))}/>{error&&<p className="library-filter-error">分类加载失败 <button type="button" onClick={onReload}>重新加载</button></p>}</div>
    <div><SearchSelect label="主题" multiple loading={topicsLoading} placeholder="全部主题" searchPlaceholder="搜索主题名称、英文名或编号" options={topics.map(t=>({value:String(t.id),label:t.name_zh||t.name_en,description:t.name_en!==t.name_zh?t.name_en:undefined,keywords:[t.id,t.standard_system,t.standard_code].join(' ')}))} selected={draft.topics.map(String)} onChange={ids=>setDraft(old=>({...old,topics:ids.map(Number)}))}/>{topicsError&&<p className="library-filter-error">主题加载失败 <button type="button" onClick={onTopicsReload}>重新加载</button></p>}</div>
   </div>
   <footer className="library-filter-footer"><button type="button" onClick={()=>setDraft({categories:[],topics:[]})}><RotateCcw size={15}/>重置</button><button type="submit" className="library-filter-apply"><Check size={16}/>应用筛选</button></footer>
  </form>
 </dialog>,document.body)
}
