import {useEffect,useId,useMemo,useRef,useState,type ReactNode} from 'react'
import {Check,ChevronDown,Search,X} from 'lucide-react'
import './search-select.css'

export type SearchOption={value:string;label:string;description?:string;keywords?:string;group?:string;disabled?:boolean}
type Props={label:string;ariaLabel?:string;placeholder?:string;searchPlaceholder?:string;options:SearchOption[];selected:string[];onChange:(values:string[])=>void;multiple?:boolean;disabled?:boolean;loading?:boolean;icon?:ReactNode}

/** Small local picker: searching never changes the selection or calls an API. */
export function SearchSelect({label,ariaLabel=label,placeholder='请选择',searchPlaceholder='搜索名称或关键词',options,selected,onChange,multiple=false,disabled=false,loading=false,icon}:Props){
 const id=useId(),root=useRef<HTMLDivElement>(null),input=useRef<HTMLInputElement>(null),trigger=useRef<HTMLButtonElement>(null)
 const [open,setOpen]=useState(false),[query,setQuery]=useState(''),[visible,setVisible]=useState(80),[active,setActive]=useState(0)
 const matches=useMemo(()=>{const term=query.trim().toLocaleLowerCase();return options.filter(o=>!term||[o.label,o.description,o.keywords,o.group].join(' ').toLocaleLowerCase().includes(term))},[options,query])
 const shown=matches.slice(0,visible),values=useMemo(()=>new Set(selected),[selected])
 const names=useMemo(()=>new Map(options.map(o=>[o.value,o.label])),[options])
 useEffect(()=>{
  if(!open)return
  input.current?.focus()
  // Close after the click so an inline menu cannot move the clicked form button.
  const outside=(event:MouseEvent)=>{if(root.current&&!event.composedPath().includes(root.current))setOpen(false)}
  document.addEventListener('click',outside)
  return()=>document.removeEventListener('click',outside)
 },[open])
 useEffect(()=>{if(disabled)setOpen(false)},[disabled])
 useEffect(()=>{if(open)root.current?.querySelector('[data-active="true"]')?.scrollIntoView({block:'nearest'})},[active,open,query])
 const toggle=()=>{if(disabled)return;setOpen(!open);setQuery('');setVisible(80);setActive(0)}
 const choose=(option:SearchOption)=>{
  if(option.disabled||disabled)return
  onChange(multiple?(values.has(option.value)?selected.filter(v=>v!==option.value):[...selected,option.value]):[option.value])
  if(!multiple){setOpen(false);trigger.current?.focus()}
 }
 return <div ref={root} className={'search-select'+(open?' is-open':'')+(disabled?' is-disabled':'')} onKeyDown={event=>{
  if(event.key==='Escape'&&open){event.preventDefault();event.stopPropagation();setOpen(false);trigger.current?.focus()}
  if(event.target!==input.current)return
  if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();setActive(old=>Math.max(0,Math.min(shown.length-1,old+(event.key==='ArrowDown'?1:-1))))}
  if(event.key==='Enter'){event.preventDefault();const option=shown[active];if(option)choose(option)}
 }}>
  <span className="search-select-label" id={id+'-label'}>{label}</span>
  <div className="search-select-control" onClick={toggle}>
   {icon&&<span className="search-select-leading" aria-hidden="true">{icon}</span>}
   <div className="search-select-values" id={id+'-value'}>{selected.length?multiple?<>{selected.slice(0,2).map(value=><span className="search-select-chip" key={value}><span title={names.get(value)||value}>{names.get(value)||value}</span><button type="button" disabled={disabled} aria-label={'移除 '+(names.get(value)||value)} onClick={event=>{event.stopPropagation();onChange(selected.filter(v=>v!==value))}}><X size={12}/></button></span>)}{selected.length>2&&<span className="search-select-extra">+{selected.length-2}</span>}</>:<span className="search-select-single" title={names.get(selected[0])||selected[0]}>{names.get(selected[0])||selected[0]}</span>:<span className="search-select-placeholder">{placeholder}</span>}</div>
   <button ref={trigger} type="button" className="search-select-toggle" aria-label={ariaLabel} aria-describedby={id+'-value'} aria-haspopup="listbox" aria-expanded={open} aria-controls={id+'-list'} disabled={disabled} onClick={event=>{event.stopPropagation();toggle()}} onKeyDown={event=>{if(event.key==='ArrowDown'&&!open){event.preventDefault();toggle()}}}><ChevronDown size={15}/></button>
  </div>
  {open&&<div className="search-select-menu">
   <div className="search-select-search"><Search size={16}/><input ref={input} aria-label={'搜索'+ariaLabel} placeholder={searchPlaceholder} value={query} onChange={event=>{setQuery(event.target.value);setVisible(80);setActive(0)}} role="combobox" aria-autocomplete="list" aria-expanded="true" aria-controls={id+'-list'} aria-activedescendant={shown[active]?id+'-option-'+active:undefined}/>{query&&<button type="button" aria-label={'清空'+ariaLabel+'搜索'} onClick={()=>{setQuery('');setVisible(80);setActive(0);input.current?.focus()}}><X size={14}/></button>}</div>
   {multiple&&<div className="search-select-tools"><span>已选 {selected.length} 项</span><button type="button" disabled={loading||!matches.some(o=>!o.disabled&&!values.has(o.value))} onClick={()=>onChange([...new Set([...selected,...matches.filter(o=>!o.disabled).map(o=>o.value)])])}>全选搜索结果</button><button type="button" disabled={!selected.length} onClick={()=>onChange([])}>清空</button></div>}
   <div className="search-select-list" role="listbox" id={id+'-list'} aria-label={ariaLabel+'选项'} aria-multiselectable={multiple||undefined}>
    {loading?<p className="search-select-empty">正在加载…</p>:shown.map((option,index)=><div key={option.value}>{option.group&&(index===0||shown[index-1].group!==option.group)&&<p className="search-select-group">{option.group}</p>}<button type="button" role="option" id={id+'-option-'+index} aria-selected={values.has(option.value)} aria-disabled={option.disabled||undefined} disabled={option.disabled} tabIndex={-1} data-active={index===active} className={'search-select-option'+(values.has(option.value)?' is-selected':'')} onMouseDown={event=>event.preventDefault()} onClick={()=>choose(option)}>{multiple&&<span className="search-select-checkbox">{values.has(option.value)&&<Check size={13}/>}</span>}<span className="search-select-option-text"><strong>{option.label}</strong>{option.description&&<small>{option.description}</small>}</span>{!multiple&&values.has(option.value)&&<Check size={15}/>}</button></div>)}
    {!loading&&!matches.length&&<p className="search-select-empty">没有匹配项</p>}
   </div>
   {!loading&&matches.length>shown.length&&<button type="button" className="search-select-more" onClick={()=>setVisible(old=>old+80)}>继续显示 · 共 {matches.length} 项</button>}
  </div>}
 </div>
}
