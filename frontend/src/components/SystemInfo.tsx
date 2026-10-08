import {Github} from 'lucide-react'
import {useApp} from '../context'
import {systemInfo} from '../system-info'
import './system-info.css'

/** Static source configuration; no request, timer or user-editable setting. */
export function SystemInfo({className=''}:{className?:string}){
 const {githubUrl,version}=systemInfo,{toast}=useApp()
 return <div className={'system-info '+className}>
  {githubUrl?<a href={githubUrl} target="_blank" rel="noopener noreferrer" aria-label="打开 GitHub 仓库" title="GitHub 仓库"><Github size={17}/></a>:<button type="button" aria-label="GitHub 仓库尚未配置" title="GitHub 仓库尚未配置" onClick={()=>toast('仓库地址尚未配置')}><Github size={17}/></button>}
  <span className="system-version" aria-label={'系统版本 '+version}>{version}</span>
  <span className="system-info-divider" aria-hidden="true"/>
 </div>
}
