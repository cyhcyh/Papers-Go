import {Clock3,LoaderCircle} from 'lucide-react'
import type {ReadingAnswers,ReadingCardData} from './ReadingCardContent'

export interface ReadingProgressData {
 stage:string;started_at?:string;queued_at?:string;queue_ahead?:number;mode?:string;
 completed?:number;total?:number;answers_completed?:number;answers_total?:number;
 answers?:ReadingAnswers;paper_kind?:ReadingCardData['paper_kind'];elapsed_seconds?:number
 source_name?:string
}

export function ReadingProgress({progress,clock,hasPartial=false}:{progress?:ReadingProgressData;clock:number;hasPartial?:boolean}){
 const stage=progress?.stage||'parsing',queued=stage==='queued'
 const elapsed=progress?.elapsed_seconds??Math.max(0,Math.floor((clock-Date.parse((queued?progress?.queued_at:progress?.started_at)||''))/1000))
 const completed=progress?.answers_completed||0,total=progress?.answers_total||6
 const label=queued?`排队中，前面还有 ${progress?.queue_ahead??0} 位`:stage==='materials'?`正在整理长文材料 · ${progress?.completed||0}/${progress?.total||0}`:stage==='generating'?`正在生成 · 已完成 ${completed}/${total} 个问题`:({parsing:'正在准备论文全文',resolving:'正在查找备用全文来源',thinking:'模型正在思考',verifying:'正在核验原文证据'}[stage]||'正在阅读论文')
 return <div className="reading-generating" role="status" aria-live="polite">
  {queued?<Clock3/>:<LoaderCircle className="animate-spin"/>}
  <div><p>{label}</p><small>{queued?'等待空闲名额 · ':progress?.mode==='direct'?'直接读取全文 · ':''}{Number.isFinite(elapsed)?`${queued?'已等待':'已用'} ${Math.floor(elapsed/60)} 分 ${Math.floor(elapsed%60)} 秒`:'正在连接'}{hasPartial?' · 内容正在逐步显示':''}</small>
   {stage==='generating'&&<progress max={total} value={completed} aria-label="六问回答完成情况"/>}
   {stage==='materials'&&!!progress?.total&&<progress max={progress.total} value={progress.completed||0} aria-label="长文材料整理进度"/>}
   <small className="reading-background-hint">关闭后仍会继续，完成后再次打开即可查看。</small>
  </div>
 </div>
}
