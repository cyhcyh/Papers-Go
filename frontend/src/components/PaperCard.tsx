import {MathText} from './MathText'
import {PaperDate} from './PaperDate'
import {PaperAuthors} from './PaperAuthors'
import {paperSourceCode,TopicTag} from './PaperLabels'
import {ArrowUpRight,BookOpen,MessageCircle,Flame,Star,FileText,ChevronDown} from 'lucide-react'
import {useNavigate} from 'react-router-dom'
import type {Paper} from '../types'
import {useApp} from '../context'
import {PaperFeedbackButtons,type PaperFeedbackAction} from './PaperFeedbackButtons'
import {CardSurface,PaperBriefBlock,PaperFeedbackGroup} from './CardUI'
export function PaperCard({paper,onRead,compact=false,extra,onFeedback,feedbackDisabled=false}:{paper:Paper;onRead:(p:Paper)=>void;compact?:boolean;extra?:React.ReactNode;onFeedback?:(p:Paper,action:PaperFeedbackAction)=>Promise<void>;feedbackDisabled?:boolean}) {
 const navigate=useNavigate(),brief=paper.brief,{requireLogin}=useApp()
 const sentences=[['研究问题',brief?.problem||'论文速读尚未生成，可展开查看摘要。'],['主要贡献和结果',brief?.contribution_result||[brief?.contribution,brief?.result].filter(Boolean).join(' ')||paper.tldr||'等待摘要解读。']]
 return <CardSurface as="article" className={'paper-card '+(compact?'compact':'')}><div className="paper-card-content"><div className="paper-top"><div className="tags"><span className="tag source-tag">{paperSourceCode(paper)}</span>{paper.topics?.filter(t=>t.name_en!=='Uncategorized').slice(0,2).map(t=><TopicTag key={t.id} name={t.name_zh}/>)}</div><div className="paper-meta">{paper.score!==undefined&&<span className="recommendation-score" title="综合推荐优先级">推荐 {paper.score.toFixed(1)}</span>}<PaperDate paper={paper}/></div></div>
 <h2><button className="paper-title-button" onClick={()=>onRead(paper)}><MathText inline>{paper.title}</MathText></button></h2>{brief?.title_zh&&<p className="original-title"><MathText inline>{brief.title_zh}</MathText></p>}<PaperAuthors authors={paper.authors||[]}/>
 <div className="paper-brief">{sentences.map(([label,text],index)=><PaperBriefBlock key={label} label={label} kind={index===0?'question':'contribution'}><MathText>{text}</MathText></PaperBriefBlock>)}</div>
 <details className="abstract-preview"><summary><FileText size={16} aria-hidden="true"/><span>查看论文摘要</span><ChevronDown className="abstract-chevron" size={16} aria-hidden="true"/></summary><p><MathText>{paper.abstract||'摘要尚未同步'}</MathText></p></details>
 {(paper.hf_upvotes>0||paper.github_stars>0||paper.venue_rank)&&<div className="signals">{paper.hf_upvotes>0&&<span><Flame size={14}/>HF {paper.hf_upvotes}</span>}{paper.github_stars>0&&<span><Star size={13}/>GitHub {paper.github_stars}</span>}{paper.venue_rank&&<span>{paper.venue_rank}</span>}</div>}
 </div><div className="paper-actions">{paper.abs_url&&<a href={paper.abs_url} target="_blank" rel="noreferrer" aria-label="论文原文"><ArrowUpRight size={14}/>原文</a>}<button onClick={()=>onRead(paper)} aria-label="展开论文"><BookOpen size={15}/>展开</button><button onClick={()=>{if(requireLogin('智能助手',()=>navigate('/chat?paper='+paper.id)))navigate('/chat?paper='+paper.id)}} aria-label="问 AI"><MessageCircle size={15}/><span>问 AI</span></button><PaperFeedbackGroup>{onFeedback&&<PaperFeedbackButtons paper={paper} onAction={onFeedback} disabled={feedbackDisabled}/>}{extra}</PaperFeedbackGroup></div></CardSurface>
}
