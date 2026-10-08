import {useEffect,useState} from 'react'
import {Check,Copy,ExternalLink,BookOpen,Globe,Shield,FileText,ScrollText,Upload,Link as LinkIcon,type LucideIcon} from 'lucide-react'
import {api} from '../api'
import {useLoad} from '../hooks/useLoad'
import {copyText} from '../clipboard'
import {useApp} from '../context'
import {Loading,ErrorBox} from '../components/Common'
import type {Site} from '../site'
import {IconTile} from '../components/PageContentUI'
import {RecommendationSettings} from '../components/RecommendationSettings'
import '../admin-basics.css'
type Settings=Site&{admin_path:string;fulltext_cache_days:number;fulltext_cache_mb:number;log_retention_days:number;log_max_entries:number;logo_upload?:{data:string};favicon_upload?:{data:string}}
function SettingHeading({icon,title,description,tone='blue'}:{icon:LucideIcon;title:string;description:string;tone?:'blue'|'green'|'purple'}){
 return <header className="basic-card-heading"><IconTile icon={icon} tone={tone}/><div><h2>{title}</h2><p>{description}</p></div></header>
}
export function AdminBasics(){
 const result=useLoad<Settings>('/admin/site'),{toast}=useApp(),[draft,setDraft]=useState<Settings|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[previews,setPreviews]=useState<Record<string,string>>({})
 useEffect(()=>{if(result.data){setDraft(result.data);setPreviews({})}},[result.data])
 if(result.error)return <ErrorBox error={result.error} reload={result.reload}/>
 if(!draft)return <Loading/>
 const selectImage=async(field:'logo'|'favicon',file:File|undefined)=>{
  if(!file)return
  if(file.size>2*1024*1024){setError('每个图标最多 2 MB');return}
  try{const data=await new Promise<string>((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result));reader.onerror=reject;reader.readAsDataURL(file)});setDraft(d=>d&&({...d,[field+'_upload']:{data:data.slice(data.indexOf(',')+1)}}));setPreviews(p=>({...p,[field]:data}));setError('')}catch{setError('无法读取图标文件，请重新选择')}
 }
 const save=async(event:React.FormEvent)=>{event.preventDefault();setBusy(true);setError('');try{const saved=await api<Settings>('/admin/site','PUT',draft);window.dispatchEvent(new Event('site-change'));toast('基本设置已保存');if(saved.admin_path!==result.data?.admin_path){window.location.assign(saved.admin_path+'/settings');return}result.setData(saved)}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
 const address=window.location.origin+(result.data?.admin_path||draft.admin_path)
 return <><form className="admin-basic-form" onSubmit={save}>
  <p className="basic-page-description">网站信息与存储设置</p>
  <fieldset className="basic-settings-fields" disabled={busy}>
   <div className="basic-grid basic-top-grid">
    <section className="panel basic-branding"><SettingHeading icon={Globe} title="网站信息" description="设置网站的基本信息，用于展示在前台。"/>
     <label>网站名字<input required maxLength={60} value={draft.name} onChange={e=>setDraft({...draft,name:e.target.value})}/></label>
     <label>网站描述<textarea rows={3} maxLength={500} value={draft.description} onChange={e=>setDraft({...draft,description:e.target.value})}/></label>
     <div className="basic-image-fields">{(['logo','favicon'] as const).map(field=>{
      const preview=previews[field]||draft[field+'_url' as 'logo_url'|'favicon_url']
      return <div className="basic-image-field" key={field}><span className="basic-field-title">{field==='logo'?'网站 Logo':'浏览器图标'}</span><div className="basic-upload-tile"><div className="basic-image-preview">{preview?<img src={preview} alt={field==='logo'?'Logo 预览':'图标预览'}/>:field==='logo'?<BookOpen size={30}/>:<FileText size={30}/>}<span>{field==='logo'?draft.name:'浏览器标签图标'}</span></div><p>PNG、JPEG、WebP 或 ICO · 最多 2 MB</p><div className="basic-upload-actions"><label className="basic-upload-button"><Upload size={14}/>上传图片<input className="basic-file-input" type="file" accept="image/png,image/jpeg,image/webp,image/x-icon,image/vnd.microsoft.icon,.ico" onChange={e=>void selectImage(field,e.target.files?.[0])}/></label><button type="button" onClick={()=>{setDraft({...draft,[field+'_url']:'',[field+'_upload']:undefined});setPreviews(p=>({...p,[field]:''}))}}>恢复默认</button></div></div></div>
     })}</div>
    </section>
    <section className="panel basic-entry"><SettingHeading icon={Shield} title="管理员安全入口" description="设置管理后台的访问路径。"/>
     <label>入口路径<input required minLength={7} maxLength={81} pattern="/[A-Za-z0-9][A-Za-z0-9_-]{5,79}" value={draft.admin_path} onChange={e=>setDraft({...draft,admin_path:e.target.value})}/></label>
     <p className="basic-help">使用 / 开头的 6～80 位字母、数字、下划线或短横线。</p>
     <div className="basic-entry-address"><strong>当前入口</strong><div><LinkIcon size={16}/><code>{address}</code></div><nav aria-label="当前管理员入口操作"><button type="button" onClick={()=>copyText(address).then(()=>toast('入口地址已复制')).catch(()=>toast('请手动复制入口地址'))}><Copy size={14}/>复制</button><a href={address} target="_blank" rel="noopener noreferrer"><ExternalLink size={14}/>打开</a></nav></div>
     <p className="basic-help">入口仅在后台展示。更改后旧地址失效，保存后自动跳转到新入口。</p>
    </section>
   </div>
   <div className="basic-grid basic-storage-grid">
    <section className="panel basic-fulltext-cache"><SettingHeading icon={FileText} tone="green" title="全文缓存" description="设置论文全文文本的缓存策略。"/>
     <div className="basic-number-fields"><label>保留天数<div className="basic-unit-input"><input type="number" required min={0} max={3650} step={1} value={draft.fulltext_cache_days} onChange={e=>setDraft({...draft,fulltext_cache_days:Number(e.target.value)})}/><span>天</span></div></label><label>容量上限<div className="basic-unit-input"><input type="number" required min={0} max={1048576} step={1} value={draft.fulltext_cache_mb} onChange={e=>setDraft({...draft,fulltext_cache_mb:Number(e.target.value)})}/><span>MB</span></div></label></div>
     <p className="basic-help">默认 7 天 / 500 MB，达到任一限制就清理；任一项设为 0 时，任务结束后不保留文本缓存。PDF 提取后立即删除，已生成的精读卡保留。</p>
    </section>
    <section className="panel basic-log-settings"><SettingHeading icon={ScrollText} tone="purple" title="日志设置" description="设置系统日志的保存策略。"/>
     <div className="basic-number-fields"><label>保存天数<div className="basic-unit-input"><input type="number" required min={1} max={3650} step={1} value={draft.log_retention_days} onChange={e=>setDraft({...draft,log_retention_days:Number(e.target.value)})}/><span>天</span></div></label><label>最多保存<div className="basic-unit-input"><input type="number" required min={1} max={10000000} step={1} value={draft.log_max_entries} onChange={e=>setDraft({...draft,log_max_entries:Number(e.target.value)})}/><span>条</span></div></label></div>
     <p className="basic-help">默认 30 天 / 500,000 条。超过任一限制时，后台分批清理最旧日志，通常每 30 秒检查。API 凭据和 token 用量统计单独保留。</p>
    </section>
   </div>
  </fieldset>
  {error&&<ErrorBox error={error}/>}<div className="basic-save"><button className="primary" disabled={busy}><Check size={15}/>{busy?'正在保存…':'保存基本设置'}</button></div>
 </form><RecommendationSettings/></>
}
