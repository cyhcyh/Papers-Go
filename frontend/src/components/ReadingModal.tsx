import {PaperDate} from './PaperDate'
import {MathText} from './MathText'
import {usePaperDwell} from '../hooks/usePaperDwell'
import {useEffect,useState} from 'react'
import {RefreshCw,BookOpen,FileText} from 'lucide-react'
import {api,streamChat} from '../api'
import {Modal,ErrorBox} from './Common'
import {ReadingCardContent,readingLevelName,type ReadingCardData,type ReadingAnswers} from './ReadingCardContent'
import {ReadingProgress,type ReadingProgressData} from './ReadingProgress'
import {IconTile} from './PageContentUI'
import type {Paper} from '../types'
import {useApp} from '../context'
interface CardResponse {status:string;error?:string;card?:ReadingCardData;progress?:ReadingProgressData}

export function ReadingModal({paper,onClose,level='L2'}:{paper:Paper;onClose:()=>void;level?:'L2'|'L3'}) {
 const {requireLogin,auth}=useApp(),isAdmin=!!auth?.user.is_admin
 usePaperDwell(paper.id,'browse',true)
 const [data,setData]=useState<CardResponse|null>(null),[error,setError]=useState(''),[generate,setGenerate]=useState(false),[clock,setClock]=useState(Date.now())
 const [attempt,setAttempt]=useState({revision:0,operation:'view'})
 useEffect(()=>{
  if(!generate)return
  let active=true
  const controller=new AbortController()
  const load=async()=>{
   let status='pending'
   const connecting=setTimeout(()=>controller.abort(),15000)
   try{
    // Show the persisted task immediately, before opening its live subscription.
    // Retry/regenerate is submitted once; subscribing only attaches to that task.
    const snapshot=await api<CardResponse>(`/papers/${paper.id}/card?`+new URLSearchParams({level,retry:String(attempt.operation==='retry'),regenerate:String(attempt.operation==='regenerate')}),'GET',undefined,controller.signal)
    clearTimeout(connecting)
    if(!active)return
    status=snapshot.status;setData(snapshot)
    if(status!=='pending')return
    await streamChat(`/papers/${paper.id}/card/stream`,{level},(event,value:CardResponse)=>{
     if(!active||event!=='card')return
     status=value.status;setData(value)
    },controller.signal)
    if(active&&status==='pending')setError('连接已中断，后台仍在生成。点击重试可重新连接。')
   }catch(e){if(active)setError(controller.signal.aborted?'连接超时，请重新连接查看任务状态。':(e as Error).message)}
   finally{clearTimeout(connecting)}
  }
  setError('');void load()
  return()=>{active=false;controller.abort()}
 },[paper.id,attempt,generate,level])
 const pending=generate&&(!data||data.status==='pending'),progress=data?.progress
 useEffect(()=>{if(!pending)return;setClock(Date.now());const timer=setInterval(()=>setClock(Date.now()),1000);return()=>clearInterval(timer)},[pending])
 const hasPartial=Object.values(progress?.answers||{}).some(Boolean)
 const partial:ReadingCardData|undefined=hasPartial?{tldr:'',method_summary:'',key_results:[],limitations:[],read_priority:'',reading_level:level,answers:progress!.answers,paper_kind:progress?.paper_kind||'mixed'}:undefined
 const card=(pending?partial:undefined)||data?.card||partial
 const retry=()=>setAttempt(x=>({revision:x.revision+1,operation:data?.status==='failed'?'retry':'resume'}))
 const regenerateCard=()=>{setData(value=>value?{...value,status:'pending',progress:undefined}:value);setAttempt(x=>({revision:x.revision+1,operation:'regenerate'}))}
 return <Modal title={readingLevelName(level)} onClose={onClose} floatingClose className="reading-dialog" titleIcon={<IconTile icon={BookOpen}/>}>
  <p className="reading-title"><MathText inline>{paper.title}</MathText></p>
  {paper.brief?.title_zh&&<p className="original-title"><MathText inline>{paper.brief.title_zh}</MathText></p>}
  <p className="paper-authors">{paper.authors.join(' · ')} · <PaperDate paper={paper}/></p>
  <p className="hint">原始主分类：{paper.primary_category||paper.venue||'未标注'}{paper.categories?.length?' · 分类：'+paper.categories.join('、'):''}</p>
  <div className="paper-detail-abstract"><h3><FileText size={17} aria-hidden="true"/>论文摘要</h3><p><MathText>{paper.abstract||'摘要尚未同步'}</MathText></p>{paper.abs_url&&<a className="detail-source" href={paper.abs_url} target="_blank" rel="noreferrer">阅读论文原文 ↗</a>}</div>
  {!generate&&<button className="primary reading-generate-action" onClick={()=>{if(requireLogin('生成精读卡',()=>setGenerate(true)))setGenerate(true)}}>{level==='L3'?'开始深度解析':'生成精读卡'}</button>}
  {error&&<ErrorBox error={error} reload={retry}/>}
  {pending&&!error&&<ReadingProgress progress={progress} clock={clock} hasPartial={hasPartial}/>}
  {data?.status==='failed'&&<ErrorBox error={data.error||'生成失败'} reload={isAdmin||!data.card?retry:undefined}/>}
  {card&&<><ReadingCardContent card={card} generating={card===partial} failed={data?.status==='failed'||!!error}/>{isAdmin&&<div className="reading-regenerate"><button disabled={pending} onClick={regenerateCard}><RefreshCw size={14}/>重新生成精读卡</button>{!card.answers&&<span className="muted">重新生成后使用六问结构。</span>}</div>}</>}
 </Modal>
}
