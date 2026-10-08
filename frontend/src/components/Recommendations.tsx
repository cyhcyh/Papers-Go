import {CardSurface} from './CardUI'
import {MathText} from './MathText'
import {paperDateLabel} from './PaperDate'
import {paperSourceCode} from './PaperLabels'
import {useEffect,useState} from 'react'
import {ArrowUpRight,RefreshCw} from 'lucide-react'
import {api} from '../api'
import {useLoad} from '../hooks/useLoad'
import {ReadingModal} from './ReadingModal'
import {ErrorBox} from './Common'
import type {Paper} from '../types'
import {useApp} from '../context'
export function Recommendations(){
 const {auth}=useApp()
 const result=useLoad<{items:Paper[]}>('/recommendations'),[reading,setReading]=useState<Paper|null>(null)
 useEffect(()=>{let timer:ReturnType<typeof setTimeout>;const update=()=>{clearTimeout(timer);timer=setTimeout(result.reload,500)};window.addEventListener('stats-change',update);window.addEventListener('profile-change',update);return()=>{clearTimeout(timer);window.removeEventListener('stats-change',update);window.removeEventListener('profile-change',update)}},[result.reload])
 const read=(paper:Paper)=>{setReading(paper);if(auth)void api('/interactions','POST',{paper_id:paper.id,action:'expand',feed_context:'browse'}).catch(()=>{})}
 return <><CardSurface className="panel recommendations"><div className="section-heading"><h3>为您推荐</h3><button className="icon-button" aria-label="刷新为您推荐" disabled={result.loading} onClick={result.reload}><RefreshCw size={14}/></button></div>{!auth&&<p className="guest-recommendation-hint">精选论文 · 设置兴趣后为您推荐</p>}{result.error?<ErrorBox error={result.error} reload={result.reload}/>:result.data?.items.map((p,i)=><button className="suggestion" key={p.id} onClick={()=>read(p)}><span className="suggestion-number">{String(i+1).padStart(2,'0')}</span><span><strong><MathText inline>{p.title}</MathText></strong><small>{paperSourceCode(p)} · {paperDateLabel(p)}</small></span><ArrowUpRight size={13}/></button>)}{!result.data?.items.length&&!result.error&&<p className="muted">{result.loading?'正在挑选论文…':'新的论文同步后，推荐会出现在这里。'}</p>}</CardSurface>{reading&&<ReadingModal paper={reading} onClose={()=>setReading(null)}/>}</>
}
