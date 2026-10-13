import {useEffect,useRef,useState} from 'react'
import {ArrowUpRight,ChevronLeft,ChevronRight,FileText,Users} from 'lucide-react'
import {useLoad} from '../hooks/useLoad'
import {ContentCard,ContentHeading} from './PageContentUI'
import {ErrorBox,Loading} from './Common'
import {MathText} from './MathText'
import {Pagination} from './ui/Pagination'
import './author-movements.css'

type Movement={id:number;title:string;authors:string[];published:string;primary_category:string;venue:string|null}
type Page={items:Movement[];total:number;offset:number;limit:number}
const PAGE_SIZE=5

export function AuthorMovements({onOpen,refreshKey}:{onOpen:(id:number)=>void;refreshKey:unknown}){
 const [page,setPage]=useState(1),lastFetch=useRef(Date.now())
 const offset=(page-1)*PAGE_SIZE,result=useLoad<Page>(`/trends/author-movements?offset=${offset}&limit=${PAGE_SIZE}`)
 const data=result.data?.offset===offset?result.data:null,total=result.data?.total||0,pages=Math.max(1,Math.ceil(total/PAGE_SIZE))
 const loading=result.loading||!data&&!result.error
 useEffect(()=>{lastFetch.current=Date.now()},[result.data])
 // Share the trend page's existing refresh cycle without an additional timer.
 useEffect(()=>{if(Date.now()-lastFetch.current>=60000){lastFetch.current=Date.now();result.reload()}},[refreshKey,result.reload])
 useEffect(()=>{if(!result.loading&&data&&page>pages)setPage(pages)},[result.loading,data,page,pages])
 return <ContentCard className="panel author-movements author-updates"><ContentHeading icon={Users} title="收藏作者的新动向 · 近 7 天" level="h3"/>
  <div className={'author-updates-body'+(loading?' is-loading':'')} aria-busy={loading}>
   {loading?<Loading/>:result.error?<ErrorBox error={result.error} reload={result.reload}/>:data?.items.length?<div className="author-update-list">{data.items.map(paper=><button type="button" className="author-update-row" key={paper.id} onClick={()=>onOpen(paper.id)}><FileText className="author-update-file" size={20} aria-hidden="true"/><span className="author-update-copy"><strong><MathText inline>{paper.title}</MathText></strong><span className="author-update-meta"><span title={paper.authors.join(' · ')}>{paper.authors.join(' · ')}</span>{(paper.venue||paper.primary_category)&&<small>{paper.venue?.split('.')[0]||paper.primary_category}</small>}</span></span><span className="author-update-end"><time dateTime={paper.published}>{paper.published?.slice(0,10)}</time><ArrowUpRight size={16} aria-hidden="true"/></span></button>)}</div>:<p className="author-update-empty">收藏作者最近 7 天有新论文时，会出现在这里。</p>}
  </div>
  {total>0&&<fieldset className="author-update-pagination" disabled={loading}>
   <div className="author-pagination-desktop"><Pagination page={page} pages={pages} total={total} pageSize={PAGE_SIZE} label="作者动态" onChange={setPage}/></div>
   <nav className="author-pagination-mobile" aria-label="作者动态列表分页"><button type="button" aria-label="上一页作者动态" disabled={page<=1} onClick={()=>setPage(old=>old-1)}><ChevronLeft size={14}/>上一页</button><span>第 {page} / {pages} 页</span><button type="button" aria-label="下一页作者动态" disabled={page>=pages} onClick={()=>setPage(old=>old+1)}>下一页<ChevronRight size={14}/></button></nav>
  </fieldset>}
 </ContentCard>
}
