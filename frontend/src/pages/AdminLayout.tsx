import {lazy,Suspense,useEffect,useState} from 'react'
import {NavLink,Navigate,useLocation} from 'react-router-dom'
import {House,LayoutDashboard,Settings,Workflow,FolderTree,Tags,Users,Bot,ScrollText,Menu,X,Moon,Sun,LogOut,RefreshCw} from 'lucide-react'
import {useApp} from '../context'
import {SiteBrand} from '../site'
import {SystemInfo} from '../components/SystemInfo'
import {Login} from './Login'
import {Loading} from '../components/Common'
import '../admin.css'
const Admin=lazy(()=>import('./Admin').then(m=>({default:m.Admin})))
const Overview=lazy(()=>import('./AdminOverview').then(m=>({default:m.AdminOverview})))
const Basics=lazy(()=>import('./AdminBasics').then(m=>({default:m.AdminBasics})))
const menu=[['overview','概览',LayoutDashboard],['settings','基本设置',Settings],['pipeline','任务中心',Workflow],['categories','分类与来源',FolderTree],['topics','主题管理',Tags],['users','用户管理',Users],['models','模型配置',Bot],['logs','日志',ScrollText]] as const
const sections:Record<string,string>={pipeline:'sources',categories:'categories',topics:'topics',users:'users',models:'models',logs:'logs'}
export function AdminLayout({base,dark,toggleTheme}:{base:string;dark:boolean;toggleTheme:()=>void}){
 const {auth,logout}=useApp(),location=useLocation(),[open,setOpen]=useState(false),[version,setVersion]=useState(0)
 const section=location.pathname.slice(base.length).replace(/^\//,'')||'overview',title=menu.find(item=>item[0]===section)?.[1]
 useEffect(()=>{setOpen(false)},[location.pathname])
 if(!auth)return <div className="admin-login"><Login admin onSuccess={()=>{}}/></div>
 if(!auth.user.is_admin)return <div className="admin-denied"><h1>仅管理员可进入</h1><p>请使用管理员账号登录。</p><button onClick={logout}>退出当前账号</button><NavLink to="/">回到首页</NavLink></div>
 if(section==='metrics')return <Navigate to={base+'/overview'} replace/>
 if(location.pathname===base||location.pathname===base+'/')return <Navigate to={base+'/overview'} replace/>
 return <div className={'admin-shell '+(['categories','topics','users'].includes(section)?'admin-shell-management':'')}>{open&&<button className="admin-backdrop" aria-label="关闭后台菜单" onClick={()=>setOpen(false)}/>}
  <aside className={'admin-sidebar '+(open?'open':'')}><NavLink to={base+'/overview'} className="brand"><SiteBrand compact/></NavLink><div className="admin-sidebar-caption">管理后台</div><nav aria-label="后台导航"><NavLink to="/" className="admin-home"><House size={18}/>回到首页</NavLink>{menu.map(([key,label,Icon])=><NavLink key={key} to={base+'/'+key}><Icon size={18}/><span>{label}</span></NavLink>)}</nav><div className="admin-account"><span className="avatar">{auth.user.username.slice(0,1).toUpperCase()}</span><div>{auth.user.username}<small>管理员</small></div><button className="icon-button" aria-label="退出登录" onClick={logout}><LogOut size={17}/></button></div></aside>
  <div className="admin-workspace"><header className="admin-topbar"><button className="icon-button admin-menu-toggle" aria-label={open?'关闭后台菜单':'打开后台菜单'} onClick={()=>setOpen(v=>!v)}>{open?<X size={20}/>:<Menu size={20}/>}</button><span className="admin-breadcrumb">管理后台 <span className="muted">/ {title||'页面不存在'}</span></span><div className="admin-toolbar-actions"><SystemInfo className="toolbar-system-info"/><button className="icon-button" aria-label="刷新当前后台页面" onClick={()=>setVersion(v=>v+1)}><RefreshCw size={17}/></button><button className="icon-button" aria-label="切换暗色模式" onClick={toggleTheme}>{dark?<Sun size={18}/>:<Moon size={18}/>}</button></div></header>
   <main className="admin-main">{!['pipeline','overview','categories','topics','users'].includes(section)&&<h1 className="admin-page-title">{title||'页面不存在'}</h1>}<Suspense fallback={<Loading/>}>{section==='overview'?<Overview key={version} base={base}/>:section==='settings'?<Basics key={version}/>:sections[section]?<Admin key={section+version} tab={sections[section]}/>:<NavLink to={base+'/overview'}>回到概览</NavLink>}</Suspense></main>
  </div></div>
}
