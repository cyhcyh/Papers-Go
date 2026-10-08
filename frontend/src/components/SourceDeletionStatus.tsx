import {CheckCircle2,LoaderCircle,TriangleAlert} from 'lucide-react'
import './source-deletion.css'

export type SourceDeletionJob={id:number;status:'queued'|'running'|'failed'|'done';phase:string;total:number;processed:number;deleted_papers:number;kept_papers:number;removed_pdf_files:number;error:string|null;sources:{key:string;code:string;label:string}[]}

export function SourceDeletionStatus({jobs,onRetry}:{jobs:SourceDeletionJob[];onRetry:(id:number)=>void}){
 const pending=jobs.filter(job=>job.status!=='done'),visible=pending.length?pending:jobs.slice(0,1)
 return <div className="source-deletion-list">{visible.map(job=>{
  const active=job.status==='queued'||job.status==='running'
  const title=job.status==='done'?'删除完成':job.status==='failed'?'删除未完成':job.status==='queued'?'已提交后台删除':({preparing:'正在整理删除任务',papers:'正在后台清理论文',profiles:'正在更新兴趣设置',finalizing:'正在完成清理'} as Record<string,string>)[job.phase]||'正在后台删除'
  return <section className={'source-deletion-status '+job.status} key={job.id} aria-label="分类删除进度">
   <div className="source-deletion-heading">{job.status==='done'?<CheckCircle2 size={18}/>:job.status==='failed'?<TriangleAlert size={18}/>:<LoaderCircle size={18}/>}<strong>{title}</strong><span>{job.sources.length} 个分类</span></div>
   <div className="source-deletion-count" role="status">已处理 {job.processed.toLocaleString()} / {job.total.toLocaleString()} 篇</div>
   {active&&<progress max={Math.max(1,job.total)} value={job.processed} aria-label="论文清理进度"/>}
   {active&&<p>关闭页面后仍会继续，完成后可在这里查看结果。</p>}
   {job.status==='done'&&<p>删除 {job.deleted_papers.toLocaleString()} 篇论文，保留 {job.kept_papers.toLocaleString()} 篇共享论文。</p>}
   {job.status==='failed'&&<div className="source-deletion-error"><p>{job.error}已完成的进度会保留。</p><button onClick={()=>onRetry(job.id)}>继续清理</button></div>}
  </section>
 })}</div>
}
