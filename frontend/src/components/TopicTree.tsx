import {useState} from 'react'
import {Search} from 'lucide-react'
import type {Topic} from '../types'

export function Checkbox({checked,mixed=false,label,onChange,disabled=false}:{checked:boolean;mixed?:boolean;label:string;onChange:()=>void;disabled?:boolean}){
 return <input type="checkbox" aria-label={label} aria-checked={mixed?'mixed':checked} checked={checked} disabled={disabled} ref={node=>{if(node)node.indeterminate=mixed}} onChange={onChange}/>
}
export function TopicTree({topics,selected,onSelect,multiple=false,showSearch=true}:{topics:Topic[];selected:number[];onSelect:(id:number)=>void;multiple?:boolean;showSearch?:boolean}) {
 const [search,setSearch]=useState('')
 const term=search.trim().toLowerCase()
 const render=()=>topics.filter(t=>!term||(t.name_zh+' '+t.name_en).toLowerCase().includes(term)).map(t=>{
  const checked=selected.includes(t.id)
  return <div key={t.id} className={'topic-row '+(checked?'selected':'')} style={{paddingLeft:8}}>
   {multiple?<label className="topic-name"><Checkbox checked={checked} label={t.name_zh} onChange={()=>onSelect(t.id)}/><span className="topic-label"><span>{t.name_zh}</span></span><small className="topic-count">{t.paper_count??t.today_count??''}</small></label>:<button className="topic-name" onClick={()=>onSelect(t.id)}><span>{t.name_zh}</span><small>{t.paper_count??t.today_count??''}</small></button>}
  </div>
 })
 return <div className="topic-tree">{showSearch&&<label className="search-input"><Search size={15}/><input aria-label="搜索主题" placeholder="搜索研究主题" value={search} onChange={e=>setSearch(e.target.value)}/></label>}{render()}</div>
}
