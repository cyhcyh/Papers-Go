import {useEffect,useState} from 'react'
import {Search} from 'lucide-react'
import {useLoad} from '../hooks/useLoad'
import {ErrorBox,Loading} from './Common'
import {MathText} from './MathText'
import {disciplines,disciplineLabel} from '../disciplines'
export type Standard={key:string;system:string;code:string;label:string;name_zh:string;description:string;discipline:string;path:string;parent:null;has_children:false}
export function StandardPicker({onSelect,endpoint='/admin/standards'}:{onSelect:(entry:Standard)=>void;endpoint?:string}){
 const [query,setQuery]=useState(''),[search,setSearch]=useState(''),[system,setSystem]=useState('')
 useEffect(()=>{const timer=setTimeout(()=>setSearch(query),250);return()=>clearTimeout(timer)},[query])
 const result=useLoad<Standard[]>(`${endpoint}?system=${encodeURIComponent(system)}&query=${encodeURIComponent(search)}`)
 return <div className="standard-picker"><div className="standard-search"><label className="search-input"><Search size={15}/><input aria-label="搜索研究方向" placeholder="方向名称或关键词" value={query} onChange={e=>setQuery(e.target.value)}/></label><select aria-label="学科" value={system} onChange={e=>setSystem(e.target.value)}><option value="">全部学科</option>{disciplines.map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></div>{result.error?<ErrorBox error={result.error} reload={result.reload}/>:result.loading?<Loading/>:<div className="standard-results">{result.data?.map(s=><div className="standard-entry" key={s.key}><button type="button" onClick={()=>onSelect(s)}><small>{disciplineLabel(s.discipline)}</small><strong><MathText inline>{s.name_zh}</MathText></strong>{s.name_zh!==s.label&&<span><MathText inline>{s.label}</MathText></span>}<span><MathText inline>{s.description}</MathText></span></button></div>)}{!result.data?.length&&<p className="hint">没有匹配的方向，试试英文关键词。</p>}</div>}</div>
}
