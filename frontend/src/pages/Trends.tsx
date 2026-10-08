import type {DirectionItem} from '../components/InterestTrends'
import {MathText} from '../components/MathText'
import {DirectionOverview} from '../components/DirectionOverview'
import {useState,useEffect} from 'react'
import {TrendingUp,Users,ArrowUpRight} from 'lucide-react'
import {useLoad} from '../hooks/useLoad'
import {api} from '../api'
import {useApp} from '../context'
import {PageTitle,Loading,ErrorBox} from '../components/Common'
import {ReadingModal} from '../components/ReadingModal'
import {ContentCard,ContentHeading,TrendSignalGraphic} from '../components/PageContentUI'
import '../components/daily-trends.css'
import type {Paper} from '../types'
interface Trend {id:number;name_zh:string;name_en:string;count:number;share:number}
type Coverage={announcement_date:string|null;batch_count:number;matched_count:number;classified_count:number;pending_count:number;awaiting_approval_count:number;classification_coverage:number}
export function Trends() {
 const [reading,setReading]=useState<Paper|null>(null);const {toast}=useApp();const result=useLoad<{topics:Trend[];status:string;coverage:Coverage;report:{content:string;created_at:string;items:DirectionItem[];period:{from:string;through:string};papers:{id:number|string;title:string;published:string;url?:string;historical?:boolean}[]}|null;movements:{id:number;title:string;authors:string[];published:string}[]}>('/trends')
 useEffect(()=>{const timer=setInterval(result.reload,['pending','updating'].includes(result.data?.status||'')?5000:60000);return()=>clearInterval(timer)},[result.data?.status,result.reload])
 useEffect(()=>{const update=()=>result.reload();globalThis.window.addEventListener('profile-change',update);return()=>globalThis.window.removeEventListener('profile-change',update)},[result.reload])
 const open=async(id:number)=>{try{setReading(await api<Paper>('/papers/'+id))}catch(e){toast((e as Error).message)}}
 return <><PageTitle eyebrow="SIGNALS IN THE NOISE" title="趋势和热点" description="分析每日 arXiv 研究热点与进展。"/>
 <div className="trends-content">{result.error?<ErrorBox error={result.error} reload={result.reload}/>:result.loading?<Loading/>:<>
  <h3 className="section-label"><TrendingUp size={17}/>当日热点</h3>
  {!!result.data&&(result.data.coverage.pending_count>0||result.data.coverage.awaiting_approval_count>0)&&<p className="hotspot-status" role="status">主题统计尚未完整，以下为已确认主题中的当日热点。</p>}
  {result.data?.topics.length?<div className="trend-grid daily-hotspots">{result.data.topics.map(t=><ContentCard as="div" className="panel trend-card daily-hotspot" key={t.id}>
   <div className="hotspot-name"><h3 title={t.name_zh}>{t.name_zh}</h3></div>
   <span className="hotspot-count"><strong>{t.count}</strong><span>篇</span></span>
  </ContentCard>)}</div>:<div className="trend-empty"><TrendSignalGraphic/><h3>本期暂无可展示的热点</h3><p>同一主题至少有 3 篇新稿才展示；分类和主题批准完成后，统计会自动更新。</p></div>}
  <DirectionOverview report={result.data?.report} status={result.data?.status}/>
  <ContentCard className="panel author-movements"><ContentHeading icon={Users} title="收藏作者的新动向 · 近 7 天" level="h3"/>{result.data?.movements.length?<div className="movement-list">{result.data.movements.map(p=><button key={p.id} onClick={()=>open(p.id)}><div><small>{p.authors.join(' · ')} · {p.published}</small><p><MathText inline>{p.title}</MathText></p></div><ArrowUpRight size={18}/></button>)}</div>:<p className="muted">收藏作者最近 7 天有新论文时，会出现在这里。</p>}</ContentCard>
 </>}</div>{reading&&<ReadingModal paper={reading} onClose={()=>setReading(null)}/>}</>

}
