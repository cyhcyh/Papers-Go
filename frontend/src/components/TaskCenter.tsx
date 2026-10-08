import {useEffect,useState} from 'react'
import {Clock,Settings2,Play,Square,Database,Trophy,Users,Tag,Network,ShieldCheck,FileText,List,ChartNoAxesCombined,Bell,Layers,ScanSearch,TrendingUp,ChartPie,UserRound,Trash2,Workflow,Activity,CirclePause,TriangleAlert,type LucideIcon} from 'lucide-react'
import {api} from '../api'
import {useLoad} from '../hooks/useLoad'
import {useApp} from '../context'
import {Loading,ErrorBox,formatTime} from './Common'
import {RedoTask,type RedoRun} from './RedoTask'
import {JobProgress,type TaskProgressData} from './JobProgress'
import {ContentCard,IconTile,StatusBadge} from './PageContentUI'
import {taskNames,pipelineTasks,independentTasks} from './taskNames'
import {TaskScheduleDialog,TaskAdvancedDialog,scheduleText,type TaskConfiguration} from './TaskSettings'
import '../task-center.css'

type TaskState={busy:boolean;stopping:boolean;stopping_pipeline?:boolean;stopping_names?:string[];active:string[];queued:string[];pipeline:boolean;worker_available?:boolean;execution?:string}
type AuthorContinuation={phase:'running'|'waiting'|'retry'|'failed'|'complete'|'stopped'|'disabled';processed:number;pending:number;next_run:string|null}
type Source={redo?:RedoRun|null;enabled:boolean;progress?:TaskProgressData;continuation?:AuthorContinuation|null;name:string;running:number;queued?:boolean;stopped?:boolean;error:string|null;last_success:string|null;added:number}
const icons:Record<string,LucideIcon>={fetch_arxiv:Database,fetch_conf:Trophy,fetch_community:Users,classify:Tag,build_vectors:Network,assess_quality:ShieldCheck,tldr_gen:FileText,preread:List,trend_stats:ChartNoAxesCombined,alert_eval:Bell,metrics:Layers,reflect:ScanSearch,trend_report:TrendingUp,audit:ChartPie,author_impact:UserRound,paper_expiry:Trash2}
const iconTones:Record<string,'blue'|'purple'|'green'|'amber'|'red'>={fetch_arxiv:'purple',fetch_conf:'blue',fetch_community:'amber',classify:'green',build_vectors:'purple',assess_quality:'blue',tldr_gen:'purple',preread:'amber',trend_stats:'green',alert_eval:'red',metrics:'blue',reflect:'green',trend_report:'blue',audit:'purple',author_impact:'blue',paper_expiry:'purple'}
const descriptions:Record<string,string>={fetch_arxiv:'按分类同步新增及更新论文，按 ID 与版本去重。',fetch_conf:'读取各会议最新一届论文，保留真实标题和会议标签。',fetch_community:'更新 Hugging Face 点赞与 GitHub 仓库 stars，复用缓存。',classify:'为论文选择唯一研究主题，新方向提议需管理员批准。',build_vectors:'生成论文和用户兴趣向量，用于语义检索与推荐。',assess_quality:'结合研究贡献、会议、作者和社区信号计算参考质量。',tldr_gen:'生成中文译名、研究问题、主要贡献和结果，共享缓存。',preread:'为全库质量分最高的 5 篇预生成精读卡，已有卡片复用。',trend_stats:'按主题与日期汇总论文数量及平均质量，不调用模型。',alert_eval:'增量检查监视项与研究方向，生成通知。',metrics:'汇总浏览、喜欢、收藏、跳过和展开等每日指标。',reflect:'根据最近 7 天的行为调整兴趣画像，无新行为则跳过。',trend_report:'为近期活跃用户生成方向趋势简述，复用已有缓存。',audit:'抽查主题分类并记录结果，按既有规则修正未归类论文。',author_impact:'从 OpenAlex 确认作者，缓存跨学科高被引数量，供质量分与推荐分共用。',paper_expiry:'分批清退到期论文及关联数据，正在生成或排队的论文暂缓清退。'}
const advancedTasks=['fetch_community','audit','author_impact','paper_expiry']

export function TaskCenter(){
 const {toast}=useApp(),[busy,setBusy]=useState(false),[dialog,setDialog]=useState<string|null>(null)
 const sources=useLoad<Source[]>('/admin/sources'),state=useLoad<TaskState>('/admin/jobs'),config=useLoad<TaskConfiguration>('/admin/task-center')
 const refresh=()=>{sources.reload();state.reload()}
 const active=!!state.data?.busy||!!sources.data?.some(source=>source.running||source.queued)
 const continuing=!!sources.data?.some(source=>source.continuation&&['waiting','retry'].includes(source.continuation.phase))
 useEffect(()=>{const reload=()=>{if(document.visibilityState==='visible'){sources.reload();state.reload()}};const timer=setInterval(reload,active||continuing?3000:15000);document.addEventListener('visibilitychange',reload);return()=>{clearInterval(timer);document.removeEventListener('visibilitychange',reload)}},[active,continuing,sources.reload,state.reload])
 useEffect(()=>{const timer=setInterval(()=>{if(document.visibilityState==='visible')config.reload()},60000);return()=>clearInterval(timer)},[config.reload])
 const run=async(work:()=>Promise<unknown>,message:string)=>{setBusy(true);try{await work();refresh();if(message)toast(message)}catch(e){toast((e as Error).message)}finally{setBusy(false)}}
 const stop=(name:string)=>run(async()=>{const result=await api<TaskState>('/admin/jobs/'+name+'/stop','POST');toast(result.stopping?'正在停止任务':'任务已停止')},'')
 const saved=(value:TaskConfiguration)=>{config.setData(value);toast('设置已保存')}
 const card=(source:Source,index?:number)=>{
  const stopping=state.data?.stopping_names?.includes(source.name),schedule=config.data?.schedules[source.name]
  const continuation=source.continuation,waiting=!!continuation&&['waiting','retry'].includes(continuation.phase)
  const authorStatus=continuation?({waiting:'等待续跑',retry:'等待重试',failed:'失败暂停',complete:'本轮完成',stopped:'已停止'} as Record<string,string>)[continuation.phase]:undefined
  const status=stopping?'正在停止':source.running?'运行中':!source.enabled?'已禁用':source.queued?'等待执行':authorStatus|| (source.stopped?'已停止':source.error?'需要检查':source.last_success?'运行正常':'尚未运行')
  const tone=stopping?'amber':source.running?'green':!source.enabled?'neutral':source.queued||waiting?'amber':source.stopped||continuation?.phase==='stopped'?'neutral':source.error?'red':source.last_success||continuation?.phase==='complete'?'green':'amber'
  const redo=['classify','build_vectors','assess_quality','tldr_gen'].includes(source.name)
  return <ContentCard as="article" className={'task-center-card'+(!source.enabled?' task-is-disabled':'')} key={source.name}>
   <span className={'task-run-status '+tone}><i aria-hidden="true"/>{status}</span>
   <div className="task-center-card-header">{index!==undefined&&<span className="task-stage-index" aria-label={'第 '+(index+1)+' 步'}>{index+1}</span>}<IconTile icon={icons[source.name]||Workflow} tone={iconTones[source.name]||'blue'}/><div className="task-center-card-identity"><h3>{taskNames[source.name]}</h3><StatusBadge tone={source.enabled?'green':'neutral'}>{source.enabled?'已启用':'已禁用'}</StatusBadge></div></div>
   <p className="task-center-description">{descriptions[source.name]}</p>
   {index===undefined&&<p className="task-center-schedule"><Clock size={14}/>{scheduleText(schedule)}</p>}
   {schedule?.enabled&&<small className="task-next-run">下次触发：{formatTime(schedule.next_run)}</small>}
   <div className="task-center-runtime"><small className="task-last-run">最近成功：{formatTime(source.last_success)}</small></div>
   {!['fetch_arxiv','fetch_conf','alert_eval','build_vectors','assess_quality','author_impact'].includes(source.name)&&source.added>0&&<p className="task-center-count">{source.name==='paper_expiry'?'已清退':'本轮处理'} {source.added} 篇</p>}
   {continuation&&<p className="fetch-count">已处理 {continuation.processed} 篇 · 待处理 {continuation.pending} 篇</p>}
   {continuation?.phase==='retry'&&<small className="task-next-run">自动重试：{formatTime(continuation.next_run)}</small>}
   {continuation?.phase==='failed'&&<small className="task-next-run">进度已保存，下次计划任务继续。</small>}
   {source.error&&!source.stopped&&<p className="error-text">{source.error}</p>}
   {['fetch_arxiv','fetch_conf','alert_eval'].includes(source.name)&&source.progress?.processed!==undefined&&<p className="fetch-count">已处理 {source.progress.processed} 篇</p>}
   {source.progress&&(source.progress.total>0||source.progress.stages)&&<JobProgress progress={source.progress} running={!!source.running}/>}
   <div className="task-center-card-footer"><div className="task-center-buttons"><button className="task-toggle-button" aria-pressed={source.enabled} disabled={busy} onClick={()=>run(()=>api('/admin/jobs/'+source.name,'PATCH',{enabled:!source.enabled}),source.enabled?'任务已禁用':'任务已启用')}>{source.enabled?'禁用':'启用'}</button><button className="task-run-button" disabled={busy||active||!source.enabled||state.data?.worker_available===false} onClick={()=>run(()=>api('/admin/jobs/'+source.name,'POST'),'任务已加入队列')}><Play size={13}/>现在运行</button><button className="task-stop-button" disabled={busy||(!source.running&&!source.queued&&!waiting)||stopping} aria-label={'停止'+taskNames[source.name]} onClick={()=>stop(source.name)}><Square size={13}/>{source.queued?'取消排队':'停止'}</button></div>
    {advancedTasks.includes(source.name)&&<button className="task-advanced-button" disabled={!config.data||busy} onClick={()=>setDialog(source.name)}><Settings2 size={14}/>高级设置</button>}
    {redo&&<RedoTask name={source.name} label={taskNames[source.name]} disabled={busy||active||!source.enabled||state.data?.worker_available===false} latest={source.redo} onStarted={refresh}/>}
   </div>
  </ContentCard>
 }
 return <div className="task-center">
  <div className="task-center-toolbar"><h1 className="admin-page-title">任务中心</h1><div className="task-center-toolbar-actions"><button disabled={!config.data} onClick={()=>setDialog('schedule')}><Clock size={16}/>计划任务设置</button><button className="task-toolbar-stop" disabled={busy||(!active&&!continuing)||state.data?.stopping_pipeline} onClick={()=>stop('pipeline')}><Square size={14}/>{state.data?.stopping_pipeline?'正在停止…':'停止当前任务'}</button><button className="primary" disabled={busy||active||state.data?.worker_available===false} onClick={()=>run(()=>api('/admin/jobs/pipeline','POST'),'流水线已加入队列')}><Play size={14}/>运行完整流水线</button></div></div>
  <ContentCard className="task-plan-banner"><IconTile icon={Clock}/><div><strong>完整流水线 <span>· {scheduleText(config.data?.schedules.pipeline)}</span></strong><p>时区：{config.data?.timezone||'加载中'}{config.data?.schedules.pipeline.enabled?' · 下次触发：'+formatTime(config.data.schedules.pipeline.next_run):''}</p></div><button disabled={!config.data} onClick={()=>setDialog('schedule')}>修改计划</button></ContentCard>
  {config.error&&<ErrorBox error={config.error} reload={config.reload}/>}{state.error&&<ErrorBox error={state.error} reload={state.reload}/>}{state.data?.worker_available===false&&<p className="error-text">后台工作进程尚未就绪，请稍后刷新。</p>}
  {sources.data&&<div className="task-center-summary">{([{label:'全部任务',value:sources.data.length,icon:Layers,tone:'blue'},{label:'运行中',value:sources.data.filter(source=>source.running).length,icon:Activity,tone:'green'},{label:'已禁用',value:sources.data.filter(source=>!source.enabled).length,icon:CirclePause,tone:'purple'},{label:'异常任务',value:sources.data.filter(source=>source.error&&!source.stopped).length,icon:TriangleAlert,tone:'red'}] as const).map(item=><ContentCard key={item.label} className={'task-summary-'+item.tone}><IconTile icon={item.icon} tone={item.tone}/><div><small>{item.label}</small><strong>{item.value}</strong></div></ContentCard>)}</div>}
  {sources.error?<ErrorBox error={sources.error} reload={sources.reload}/>:!sources.data?<Loading/>:<>
   <section className="task-center-group"><header><IconTile icon={Workflow}/><h2>流水线任务</h2><span>按顺序执行 · 共 {pipelineTasks.length} 项</span></header><div className="task-center-grid">{pipelineTasks.map(name=>sources.data?.find(source=>source.name===name)).filter((source):source is Source=>!!source).map((source,index)=>card(source,index))}</div></section>
   <section className="task-center-group"><header><IconTile icon={Clock} tone="green"/><h2>独立任务</h2><span>按各自计划执行 · 共 {independentTasks.length} 项</span></header><div className="task-center-grid">{independentTasks.map(name=>sources.data?.find(source=>source.name===name)).filter((source):source is Source=>!!source).map(source=>card(source))}</div></section>
  </>}
  {dialog&&config.data&&(dialog==='schedule'?<TaskScheduleDialog config={config.data} onSaved={saved} onClose={()=>setDialog(null)}/>:<TaskAdvancedDialog key={dialog} name={dialog} config={config.data} onSaved={saved} onClose={()=>setDialog(null)}/>)}
 </div>
}
