import {CardSurface} from './CardUI'
import {useEffect} from 'react'
import {NavLink} from 'react-router-dom'
import {TrendingUp,ArrowUpRight} from 'lucide-react'
import {useApp} from '../context'
import {useLoad} from '../hooks/useLoad'
import {TrendContent} from './TrendContent'
import './daily-trends.css'
export type DirectionItem={direction:string;kind?:'daily_overview'|'direction';status:'ready'|'insufficient'|'error';summary:string;short_summary:string;matched_count?:number;sampled_count?:number}
type Summary={items:DirectionItem[];status:string;created_at:string|null;period:{from:string|null;through:string|null}}
export function InterestTrends(){
 const {auth,requireLogin}=useApp(),result=useLoad<Summary>(auth?'/trends/summary':null)
 useEffect(()=>{const update=()=>result.reload();window.addEventListener('profile-change',update);return()=>window.removeEventListener('profile-change',update)},[result.reload])
 useEffect(()=>{if(!auth)return;const timer=setInterval(result.reload,['pending','updating'].includes(result.data?.status||'')?5000:60000);return()=>clearInterval(timer)},[auth,result.data?.status,result.reload])
 const overview=result.data?.items.find(item=>item.kind==='daily_overview')
 const placeholder=result.error||result.data?.status==='error'?'每日趋势暂时无法生成。':result.data?.status==='empty'?'设置兴趣方向后，查看每日研究进展。':'等待后台整理最新一期 arXiv 新稿…'
 return <CardSurface className="panel interest-trends daily-trends"><div className="section-heading"><h3><TrendingUp size={16}/>每日趋势</h3></div>{auth?<>{result.data?.period.through&&<small className="hint daily-trends-date">arXiv 公告日 {result.data.period.through}</small>}{overview?<div className="direction-short daily-overview"><TrendContent text={overview.short_summary} compact/></div>:<p>{placeholder}</p>}<NavLink className="trend-more" to="/trends">查看趋势分析 <ArrowUpRight size={14}/></NavLink>{result.data?.status==='updating'&&<small>正在更新…</small>}</>:<button className="trend-login" onClick={()=>requireLogin('查看每日趋势')}>登录后查看每日趋势</button>}</CardSurface>
}
