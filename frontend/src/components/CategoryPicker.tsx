import {useState} from 'react'
import {ChevronDown,ChevronRight,Search} from 'lucide-react'
import {Checkbox,TopicTree} from './TopicTree'
import type {Category,CategorySelection,Profile} from '../types'
import {groupSources,sourceMatches,topicMatches} from '../disciplines'
import {DisciplineSection} from './DisciplineSection'
import {useTreeExpansion} from '../hooks/useTreeExpansion'
import {TreeExpansionControls} from './TreeExpansionControls'
import '../category-picker.css'

export function profileSelection(profile:Profile|null,catalog:Category[]):CategorySelection{
 if(profile?.structured.category_selection){
  const old=profile.structured.category_selection
  return withWeights({categories:old.categories.filter(key=>catalog.some(c=>c.key===key)),topics:Object.fromEntries(Object.entries(old.topics).filter(([key])=>catalog.some(c=>c.key===key)).map(([key,ids])=>[key,ids.filter(id=>catalog.find(c=>c.key===key)!.topics.some(t=>t.id===id))])),weights:old.weights})
 }
 const old=profile?.structured.topic_ids||[],selection:CategorySelection={categories:[],topics:{}}
 catalog.forEach(c=>{const ids=c.topics.filter(t=>old.includes(t.id)).map(t=>t.id);if(ids.length)selection.topics[c.key]=ids})
 return withWeights(selection)
}
function withWeights(selection:CategorySelection):CategorySelection{
 const topics=Object.fromEntries(Object.entries(selection.topics).filter(([,ids])=>ids.length))
 const keys=[...new Set([...selection.categories,...Object.keys(topics)])]
 return {...selection,topics,weights:Object.fromEntries(keys.map(key=>[key,selection.weights?.[key]??.7]))}
}
export function CategoryPicker({catalog,selection,onChange}:{catalog:Category[];selection:CategorySelection;onChange:(s:CategorySelection)=>void}){
 const [search,setSearch]=useState('')
 const term=search.trim().toLowerCase(),groups=groupSources(catalog.filter(c=>sourceMatches(c,term)||c.topics.some(t=>topicMatches(t,term))))
 const tree=useTreeExpansion(term),keys=[...groupSources(catalog).map(g=>'discipline:'+g.key),...catalog.map(c=>c.key)]
 const selectedTopics=new Set(catalog.flatMap(c=>selection.categories.includes(c.key)?c.topics.map(t=>t.id):selection.topics[c.key]||[])).size
 const toggleCategory=(c:Category)=>{
  const isAll=selection.categories.includes(c.key),partial={...selection.topics};delete partial[c.key]
  onChange(withWeights({categories:isAll?selection.categories.filter(key=>key!==c.key):[...selection.categories,c.key],topics:partial,weights:selection.weights}))
 }
 const toggleTopic=(c:Category,id:number)=>{
  const ids=selection.categories.includes(c.key)?c.topics.map(t=>t.id):selection.topics[c.key]||[]
  const next=ids.includes(id)?ids.filter(t=>t!==id):[...ids,id],all=c.topics.length>0&&c.topics.every(t=>next.includes(t.id)),partial={...selection.topics}
  if(all||!next.length)delete partial[c.key];else partial[c.key]=next
  onChange(withWeights({categories:[...selection.categories.filter(key=>key!==c.key),...(all?[c.key]:[])],topics:partial,weights:selection.weights}))
 }
 return <div className="category-picker"><label className="search-input"><Search size={15}/><input aria-label="搜索关注分类或主题" placeholder="分类代码、中文或英文名称、主题" value={search} onChange={e=>setSearch(e.target.value)}/></label><TreeExpansionControls summary={'已选分类 '+selection.categories.length+' · 主题 '+selectedTopics} onExpand={()=>tree.setAll(keys,true)} onCollapse={()=>tree.setAll(keys,false)}/>{groups.map(group=>{
 const selected=catalog.filter(c=>group.sources.some(s=>s.key===c.key)&&(selection.categories.includes(c.key)||selection.topics[c.key]?.length)).length
 return <DisciplineSection key={group.key} label={group.label} count={group.sources.length} summary={selected?'已选 '+selected+' 类':undefined} expanded={tree.isOpen('discipline:'+group.key)} onToggle={()=>tree.toggle('discipline:'+group.key)}>{group.sources.map(c=>{
  const checked=selection.categories.includes(c.key),ids=checked?c.topics.map(t=>t.id):selection.topics[c.key]||[],expanded=tree.isOpen(c.key),weight=selection.weights?.[c.key]??.7
  return <div className="category-option" role="group" aria-label={c.code+'分类'} key={c.key}><div className="category-choice"><button type="button" className="tree-toggle" aria-label={(expanded?'收起':'展开')+c.label+'的主题'} aria-expanded={expanded} onClick={()=>tree.toggle(c.key)}>{expanded?<ChevronDown size={15}/>:<ChevronRight size={15}/>}</button><label><Checkbox checked={checked} mixed={!checked&&ids.length>0} label={'关注分类 '+c.code} onChange={()=>toggleCategory(c)}/><span>{c.code}<small>{c.kind==='arxiv'?c.label:'顶会'}</small></span><span className="category-count">{c.paper_count}</span></label>{(checked||ids.length>0)&&<><span className="tree-selection-summary">{checked?'全部主题':'已选 '+ids.length}</span><label className="category-weight"><span>关注程度</span><select aria-label={c.code+'关注程度'} value={weight} onChange={e=>onChange(withWeights({...selection,weights:{...selection.weights,[c.key]:Number(e.target.value)}}))}>{![.4,.7,1].includes(weight)&&<option value={weight}>当前程度</option>}<option value={.4}>一般关注</option><option value={.7}>持续关注</option><option value={1}>重点关注</option></select></label></>}</div>{expanded&&<div className="category-children">{c.topics.length?<TopicTree showSearch={false} topics={c.topics.filter(t=>!term||sourceMatches(c,term)||topicMatches(t,term))} multiple selected={ids} onSelect={id=>toggleTopic(c,id)}/>:<p className="hint">关注此分类后，新主题也会自动包含。</p>}</div>}</div>
 })}</DisciplineSection>})}{!groups.length&&<p className="tree-empty">没有匹配的分类或主题。</p>}</div>
}
