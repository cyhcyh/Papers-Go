import {lazy,useState} from 'react'
import {Plus} from 'lucide-react'
import '../admin-management.css'
import {api} from '../api'
import {useLoad} from '../hooks/useLoad'
import {copyText} from '../clipboard'
import {useApp} from '../context'
import type {Topic} from '../types'
import {TopicManager} from '../components/TopicManager'
import {ModelSettings} from '../components/ModelSettings'
import {AdminUsers} from '../components/AdminUsers'
import {SourceManager} from '../components/SourceManager'
import {AdminLogs} from '../components/AdminLogs'
export {taskNames as names} from '../components/taskNames'
const TaskCenter=lazy(()=>import('../components/TaskCenter').then(module=>({default:module.TaskCenter})))

export function Admin({tab}:{tab:string}){
 const {toast}=useApp();const [invite,setInvite]=useState(''),[busy,setBusy]=useState(false)
 const topics=useLoad<Topic[]>(tab==='topics'?'/admin/topics':null),users=useLoad<{id:number;username:string;disabled:number;is_admin:number}[]>(tab==='users'?'/admin/users':null)
 const run=async(work:()=>Promise<unknown>,message:string)=>{setBusy(true);try{await work();if(message)toast(message)}catch(e){toast((e as Error).message)}finally{setBusy(false)}}
 return <>{tab==='sources'&&<TaskCenter/>}{tab==='categories'&&<SourceManager/>}{tab==='topics'&&<TopicManager topics={topics.data} reload={topics.reload} loading={topics.loading} error={topics.error}/>}{tab==='users'&&<section className="admin-management user-management-page"><div className="management-page-tools"><div><h1 className="management-page-title">用户管理</h1><p>管理用户账号、角色与访问状态，支持批量操作。</p></div><button disabled={busy} onClick={()=>run(async()=>{const r=await api<{code:string}>('/admin/invites','POST');setInvite(r.code)},'邀请码已生成')}><Plus size={15}/>生成邀请码</button></div>{invite&&<div className="panel invite-result">一次性邀请码：<code>{invite}</code><button onClick={()=>copyText(invite).then(()=>toast('已复制')).catch(()=>toast('请手动复制邀请码'))}>复制</button></div>}<div className="management-list-card"><AdminUsers users={users.data} reload={users.reload} loading={users.loading} error={users.error}/></div></section>}{tab==='logs'&&<AdminLogs/>}{tab==='models'&&<ModelSettings/>}</>
}
