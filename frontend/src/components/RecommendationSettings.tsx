import {useEffect,useState} from 'react'
import {SlidersHorizontal,RotateCcw,Check,Save,ChevronDown} from 'lucide-react'
import {api} from '../api'
import {useLoad} from '../hooks/useLoad'
import {useApp} from '../context'
import {Loading,ErrorBox} from './Common'
import {IconTile} from './PageContentUI'
import '../recommendation-settings.css'

type Weights={personal:{interest:number;quality:number;diversity:number;author:number};guest:{quality:number;recency:number;author:number}}
type Configuration={value:Weights;defaults:Weights}
type Draft={interest:string;quality:string;diversity:string;guestQuality:string;recency:string;personalAuthor:string;guestAuthor:string}
const percent=(n:number)=>String(Math.round(n*1000000)/10000)
const form=(v:Weights):Draft=>({interest:percent(v.personal.interest),quality:percent(v.personal.quality),diversity:percent(v.personal.diversity),guestQuality:percent(v.guest.quality),recency:percent(v.guest.recency),personalAuthor:percent(v.personal.author),guestAuthor:percent(v.guest.author)})
const coefficient=(n:string)=>(Number(n)/100).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:3})

export function RecommendationSettings(){
 const result=useLoad<Configuration>('/admin/recommendation'),{toast}=useApp(),[draft,setDraft]=useState<Draft|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('')
 useEffect(()=>{if(result.data)setDraft(form(result.data.value))},[result.data])
 if(result.error)return <ErrorBox error={result.error} reload={result.reload}/>
 if(!draft||!result.data)return <Loading/>
 const personal=Number(draft.interest)+Number(draft.quality)+Number(draft.diversity)+Number(draft.personalAuthor),guest=Number(draft.guestQuality)+Number(draft.recency)+Number(draft.guestAuthor)
 const valid=Object.values(draft).every(v=>v.trim()!==''&&Number.isFinite(Number(v))&&Number(v)>=0&&Number(v)<=100)&&Math.abs(personal-100)<1e-6&&Math.abs(guest-100)<1e-6
 const dirty=JSON.stringify(draft)!==JSON.stringify(form(result.data.value))
 const input=(key:keyof Draft,title:string,help:string)=><label className="recommendation-field">{title}<div><input aria-label={title+(key==='guestQuality'||key==='guestAuthor'?'（游客）':'权重')} type="number" required min={0} max={100} step="any" value={draft[key]} onChange={e=>{setDraft({...draft,[key]:e.target.value});setError('')}}/><span>%</span></div><small>{help}</small></label>
 const sum=(value:number,values:string[])=><div className="recommendation-total"><div aria-hidden="true">{values.map((v,i)=><i key={i} style={{flexGrow:Math.max(0,Number(v)||0)}}/>)}</div><span className={Math.abs(value-100)<1e-6?'valid':'invalid'}>{Math.abs(value-100)<1e-6&&<Check size={16}/>}权重合计 {Number(value.toFixed(1))}%</span></div>
 const authorHelp='使用后台共享缓存；数据缺失时，其余项按比例计算。质量分中的作者加分继续保留。'
 const save=async(e:React.FormEvent)=>{e.preventDefault();if(!valid)return;setBusy(true);setError('');try{const saved=await api<Configuration>('/admin/recommendation','PUT',{personal:{interest:Number(draft.interest)/100,quality:Number(draft.quality)/100,diversity:Number(draft.diversity)/100,author:Number(draft.personalAuthor)/100},guest:{quality:Number(draft.guestQuality)/100,recency:Number(draft.recency)/100,author:Number(draft.guestAuthor)/100}});result.setData(saved);toast('推荐权重已保存，新请求将按新权重计算')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
 return <section className="panel recommendation-settings"><header className="recommendation-heading"><IconTile icon={SlidersHorizontal}/><div><h2>推荐分设置</h2><p>调整推荐分的组成比例。</p></div></header><form onSubmit={e=>void save(e)}><fieldset disabled={busy}>
  <section className="recommendation-personal"><h3>有兴趣画像的用户</h3><p className="recommendation-formula">推荐分 = {coefficient(draft.interest)} × 兴趣匹配 + {coefficient(draft.quality)} × 质量 + {coefficient(draft.diversity)} × 新近程度 + {coefficient(draft.personalAuthor)} × 作者影响力</p><div className="recommendation-fields">{input('interest','兴趣匹配','按最匹配的兴趣方向计算语义相关程度')}{input('quality','论文质量','保留内容、会议、作者和社区质量信号')}{input('diversity','新近程度','按已知发表日期计算，30天半衰期；入库时间不加分')}{input('personalAuthor','作者影响力',authorHelp)}</div>{sum(personal,[draft.interest,draft.quality,draft.diversity,draft.personalAuthor])}</section>
  <details className="recommendation-guest"><summary>游客 / 无兴趣画像的用户<ChevronDown size={17}/></summary><p className="recommendation-formula">推荐分 = {coefficient(draft.guestQuality)} × 质量 + {coefficient(draft.recency)} × 时效性 + {coefficient(draft.guestAuthor)} × 作者影响力</p><p className="recommendation-detail-help">时效性由 100 分起，每天递减 5 分，最低为 0 分。</p><div className="recommendation-fields guest-fields">{input('guestQuality','论文质量','使用已有的论文质量结果')}{input('recency','时效性','适当提高较新论文的推荐分')}{input('guestAuthor','作者影响力',authorHelp)}</div>{sum(guest,[draft.guestQuality,draft.recency,draft.guestAuthor])}</details>
 </fieldset>{error&&<ErrorBox error={error}/>}<footer className="recommendation-footer"><p>{!valid?'各项须为 0–100%，每组权重合计须为 100%。':dirty?'修改尚未保存。':'保存后用于新的推荐计算，总分封顶 100。'}</p><div><button type="button" disabled={busy} onClick={()=>{setDraft(form(result.data!.defaults));setError('')}}><RotateCcw size={14}/>恢复默认</button><button className="primary" disabled={busy||!valid||!dirty}><Save size={14}/>{busy?'正在保存…':'保存推荐设置'}</button></div></footer></form></section>
}
