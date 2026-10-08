import {useEffect,useState} from 'react'
import {ChartNoAxesCombined,ChevronDown,Search,X,Heart,Bookmark,Ban,BookOpen} from 'lucide-react'
import {ResponsiveContainer,LineChart,Line,XAxis,YAxis,Tooltip,CartesianGrid,Legend} from 'recharts'
import {useLoad} from '../hooks/useLoad'
import {Loading,ErrorBox} from './Common'
import {IconTile} from './PageContentUI'
import '../task-center.css'

type Metrics={date:string;shown:number;skipped:number;liked:number;saved:number;expanded:number;like_rate:number;save_rate:number;quick_skip_rate:number;expand_rate:number}
type User={id:number;username:string}
const series=[{key:'like_rate',name:'喜欢率',color:'#3568ff',icon:Heart,tone:'red'},{key:'save_rate',name:'收藏率',color:'#18a66a',icon:Bookmark,tone:'amber'},{key:'quick_skip_rate',name:'快速不感兴趣率',color:'#f69046',icon:Ban,tone:'red'},{key:'expand_rate',name:'精读展开率',color:'#9362e7',icon:BookOpen,tone:'purple'}] as const
export function DailyReadingMetrics(){
 const [user,setUser]=useState<User|null>(null),[open,setOpen]=useState(false),[query,setQuery]=useState(''),[search,setSearch]=useState('')
 const metrics=useLoad<Metrics[]>('/admin/metrics/daily?days=30'+(user?'&user_id='+user.id:''))
 const users=useLoad<User[]>(open?'/admin/task-center/metric-users?query='+encodeURIComponent(search):null)
 useEffect(()=>{const timer=setTimeout(()=>setSearch(query),300);return()=>clearTimeout(timer)},[query])
 useEffect(()=>{const timer=setInterval(()=>{if(document.visibilityState==='visible')metrics.reload()},60000);return()=>clearInterval(timer)},[metrics.reload])
 const choose=(value:User|null)=>{setUser(value);setOpen(false);setQuery('');setSearch('')}
 return <section className="panel overview-reading-metrics">
  <div className="reading-metrics-header"><IconTile icon={ChartNoAxesCombined}/><div><h2>每日阅读指标</h2><p className="muted">近 30 天 · {user?user.username:'全部用户'}</p></div><div className="reading-user-filter" onBlur={event=>{if(!event.currentTarget.contains(event.relatedTarget as Node|null))setOpen(false)}}><button aria-expanded={open} aria-label="筛选阅读指标用户" onClick={()=>setOpen(value=>!value)}>{user?user.username:'全部用户'}<ChevronDown size={15}/></button>{open&&<div className="reading-user-options"><div className="reading-user-search"><Search size={15}/><input autoFocus placeholder="搜索用户名" aria-label="搜索指标用户" value={query} onChange={e=>setQuery(e.target.value)}/><button className="icon-button" aria-label="关闭用户筛选" onClick={()=>setOpen(false)}><X size={14}/></button></div><button onClick={()=>choose(null)}>全部用户</button>{users.error?<p className="error-text">{users.error}</p>:users.loading&&!users.data?<Loading/>:users.data?.map(value=><button key={value.id} onClick={()=>choose(value)}>{value.username}</button>)}{users.data&&!users.data.length&&<p className="muted">没有匹配用户</p>}<small>最多显示 30 位用户，可输入用户名查找。</small></div>}</div></div>
  {metrics.error?<ErrorBox error={metrics.error} reload={metrics.reload}/>:!metrics.data?<Loading/>:!metrics.data.length?<p className="overview-empty">这一时期暂无已汇总的阅读记录，可在任务中心运行“每日指标汇总”。</p>:<><div className="reading-metrics-summaries">{series.map(item=><div key={item.key}><IconTile icon={item.icon} tone={item.tone}/><div><span>{item.name}</span><strong>{((metrics.data?.at(-1)?.[item.key]||0)*100).toFixed(1)}%</strong><small>最近有记录的一天</small></div></div>)}</div><div className="reading-metrics-chart"><ResponsiveContainer width="100%" height="100%"><LineChart data={metrics.data} margin={{right:12,left:0,top:12,bottom:4}}><CartesianGrid vertical={false} stroke="var(--border)"/><XAxis dataKey="date" tickFormatter={value=>value.slice(5)} tick={{fontSize:12,fill:'var(--secondary)'}} minTickGap={22} axisLine={false} tickLine={false}/><YAxis domain={[0,'auto']} width={46} axisLine={false} tickLine={false} tickFormatter={value=>Math.round(value*100)+'%'} tick={{fontSize:12,fill:'var(--secondary)'}}/><Tooltip formatter={value=>(Number(value||0)*100).toFixed(1)+'%'}/><Legend iconType="circle" iconSize={7}/>{series.map(item=><Line key={item.key} type="monotone" dataKey={item.key} name={item.name} stroke={item.color} strokeWidth={2} dot={{r:2,strokeWidth:0}} activeDot={{r:4}} isAnimationActive={false}/>)}</LineChart></ResponsiveContainer></div><p className="hint">以停留达到阅读阈值的记录计为已刷；指标在汇总任务完成后更新。</p></>}
 </section>
}
