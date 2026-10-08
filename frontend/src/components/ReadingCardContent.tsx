import {CheckCircle,AlertCircle,FileText,TriangleAlert} from 'lucide-react'
import {MathText} from './MathText'
import {ChatContent} from './ChatContent'
import '../reading-card.css'

export type ReadingAnswers=Record<'problem'|'related_work'|'method'|'evaluation'|'future'|'summary',string>
export const readingLevelName=(level:string|undefined)=>level==='L3'?'深度解析':'论文精读'
export interface ReadingCardData {
 tldr:string;method_summary:string;key_results:{claim:string;verified:boolean;evidence:{section:string;quote:string}}[];
 limitations:string[];read_priority:string;reading_level:string;answers?:ReadingAnswers|null;
 paper_kind?:'empirical'|'theoretical'|'mixed';schema_version?:number
 fulltext_source?:{name:string;url:string;pdf_url:string;version:string;preprint:boolean}
}

export function ReadingCardContent({card,generating=false,failed=false}:{card:ReadingCardData;generating?:boolean;failed?:boolean}){
 const questions:[keyof ReadingAnswers,string][]=[
  ['problem','研究问题'],['related_work','已有研究与本文定位'],
  ['method','核心方法'],['evaluation','关键结果与证据'],
  ['future','后续研究方向'],['summary','核心总结']]
 return <div className="reading-content">
  <div className="reading-meta"><span className="tag explore">{generating?(failed?'未完成的内容':'生成中'):{must_read:'重点精读',worth_reading:'值得阅读',skim:'快速浏览'}[card.read_priority]||'精读卡'}</span><span className="muted">{readingLevelName(card.reading_level)}</span></div>
  {card.fulltext_source&&<p className="reading-fulltext-source">全文来源：<a href={card.fulltext_source.url} target="_blank" rel="noreferrer">{card.fulltext_source.name}</a>{card.fulltext_source.preprint?' · arXiv 预印本':' · '+(card.fulltext_source.version||'论文全文')}{card.fulltext_source.preprint&&card.fulltext_source.version?' · '+card.fulltext_source.version:''}</p>}
  {card.answers?<div className="reading-questions">{questions.map(([key,title],index)=>(!generating||card.answers![key])&&<section className="reading-question" key={key}><h3><span className="reading-question-number">{index+1}、</span><span>{title}</span></h3><ChatContent text={card.answers![key]}/></section>)}</div>:<>
   <h3>一句话</h3><p><MathText>{card.tldr}</MathText></p><h3>方法要点</h3><ChatContent text={card.method_summary||''}/>
  </>}
  {!generating&&<details className="reading-evidence"><summary><FileText size={17} aria-hidden="true"/>关键结果与原文证据 · {card.key_results?.length||0} 条</summary>
   {card.key_results?.map((r,i)=><div className={'evidence '+(!r.verified?'unverified':'')} key={i}><strong><MathText>{r.claim}</MathText></strong><blockquote><MathText>{r.evidence.quote}</MathText></blockquote><small>{r.verified?<CheckCircle size={13}/>:<AlertCircle size={13}/>} {r.evidence.section} · {r.verified?'引文已在全文回验':'引文未通过回验'}</small></div>)}
   {!card.key_results?.length&&<p className="muted">当前材料没有可核实的结果引文。</p>}
  </details>}
  {!!card.limitations?.length&&<details className="reading-evidence"><summary><TriangleAlert size={17} aria-hidden="true"/>局限与适用边界</summary><ol className="reading-limitations">{card.limitations.map((text,i)=><li key={i}><MathText>{text}</MathText></li>)}</ol></details>}
 </div>
}
