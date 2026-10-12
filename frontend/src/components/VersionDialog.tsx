import {useEffect,useRef,useState} from 'react'
import {createPortal} from 'react-dom'
import {ArrowUpRight,Check,Package,RefreshCw,X} from 'lucide-react'
import {api} from '../api'
import {systemInfo} from '../system-info'
import {checkVersion} from '../version-updates'
import type {ChangelogEntry,InstalledVersion,RemoteChangelog,VersionCheck} from '../version-updates'
import './version-dialog.css'

export default function VersionDialog({admin,initial,onUpdate,onClose}:{admin:boolean;initial:VersionCheck|null;onUpdate:(value:VersionCheck)=>void;onClose:()=>void}){
 const dialog=useRef<HTMLDialogElement>(null),live=useRef(true)
 const [installed,setInstalled]=useState<InstalledVersion|null>(null),[remote,setRemote]=useState<RemoteChangelog|null>(null)
 const [check,setCheck]=useState<VersionCheck|null>(initial),[checking,setChecking]=useState(admin),[loading,setLoading]=useState(true),[error,setError]=useState('')
 const onUpdateRef=useRef(onUpdate);onUpdateRef.current=onUpdate
 useEffect(()=>{
  const element=dialog.current
  element?.showModal()
  const previous=document.body.style.overflow
  document.body.style.overflow='hidden'
  return()=>{element?.close();document.body.style.overflow=previous}
 },[])
 useEffect(()=>{
  live.current=true
  void api<InstalledVersion>('/version').then(value=>{if(live.current)setInstalled(value)}).catch(()=>{if(live.current)setError('版本记录暂时无法加载，请稍后重试。')}).finally(()=>{if(live.current)setLoading(false)})
  if(admin){
   void Promise.allSettled([checkVersion(),api<RemoteChangelog>('/admin/version/changelog')]).then(([version,notes])=>{
    if(!live.current)return
    if(version.status==='fulfilled'){setCheck(version.value);onUpdateRef.current(version.value)}
    else setError('暂时无法检查更新，请稍后重试。')
    if(notes.status==='fulfilled')setRemote(notes.value)
    else setRemote({status:'failed',changelog:[],checked_at:''})
   }).finally(()=>{if(live.current)setChecking(false)})
  }
  return()=>{live.current=false}
 },[admin])
 const refresh=async()=>{
  if(checking)return
  setChecking(true);setError('')
  const [version,notes]=await Promise.allSettled([checkVersion(true),api<RemoteChangelog>('/admin/version/changelog?force=true')])
  if(!live.current)return
  if(version.status==='fulfilled'){setCheck(version.value);onUpdateRef.current(version.value)}
  else setError('暂时无法检查更新，请稍后重试。')
  if(notes.status==='fulfilled')setRemote(notes.value)
  else setRemote(old=>({status:'failed',changelog:old?.changelog||[],checked_at:''}))
  setChecking(false)
 }
 const current=installed?.current_version||systemInfo.version.replace(/^v/,''),hasUpdate=admin&&!!check?.has_update
 const entries:ChangelogEntry[]=[]
 for(const entry of [...(admin?remote?.changelog||[]:[]),...(installed?.changelog||[])]){
  if(!entries.some(item=>item.version===entry.version))entries.push(entry)
  if(entries.length===10)break
 }
 const status=checking?'正在检查更新…':error||check?.status==='failed'?'暂时无法检查更新':hasUpdate?'新版本已发布':check?.status==='ok'?'当前已是最新版本':'当前安装版本'
 return createPortal(<dialog ref={dialog} className="version-dialog" aria-labelledby="version-dialog-title" onCancel={e=>{e.preventDefault();onClose()}} onClick={e=>{
  if(e.target!==e.currentTarget)return
  const bounds=e.currentTarget.getBoundingClientRect()
  if(e.clientX<bounds.left||e.clientX>bounds.right||e.clientY<bounds.top||e.clientY>bounds.bottom)onClose()
 }}>
  <header className="version-dialog-header"><span className="version-dialog-icon"><Package size={21}/></span><div><small>PAPERS-GO</small><h2 id="version-dialog-title">版本与更新</h2></div><button type="button" className="version-close" aria-label="关闭版本信息" onClick={onClose}><X size={19}/></button></header>
  <section className={'version-summary'+(hasUpdate?' has-update':'')} aria-label="版本概况">
   <div><small>当前版本</small><strong>v{current}</strong><span>已安装</span></div>
   {admin&&<div><small>最新版本</small><strong>{check?.latest_version?'v'+check.latest_version:'—'}</strong><span className={hasUpdate?'version-available':''}>{hasUpdate?'可更新':checking?'检查中':check?.status==='ok'?'已是最新':'尚未确认'}</span></div>}
  </section>
  <div className={'version-check-state'+(hasUpdate?' has-update':'')} role="status"><span>{!checking&&check?.status==='ok'&&!hasUpdate?<Check size={14}/>:<i/>}{status}</span>{admin&&check?.checked_at&&<small>{new Date(check.checked_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})} 检查</small>}</div>
  <div className="version-notes" tabIndex={0} aria-label="更新记录">
   <div className="version-notes-heading"><h3>更新记录</h3><span>{admin&&remote?.changelog.length?'项目仓库':'随当前版本附带'}</span></div>
   {(error||admin&&remote?.status==='failed')&&<p className="version-notes-hint">{error||'暂时无法获取最新日志，以下展示已有记录。'}</p>}
   {loading&&!entries.length?<p className="version-notes-empty">正在加载版本记录…</p>:!entries.length?<p className="version-notes-empty">暂无详细更新记录。</p>:entries.map(entry=><article className="version-release" key={entry.version}>
    <header><h4>v{entry.version}</h4>{entry.version===current?<span className="version-release-current">当前版本</span>:hasUpdate&&entry.version===check?.latest_version?<span className="version-release-new">新版本</span>:null}<time dateTime={entry.date}>{entry.date}</time></header>
    {([{key:'added',title:'新增'},{key:'changed',title:'优化'},{key:'fixed',title:'修复'}] as const).filter(group=>entry[group.key].length).map(group=><section className={'version-change-group version-change-'+group.key} key={group.key}><h5><i/>{group.title}</h5><ul>{entry[group.key].map((item,i)=><li key={i}>{item}</li>)}</ul></section>)}
   </article>)}
  </div>
  <footer className="version-dialog-footer"><p>{admin?'仅检查版本信息，更新需重新部署镜像。':'Papers-Go · 开源论文阅读助手'}</p><div>{admin&&<button type="button" disabled={checking} onClick={()=>void refresh()}><RefreshCw size={14} className={checking?'animate-spin':undefined}/>{checking?'检查中…':'检查更新'}</button>}<a href={systemInfo.githubUrl+(admin?'':'/blob/main/CHANGELOG')} target="_blank" rel="noopener noreferrer">{admin?'查看 GitHub':'更多更新记录'}<ArrowUpRight size={15}/></a></div></footer>
 </dialog>,document.body)
}
