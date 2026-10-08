import {Fragment,useEffect,useState} from 'react'
import {Link,useLocation} from 'react-router-dom'
import {ChevronDown,ChevronRight,Info,RefreshCw,Search,ScrollText} from 'lucide-react'
import {useLoad} from '../hooks/useLoad'
import {ErrorBox,Loading,formatTime} from './Common'
import {Pagination} from './ui/Pagination'
import {IconTile} from './PageContentUI'
import {taskNames} from './taskNames'
import '../admin-logs.css'

type Entry={id:number;kind:string;level:string;job:string|null;model:string|null;message:string;detail:Record<string,unknown>;user_id:number|null;created_at:string}
const kinds:Record<string,string>={task:'任务',model:'模型调用',source:'抓取',admin:'管理员操作',system:'系统',topic:'主题'}
const levels:Record<string,string>={info:'正常',warning:'提醒',error:'错误'}
const PAGE_SIZE=20
type LogResult={items:Entry[];total:number;policy:{retention_days:number;max_entries:number}}
export function AdminLogs(){
 const [kind,setKind]=useState(''),[level,setLevel]=useState(''),[query,setQuery]=useState(''),[search,setSearch]=useState(''),[offset,setOffset]=useState(0)
 const [since,setSince]=useState(''),[until,setUntil]=useState('')
 const [expanded,setExpanded]=useState<number[]>([]),location=useLocation()
 const result=useLoad<LogResult>(`/admin/logs?kind=${kind}&level=${level}&query=${encodeURIComponent(search)}&offset=${offset}&limit=${PAGE_SIZE}${since?'&since='+since:''}${until?'&until='+until:''}`)
 useEffect(()=>setExpanded([]),[offset,kind,level,search,since,until])
 const total=result.data?.total||0,page=Math.floor(offset/PAGE_SIZE)+1,pages=Math.max(1,Math.ceil(total/PAGE_SIZE))
 useEffect(()=>{if(result.data&&offset>0&&offset>=result.data.total)setOffset(Math.max(0,(Math.ceil(result.data.total/PAGE_SIZE)-1)*PAGE_SIZE))},[result.data,offset])
 const reset=()=>{setKind('');setLevel('');setQuery('');setSearch('');setSince('');setUntil('');setOffset(0)}
 return <section className="admin-logs" aria-busy={result.loading}>
  <div className="logs-toolbar"><p>查看任务、模型调用与管理操作记录</p><button type="button" disabled={result.loading} onClick={result.reload}><RefreshCw size={15}/>刷新</button></div>
  <div className="logs-policy"><Info size={16}/><span>{result.data?.policy?`保留策略：${result.data.policy.retention_days} 天 / 最多 ${result.data.policy.max_entries.toLocaleString('zh-CN')} 条`:'正在读取保留策略…'}</span><Link to={location.pathname.replace(/\/logs\/?$/,'/settings')}>前往日志设置<ChevronRight size={14}/></Link></div>
  <form className="panel logs-filter-card" onSubmit={e=>{e.preventDefault();setSearch(query.trim());setOffset(0)}}>
   <div className="logs-filter-fields"><label>日志类型<select aria-label="日志类型" value={kind} onChange={e=>{setKind(e.target.value);setOffset(0)}}><option value="">全部类型</option>{Object.entries(kinds).map(([k,v])=><option value={k} key={k}>{v}</option>)}</select></label><label>日志级别<select aria-label="日志级别" value={level} onChange={e=>{setLevel(e.target.value);setOffset(0)}}><option value="">全部级别</option>{Object.entries(levels).map(([k,v])=><option value={k} key={k}>{v}</option>)}</select></label><label>开始日期<input type="date" value={since} max={until||undefined} onChange={e=>{setSince(e.target.value);setOffset(0)}}/></label><label>结束日期<input type="date" value={until} min={since} onChange={e=>{setUntil(e.target.value);setOffset(0)}}/></label></div>
   <div className="logs-search-row"><label className="logs-search"><Search size={17}/><input aria-label="搜索日志" placeholder="搜索任务、模型或错误" maxLength={100} value={query} onChange={e=>setQuery(e.target.value)}/></label><button className="primary" type="submit">搜索</button><button type="button" onClick={reset}>重置</button></div>
  </form>
  {result.error?<ErrorBox error={result.error} reload={result.reload}/>:result.loading&&!result.data?<Loading/>:<section className="panel logs-records"><header><IconTile icon={ScrollText}/><h2>运行记录</h2><span>筛选结果 {total.toLocaleString('zh-CN')} 条</span></header>
   <div className="logs-table-wrap"><table className="logs-table"><thead><tr><th>时间</th><th>类型</th><th>级别</th><th>内容</th><th>详情</th></tr></thead><tbody>{result.data?.items.map(r=>{
    const open=expanded.includes(r.id),type=kinds[r.kind]||r.kind,time=formatTime(r.created_at),source=[type,r.job&&(taskNames[r.job]||r.job),r.model].filter(Boolean).join(' · ')
    return <Fragment key={r.id}><tr className="logs-record-row"><td data-label="时间"><time title={time}><span className="logs-full-time">{time}</span><span className="logs-short-time">{time.split(' ').at(-1)}</span></time></td><td data-label="类型"><div className="logs-source" title={source}>{source}</div></td><td data-label="级别"><span className={'logs-level '+r.level}>{levels[r.level]||r.level}</span></td><td className="logs-message" title={r.message}>{r.message}</td><td><button type="button" className="logs-details-button" aria-expanded={open} aria-controls={'log-details-'+r.id} aria-label={(open?'收起':'展开')+'日志详情 '+r.id} onClick={()=>setExpanded(ids=>open?ids.filter(id=>id!==r.id):[...ids,r.id])}>{open?<ChevronDown size={14}/>:<ChevronRight size={14}/>}<span>详情</span></button></td></tr>{open&&<tr className="logs-detail-row" id={'log-details-'+r.id}><td colSpan={5}><div className="logs-detail-panel"><dl><dt>时间</dt><dd>{time}</dd><dt>类型</dt><dd>{type}</dd>{r.job&&<><dt>任务</dt><dd>{taskNames[r.job]||r.job}</dd></>}{r.model&&<><dt>模型</dt><dd>{r.model}</dd></>}<dt>内容</dt><dd className="logs-full-message">{r.message}</dd>{r.user_id!=null&&<><dt>操作用户</dt><dd>#{r.user_id}</dd></>}{Object.entries(r.detail).map(([key,value])=><Fragment key={key}><dt>{key}</dt><dd>{typeof value==='object'?<pre>{JSON.stringify(value,null,2)}</pre>:String(value)}</dd></Fragment>)}</dl></div></td></tr>}</Fragment>
   })}</tbody></table></div>
   {!result.data?.items.length&&<div className="logs-empty"><ScrollText size={29}/><p>{kind||level||search||since||until?'没有符合筛选条件的日志。':'暂无日志，新的运行记录会显示在这里。'}</p></div>}
   <Pagination label="日志" pageSize={PAGE_SIZE} page={Math.min(page,pages)} pages={pages} total={total} onChange={p=>setOffset((p-1)*PAGE_SIZE)}/>
  </section>}
 </section>
}
