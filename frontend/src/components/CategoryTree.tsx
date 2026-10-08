import {useState} from 'react'
import {ChevronDown,ChevronRight,Search} from 'lucide-react'
import type {Category,Topic} from '../types'
import {groupSources,sourceMatches,topicMatches} from '../disciplines'
import {DisciplineSection} from './DisciplineSection'
import {useTreeExpansion} from '../hooks/useTreeExpansion'
import {TreeExpansionControls} from './TreeExpansionControls'

export function CategoryTree({catalog,category,topic,onSelect,allLabel='全部论文',allCount,controlsVariant='link',search:controlledSearch,onSearchChange}:{catalog:Category[];category:string|null;topic:number|null;onSelect:(category:string|null,topic:number|null)=>void;allLabel?:string;allCount?:number;controlsVariant?:'link'|'buttons';search?:string;onSearchChange?:(value:string)=>void}){
 const [localSearch,setSearch]=useState('')
 const search=controlledSearch??localSearch
 const term=search.trim().toLowerCase(),tree=useTreeExpansion(term)
 const keys=[...groupSources(catalog).map(g=>'discipline:'+g.key),...catalog.map(c=>c.key)]
 const matches=(t:Topic)=>topicMatches(t,term)
 const sourceMatch=(c:Category)=>sourceMatches(c,term)
 const renderTopics=(c:Category)=>c.topics.filter(t=>!term||sourceMatch(c)||matches(t)).map(t=>{
  return <div key={t.id} className={'category-tree-row topic-leaf '+(category===c.key&&topic===t.id?'selected':'')} style={{paddingLeft:14}}>
   <span className="tree-spacer"/>
   <button className="tree-node-label" aria-pressed={category===c.key&&topic===t.id} onClick={()=>onSelect(c.key,category===c.key&&topic===t.id?null:t.id)}><span>{t.name_zh}{t.status==='proposed'&&<small>待审核</small>}</span><small>{t.paper_count??0}</small></button>
  </div>
 })
 const groups=groupSources(catalog.filter(c=>!term||sourceMatch(c)||c.topics.some(matches)))
 return <nav className="category-tree" aria-label="论文分类与主题"><label className="search-input"><Search size={15}/><input aria-label="搜索分类或主题" placeholder="搜索分类或主题" value={search} onChange={e=>{setSearch(e.target.value);onSearchChange?.(e.target.value)}}/></label><button className={'all-topics '+(!category&&!topic?'active':'')} aria-label={allLabel} onClick={()=>onSelect(null,null)}><span>{allLabel}</span>{allCount!==undefined&&<small>{allCount}</small>}</button>
  <TreeExpansionControls variant={controlsVariant} onExpand={()=>tree.setAll(keys,true)} onCollapse={()=>tree.setAll(keys,false)}/>
  {groups.map(group=><DisciplineSection key={group.key} label={group.label} count={group.sources.length} expanded={tree.isOpen('discipline:'+group.key)} onToggle={()=>tree.toggle('discipline:'+group.key)}>{group.sources.map(c=>{
   const isOpen=tree.isOpen(c.key)
   return <div key={c.key}><div className={'category-tree-row source-node '+(category===c.key&&topic===null?'selected':'')}>
    <button className="tree-toggle" aria-label={(isOpen?'收起':'展开')+c.code+'分类'} aria-expanded={isOpen} onClick={()=>tree.toggle(c.key)}>{isOpen?<ChevronDown size={15}/>:<ChevronRight size={15}/>}</button>
    <button className="tree-node-label" aria-pressed={category===c.key&&topic===null} onClick={()=>onSelect(c.key,null)}><span>{c.code}<small>{c.kind==='venue'?'顶会':c.label}</small></span><small>{c.paper_count}</small></button>
   </div>{isOpen&&(c.topics.length?renderTopics(c):<p className="tree-empty">暂无主题</p>)}</div>
  })}</DisciplineSection>)}{!groups.length&&<p className="tree-empty">没有匹配的分类或主题。</p>}
 </nav>
}
