import {useState} from 'react'
import {useNavigate} from 'react-router-dom'
import {ArrowRight,ArrowLeft,Check} from 'lucide-react'
import {api} from '../api'
import {useLoad} from '../hooks/useLoad'
import {CategoryPicker} from '../components/CategoryPicker'
import {PageTitle,ErrorBox} from '../components/Common'
import type {Category,CategorySelection} from '../types'
export function Onboarding() {
 const [step,setStep]=useState(0),[description,setDescription]=useState(''),[selection,setSelection]=useState<CategorySelection>({categories:[],topics:{}}),[exclusions,setExclusions]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');const navigate=useNavigate();const catalog=useLoad<Category[]>('/categories')
 const submit=async()=>{setBusy(true);setError('');try{await api('/profile/init','POST',{description,category_selection:selection,exclusions:exclusions.split('\n').filter(Boolean)});navigate('/')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
 return <div className="onboarding"><PageTitle eyebrow="YOUR RESEARCH, YOUR FEED" title="先从您的好奇心开始" description="建立初始兴趣画像，之后也可以随时调整。"/><div className="steps">{['研究背景','关注主题','明确排除'].map((label,i)=><button key={label} className={step===i?'active':''} onClick={()=>setStep(i)}><span>{i<step?<Check size={13}/>:i+1}</span>{label}</button>)}</div><div className="panel wizard">{step===0&&<><h2>您最近在研究什么？</h2><p className="muted">介绍研究背景，供智能助手参考。下一步选择用于推荐和趋势的分类与主题。</p><textarea rows={7} value={description} maxLength={2000} onChange={e=>setDescription(e.target.value)} placeholder="例如：我在研究大模型智能体的长期记忆，尤其关心图谱记忆和长程任务评测。"/></>}{step===1&&<><h2>选择感兴趣的分类与主题</h2><p className="muted">勾选分类将选中下面全部主题，您也可以只选择部分主题。各类论文混合展示，关注程度越高，展示比例越大。</p>{catalog.error?<ErrorBox error={catalog.error} reload={catalog.reload}/>:<CategoryPicker catalog={catalog.data||[]} selection={selection} onChange={setSelection}/>}</>}{step===2&&<><h2>有什么方向暂时不想看？</h2><p className="muted">可选，每行填写一个方向。</p><textarea rows={6} value={exclusions} onChange={e=>setExclusions(e.target.value)} placeholder="例如：具身智能（embodied AI）"/></>}{error&&<ErrorBox error={error}/>}<div className="wizard-actions">{step>0&&<button onClick={()=>setStep(s=>s-1)}><ArrowLeft size={16}/>上一步</button>}<button className="primary" disabled={busy||(!description.trim()&&!selection.categories.length&&!Object.values(selection.topics).some(ids=>ids.length)&&step===2)} onClick={()=>step<2?setStep(s=>s+1):submit()}>{busy?'正在建立画像':step===2?'开始刷论文':'下一步'}<ArrowRight size={16}/></button></div></div></div>
}
