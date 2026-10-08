import type {Paper} from '../types'

export function paperDateLabel(paper:Paper) {
 const value=paper.paper_date||paper.published||String(paper.venue_year||'')
 if(!value)return ''
 if(paper.paper_date_basis==='conference')return `${value} · 会议月份`
 if(value.length===4)return `${value} · 仅年份`
 if(paper.paper_date_basis==='ingested')return `${value} · 收录`
 return value
}

export function PaperDate({paper}:{paper:Paper}) {
 const hint=paper.paper_date_basis==='conference'?'官方会议月份，未确认论文具体发表日':paper.paper_date_basis==='year'?'仅确认届次年份':paper.paper_date_basis==='ingested'?'本站收录日期，未确认论文发表日期':'论文发表日期'
 return <span className="paper-date" title={hint}>{paperDateLabel(paper)}</span>
}
