import {useEffect,useState} from 'react'
import {BrowseDateFilters} from '../components/BrowseDateFilters'
import {Search,Bookmark,Heart,History} from 'lucide-react'
import {useLoad} from '../hooks/useLoad'
import {api} from '../api'
import {useApp} from '../context'
import {CategoryTree} from '../components/CategoryTree'
import {TreeDrawer} from '../components/TreeDrawer'
import {useViewportPanel} from '../hooks/useViewportPanel'
import {PaperCard} from '../components/PaperCard'
import {ReadingModal} from '../components/ReadingModal'
import {BrowsingHistory} from '../components/BrowsingHistory'
import {PageTitle,Loading,ErrorBox,Empty} from '../components/Common'
import {useHotkeys} from '../hooks/useHotkeys'
import type {Category,Paper,InteractionResult} from '../types'
export function Browse({library=false}:{library?:boolean}) {
 const {toast,auth,requireLogin}=useApp();const [category,setCategory]=useState<string|null>(null),[topic,setTopic]=useState<number|null>(null),[range,setRange]=useState('week'),[sort,setSort]=useState('date'),[query,setQuery]=useState(''),[search,setSearch]=useState(''),[tab,setTab]=useState('save'),[reading,setReading]=useState<Paper|null>(null),[offset,setOffset]=useState(0),[selected,setSelected]=useState(0)
 const [dateBasis,setDateBasis]=useState('paper'),[dateYear,setDateYear]=useState(String(new Date().getFullYear())),[dateMonth,setDateMonth]=useState(`${new Date().getFullYear()}-${String(new Date().getMonth()+1).padStart(2,'0')}`)
 const catalog=useLoad<Category[]>('/categories')
 const viewportPanel=useViewportPanel()
 const selectCategory=(key:string|null,id:number|null)=>{setCategory(key);setTopic(id);if(key!==category&&key?.toLowerCase().startsWith('venue:'))setRange('all');setOffset(0)}
 const tree=<div className="browse-category-tree">{catalog.error?<ErrorBox error={catalog.error} reload={catalog.reload}/>:<CategoryTree catalog={catalog.data||[]} category={category} topic={topic} onSelect={selectCategory} controlsVariant="buttons"/>}</div>
 const treeLabel=catalog.data?.find(c=>c.key===category)?.topics.find(t=>t.id===topic)?.name_zh||catalog.data?.find(c=>c.key===category)?.code||'全部论文'
 const path=library?`/library?type=${tab}`:`/browse?range=${['year','calendar_month'].includes(range)?'all':range}&date_basis=${dateBasis}${range==='year'&&/^\d{4}$/.test(dateYear)?'&year='+dateYear:''}${range==='calendar_month'&&/^\d{4}-\d{2}$/.test(dateMonth)?'&month='+dateMonth:''}&sort=${sort}&limit=30&offset=${offset}&query=${encodeURIComponent(search)}${category?'&category='+encodeURIComponent(category):''}${topic?'&topic_id='+topic:''}`
 const result=useLoad<{items:Paper[];total?:number}>(library&&tab==='history'?null:path)
 useEffect(()=>setOffset(0),[category,topic,range,sort,dateBasis,dateYear,dateMonth]);
 useEffect(()=>{const timer=setTimeout(()=>{setSearch(query);setOffset(0)},250);return ()=>clearTimeout(timer)},[query])
 const interaction=async(p:Paper,action:string)=>{if(action==='expand'&&!auth)return;if(action!=='expand'&&!requireLogin('保存阅读反馈',()=>{void interaction(p,action)}))return;try{const event=await api<InteractionResult>('/interactions','POST',{paper_id:p.id,action,feed_context:library?'library':'browse'});if(action!=='expand'){toast(action.startsWith('remove')?'已移除':action==='save'?'已收藏':'已标记喜欢');result.setData(data=>data?{...data,items:data.items.map(item=>item.id===p.id?{...item,liked:event.liked,saved:event.saved,like_count:event.like_count,save_count:event.save_count,expires_at:event.expires_at}:item).filter(item=>!library||(tab==='save'?item.saved:item.liked))}:data)}window.dispatchEvent(new Event('stats-change'))}catch(e){toast((e as Error).message)}}
 const read=(p:Paper)=>{setReading(p);void interaction(p,'expand')}
 useHotkeys(key=>{if(reading||library&&tab==='history')return;const p=result.data?.items[selected];if(!p)return;if(key==='s')void interaction(p,'save');if(key==='l')void interaction(p,'like');if(key==='enter')read(p);if(key==='x'){void interaction(p,'skip');setSelected(s=>Math.min(s+1,(result.data?.items.length||1)-1))}})
 return <><PageTitle eyebrow={library?'YOUR RESEARCH SHELF':'BROWSE BY CATEGORY'} title={library?'我的书架':'分类浏览'} description={library?'“喜欢”积累兴趣，“收藏”保存和追踪好文章。':'选择 arXiv 分类或会议，探索其中的文献。'}/>{library?<div className="tabs"><button className={tab==='like'?'active':''} onClick={()=>setTab('like')}><Heart size={16}/>喜欢</button><button className={tab==='save'?'active':''} onClick={()=>setTab('save')}><Bookmark size={16}/>收藏</button><button className={tab==='history'?'active':''} onClick={()=>setTab('history')}><History size={16}/>浏览记录</button></div>:<div className="browse-filters"><label className="search-input"><Search size={16}/><input value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜索标题、摘要或作者" aria-label="搜索论文"/></label><BrowseDateFilters basis={dateBasis} range={range} year={dateYear} month={dateMonth} onBasis={setDateBasis} onRange={setRange} onYear={setDateYear} onMonth={setDateMonth}/><select aria-label="排序方式" value={sort} onChange={e=>setSort(e.target.value)}><option value="score">推荐分</option><option value="date">{dateBasis==='ingested'?'最近收录':'日期从新到旧'}</option></select></div>}{!library&&<TreeDrawer title="分类与主题" label={treeLabel}>{tree}</TreeDrawer>}<div ref={viewportPanel} className={library?'':'browse-layout viewport-panel'}>{!library&&<aside className="browse-tree panel desktop-topic-tree">{tree}</aside>}{library&&tab==='history'?<BrowsingHistory onRead={read}/>:<div className="paper-list">{result.error?<ErrorBox error={result.error} reload={result.reload}/>:result.loading?<Loading/>:result.data?.items.length?result.data.items.map((p,i)=><div className={i===selected?'paper-list-item selected-item':'paper-list-item'} key={p.id} onMouseEnter={()=>setSelected(i)}><PaperCard paper={p} compact onRead={read} onFeedback={interaction}/></div>):<Empty title={library?'书架等您来填':'还没有符合条件的论文'} text={library?'在论文卡片上点击喜欢或收藏，即可留在这里。':'换一个时间范围或研究主题试试。'}/>} {!library&&result.data?.total?<div className="pagination"><button disabled={offset===0} onClick={()=>setOffset(o=>Math.max(0,o-30))}>上一页</button><span>{offset+1}–{Math.min(offset+30,result.data.total)} / {result.data.total}</span><button disabled={offset+30>=result.data.total} onClick={()=>setOffset(o=>o+30)}>下一页</button></div>:null}</div>}</div>{reading&&<ReadingModal paper={reading} onClose={()=>setReading(null)}/>}</>
}

