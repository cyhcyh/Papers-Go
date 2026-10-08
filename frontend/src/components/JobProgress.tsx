export type ProgressStage={key?:string;label?:string;unit?:string;processed?:number;total:number;completed:number;failed:number;pending:number;current_paper?:{id:number;title:string}|null;model?:string;average_seconds?:number|null;estimated_remaining_seconds?:number|null;concurrency?:number;in_flight?:number;papers_per_minute?:number|null}
export type TaskProgressData=ProgressStage&{stage?:string|null;stages?:ProgressStage[];phase?:string;phase_label?:string;phase_completed?:number;phase_total?:number}

export function JobProgress({progress,running}:{progress:TaskProgressData;running:boolean}){
 if(progress.phase==='complete')return null
 if(!running&&(progress.phase==='indexing'||progress.phase==='applying'))return null
 if(progress.phase==='indexing'||progress.phase==='applying')return <div className="job-progress" aria-label="任务进度"><p className="job-stage-label">{progress.phase_label}</p><progress aria-label={progress.phase_label||'任务进度'} {...(progress.phase==='indexing'?{value:progress.phase_completed||0,max:progress.phase_total||1}:{})}/>{progress.phase==='indexing'?<p>已写入 {progress.phase_completed||0} / {progress.phase_total||0} 篇</p>:<p>生成已完成，正在提交模型与索引切换。</p>}</div>
 const stage=progress.stages?.length?progress.stages.find(s=>s.key===progress.stage):progress
 if(!stage||stage.completed+stage.failed>=stage.total)return null
 const stages=[stage]
 return <div className="job-progress" aria-label="任务进度">
  {progress.stages&&<p className="job-stage-label">{running?'当前阶段：'+(progress.stages.find(s=>s.key===progress.stage)?.label||'准备中'):'本次处理记录'}</p>}
  {stages.map((stage,index)=>{
   const unit=stage.unit||'篇',current=running&&(!progress.stages||stage.key===progress.stage)
   return <div className="job-progress-stage" key={stage.key||index}>
    {stage.label&&<strong>{stage.label}</strong>}
    <progress aria-label={(stage.label||'任务')+'进度'} value={stage.completed+stage.failed} max={stage.total||1}/>
    <p>完成 {stage.completed} / {stage.total} {unit} · 失败 {stage.failed}</p>
    <small>待处理 {stage.pending} {unit}{stage.model?' · '+stage.model:''}</small>
    {stage.average_seconds!=null&&<small>每{unit}平均 {stage.average_seconds} 秒{stage.concurrency&&stage.concurrency>1?' · '+stage.concurrency+' 并发':''}{stage.papers_per_minute!=null?' · '+stage.papers_per_minute+' '+unit+'/分钟':''}{current&&stage.estimated_remaining_seconds!=null?' · 预计剩余 '+Math.ceil(stage.estimated_remaining_seconds/60)+' 分钟':''}</small>}
    {current&&stage.current_paper&&<p className="current-paper">当前：{stage.current_paper.title}</p>}
   </div>
  })}
 </div>
}
