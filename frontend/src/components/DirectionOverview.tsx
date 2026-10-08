import {useId,useRef,useState,type KeyboardEvent} from 'react'
import {TrendingUp} from 'lucide-react'
import type {DirectionItem} from './InterestTrends'
import {ContentCard,IconTile} from './PageContentUI'
import {TrendContent} from './TrendContent'
import {formatTime} from './Common'
import './direction-overview.css'

type Report={items:DirectionItem[];period:{from:string;through:string};created_at:string}

/** Tabs select existing report content only; switching never starts a request. */
export function DirectionOverview({report,status}:{report:Report|null|undefined;status?:string}){
 const [direction,setDirection]=useState(''),id=useId(),body=useRef<HTMLDivElement>(null)
 const items=(report?.items||[]).filter(item=>item.kind!=='daily_overview'),index=Math.max(0,items.findIndex(item=>item.direction===direction)),active=items[index]
 const select=(next:number)=>{setDirection(items[next].direction);if(body.current)body.current.scrollTop=0}
 const navigate=(event:KeyboardEvent<HTMLButtonElement>,current:number)=>{
  const next=event.key==='ArrowRight'?(current+1)%items.length:event.key==='ArrowLeft'?(current-1+items.length)%items.length:event.key==='Home'?0:event.key==='End'?items.length-1:null
  if(next===null)return
  event.preventDefault();select(next)
  event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role=tab]')[next]?.focus()
 }
 return <ContentCard className="panel interpretation direction-overview">
  <div className="direction-report-header"><div className="direction-report-identity"><IconTile icon={TrendingUp}/><div><p className="eyebrow">RESEARCH DIRECTIONS</p><h2>趋势分析</h2></div></div>{report&&<p className="direction-period">arXiv 公告日 {report.period.through}</p>}</div>
  {report?<>
   {items.length>0&&<>
    <div className="direction-tabs" role="tablist" aria-label="研究方向">{items.map((item,i)=><button type="button" role="tab" id={id+'-tab-'+i} aria-controls={id+'-panel'} aria-selected={i===index} tabIndex={i===index?0:-1} title={item.direction} key={item.direction} onClick={()=>select(i)} onKeyDown={event=>navigate(event,i)}>{item.direction.replace(/\s*[（(].*$/,'').trim()||item.direction}</button>)}</div>
    <div className="direction-scroll" ref={body} role="tabpanel" id={id+'-panel'} aria-labelledby={id+'-tab-'+index} tabIndex={0}>
     <article className="direction-full" key={active.direction}><TrendContent text={active.summary} analysis/></article>
    </div>
   </>}
   <small className="muted direction-updated">更新于 {formatTime(report.created_at)}{status==='updating'?' · 正在更新':''}</small>
  </>:<p className="muted direction-report-empty">{status==='empty'?'请先在设置中填写您感兴趣的方向。':'等待后台整理最新一期 arXiv 新稿的研究分析…'}</p>}
 </ContentCard>
}
