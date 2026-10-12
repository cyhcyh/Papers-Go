import {Github} from 'lucide-react'
import {lazy,Suspense,useEffect,useState} from 'react'
import {useApp} from '../context'
import {systemInfo} from '../system-info'
import type {VersionCheck} from '../version-updates'
import './system-info.css'

const VersionDialog=lazy(()=>import('./VersionDialog'))

export function SystemInfo({className=''}:{className?:string}){
 const {githubUrl,version}=systemInfo,{toast,auth}=useApp()
 const admin=!!auth?.user.is_admin
 const [update,setUpdate]=useState<VersionCheck|null>(null),[open,setOpen]=useState(false)
 useEffect(()=>{
  if(!admin){setUpdate(null);return}
  let closed=false
  const check=()=>{void import('../version-updates').then(m=>m.checkVersion()).then(value=>{if(!closed)setUpdate(value)}).catch(()=>{})}
  const idle=typeof window.requestIdleCallback==='function'?window.requestIdleCallback(check,{timeout:3000}):null
  const timer=idle===null?window.setTimeout(check,1500):null
  return()=>{closed=true;if(idle!==null)window.cancelIdleCallback(idle);if(timer!==null)window.clearTimeout(timer)}
 },[admin])
 const hasUpdate=admin&&!!update?.has_update
 return <div className={'system-info '+className}>
  {githubUrl?<a href={githubUrl} target="_blank" rel="noopener noreferrer" aria-label="打开 GitHub 仓库" title="GitHub 仓库"><Github size={17}/></a>:<button type="button" aria-label="GitHub 仓库尚未配置" title="GitHub 仓库尚未配置" onClick={()=>toast('仓库地址尚未配置')}><Github size={17}/></button>}
  <button type="button" className="system-version-button" aria-label={'系统版本 '+version+(hasUpdate?'，有更新':'')+'，查看版本信息'} title="版本与更新" onClick={()=>setOpen(true)}><span className="system-version">{version}</span>{hasUpdate&&<><i className="version-update-dot" aria-hidden="true"/><span className="version-update-label">有更新</span></>}</button>
  <span className="system-info-divider" aria-hidden="true"/>
  {open&&<Suspense fallback={<span className="version-opening" role="status">正在打开…</span>}><VersionDialog admin={admin} initial={update} onUpdate={setUpdate} onClose={()=>setOpen(false)}/></Suspense>}
 </div>
}
