import {useState} from 'react'
import {Search} from 'lucide-react'
import {groupSources,sourceMatches,sourceGroupState,toggleSourceGroup,sourceDiscipline,type SourceNode} from '../disciplines'
import {DisciplineSection} from './DisciplineSection'
import {Checkbox} from './TopicTree'
import {useTreeExpansion} from '../hooks/useTreeExpansion'
import {TreeExpansionControls} from './TreeExpansionControls'

export function SourcePicker({sources,selected,onChange,multiple=true,label='搜索分类或会议',selectGroups=false,disabledKeys=[],disabled=false}:{sources:SourceNode[];selected:string[];onChange:(keys:string[])=>void;multiple?:boolean;label?:string;selectGroups?:boolean;disabledKeys?:string[];disabled?:boolean}){
 const [search,setSearch]=useState(''),term=search.trim().toLowerCase()
 const groups=groupSources(sources.filter(s=>sourceMatches(s,term)))
 const tree=useTreeExpansion(term),keys=groupSources(sources).map(g=>'discipline:'+g.key)
 const choose=(source:SourceNode)=>{
  if(disabled||disabledKeys.includes(source.key))return
  onChange(multiple?(selected.includes(source.key)?selected.filter(k=>k!==source.key):[...new Set([...selected,source.key])]):[source.key])
 }
 return <div className="source-picker"><label className="search-input"><Search size={15}/><input aria-label={label} placeholder="分类代码、中文或英文名称" value={search} disabled={disabled} onChange={e=>setSearch(e.target.value)}/></label><TreeExpansionControls disabled={disabled} summary={'已选 '+selected.length+' 个分类'} onExpand={()=>tree.setAll(keys,true)} onCollapse={()=>tree.setAll(keys,false)}/>{selectGroups&&<p className="hint source-group-hint">勾选学科会选择该学科全部可添加分类，包含搜索未显示的分类。</p>}<div className="source-picker-list">{groups.map(group=>{
  const state=sourceGroupState(sources,selected,group.key,disabledKeys)
  const selection=multiple&&selectGroups?<Checkbox label={'全选'+group.label+'全部分类（'+state.available.length+'个可添加）'} checked={state.checked} mixed={state.mixed} disabled={disabled||!state.available.length} onChange={()=>onChange(toggleSourceGroup(sources,selected,group.key,disabledKeys))}/>:undefined
  return <DisciplineSection key={group.key} label={group.label} count={selectGroups?sources.filter(s=>sourceDiscipline(s)===group.key).length:group.sources.length} summary={state.selectedCount?'已选 '+state.selectedCount:undefined} selection={selection} expanded={tree.isOpen('discipline:'+group.key)} onToggle={()=>tree.toggle('discipline:'+group.key)}>{selectGroups&&<p className="hint source-group-count">可添加 {state.available.length} 项 · 已选 {state.selectedCount} 项{term?` · 搜索匹配 ${group.sources.length} 项`:''}</p>}<table className="source-choice-table"><tbody>{group.sources.map(source=>{
   const exists=disabledKeys.includes(source.key)
   return <tr key={source.key} className={exists?'source-already-added':''}><td><input type={multiple?'checkbox':'radio'} name={multiple?undefined:'official-arxiv-category'} aria-label={'选择分类 '+source.code} checked={exists||selected.includes(source.key)} disabled={disabled||exists} onChange={()=>choose(source)}/></td><td><button type="button" className="source-choice-label" disabled={disabled||exists} onClick={()=>choose(source)}><strong>{source.code}{source.kind==='venue'&&<small>顶会</small>}{exists&&<small>已添加</small>}</strong><span>{source.label}{source.label_en&&source.label_en!==(source.label)&&<small>{source.label_en}</small>}</span></button></td></tr>
  })}</tbody></table></DisciplineSection>
 })}{!groups.length&&<p className="tree-empty">没有匹配的分类。</p>}</div><small className="hint">{multiple?`已选 ${selected.length} 个${selectGroups?'分类':'主类'}`:selected[0]?`已选 ${sources.find(s=>s.key===selected[0])?.code||selected[0]}`:'请选择一个分类或会议'}</small></div>
}
