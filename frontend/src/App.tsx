import {CardSurface} from './components/CardUI'
import {lazy,Suspense,useEffect,useState} from 'react'
import {Routes,Route,NavLink,Navigate,useLocation,useNavigate} from 'react-router-dom'
import {FileText,Layers,TrendingUp,FolderTree,Bookmark,MessageCircle,Settings as SettingsIcon,Bell,Sun,Moon,LogOut} from 'lucide-react'
import {api,readAuth,storeAuth,type Auth} from './api'
import {AppContext,useApp} from './context'
import {Login} from './pages/Login'
import {Onboarding} from './pages/Onboarding'
import {SiteBrand} from './site'
import {SystemInfo} from './components/SystemInfo'
import {Feed} from './pages/Feed'
import {Browse} from './pages/Browse'
const Trends=lazy(()=>import('./pages/Trends').then(m=>({default:m.Trends})))
const Chat=lazy(()=>import('./pages/Chat').then(m=>({default:m.Chat})))
import {Notifications} from './pages/Notifications'
import {Settings} from './pages/Settings'
const AdminLayout=lazy(()=>import('./pages/AdminLayout').then(m=>({default:m.AdminLayout})))
const adminBase=document.querySelector('meta[name="app-admin-base"]')?.getAttribute('content')||''
import {Recommendations} from './components/Recommendations'
import {InterestTrends} from './components/InterestTrends'
import {Modal,Loading} from './components/Common'

const nav=[['/','刷论文',Layers],['/trends','趋势雷达',TrendingUp],['/browse','分类浏览',FolderTree],['/library','我的书架',Bookmark],['/chat','智能助手',MessageCircle]] as const
function applyTheme(){const pref=localStorage.getItem('theme');const dark=pref==='dark'||(!pref&&matchMedia('(prefers-color-scheme: dark)').matches);document.documentElement.classList.toggle('dark',dark);return dark}
function AuthGate({label,children}:{label:string;children:React.ReactNode}){
 const {auth,requireLogin}=useApp()
 return auth?<>{children}</>:<div className="panel login-needed"><h2>登录后使用{label}</h2><p>论文信息流和分类浏览可以直接查看。</p><button className="primary" onClick={()=>requireLogin(label)}>登录 / 注册</button><NavLink to="/">继续刷论文</NavLink></div>
}
export default function App(){
 const [auth,setAuth]=useState<Auth|null>(readAuth()),[toast,setToast]=useState(''),[dark,setDark]=useState(applyTheme),[unread,setUnread]=useState(0),[stats,setStats]=useState<any>(null),[loginRequest,setLoginRequest]=useState<{reason:string;resume?:()=>void}|null>(null)
 const location=useLocation(),navigate=useNavigate()
 useEffect(()=>{const update=()=>setAuth(readAuth());const storage=(event:StorageEvent)=>{if(event.key==='auth'||event.key===null)update()};window.addEventListener('auth-change',update);window.addEventListener('storage',storage);return ()=>{window.removeEventListener('auth-change',update);window.removeEventListener('storage',storage)}},[])
 useEffect(()=>{document.body.classList.toggle('feed-view',location.pathname==='/');return()=>document.body.classList.remove('feed-view')},[location.pathname])
 useEffect(()=>{const update=()=>setDark(applyTheme());const query=matchMedia('(prefers-color-scheme: dark)');window.addEventListener('theme-change',update);query.addEventListener('change',update);return ()=>{window.removeEventListener('theme-change',update);query.removeEventListener('change',update)}},[])
 useEffect(()=>{if(toast){const timer=setTimeout(()=>setToast(''),3500);return ()=>clearTimeout(timer)}},[toast])
 useEffect(()=>{if(!auth)return;let active=true,generation=0;const update=()=>{const current=++generation;api<{unread:number}>('/notifications/count').then(n=>{if(active&&current===generation)setUnread(n.unread)}).catch(()=>{});api('/stats/today').then(s=>{if(active&&current===generation)setStats(s)}).catch(()=>{})};update();const timer=setInterval(update,30000);window.addEventListener('stats-change',update);return ()=>{active=false;clearInterval(timer);window.removeEventListener('stats-change',update)}},[auth?.user.id])
 const requireLogin=(reason:string,resume?:()=>void)=>{if(readAuth())return true;setLoginRequest({reason,resume});return false}
 const visit=(event:React.MouseEvent,path:string,label:string)=>{if(path==='/'||path==='/browse'||readAuth())return;event.preventDefault();requireLogin(label,()=>navigate(path))}
 const toggleTheme=()=>{localStorage.setItem('theme',dark?'light':'dark');window.dispatchEvent(new Event('theme-change'))}
 const logout=async()=>{try{if(auth)await api('/auth/logout','POST',{refresh_token:auth.refresh_token})}catch{}storeAuth(null);setStats(null);setUnread(0);navigate('/')}
 const authPage=['/login','/register'].includes(location.pathname)
 const adminPage=adminBase&&(location.pathname===adminBase||location.pathname.startsWith(adminBase+'/'))
 return <AppContext.Provider value={{auth,toast:setToast,logout,requireLogin}}>{authPage?<Login/>:adminPage?<Suspense fallback={<Loading/>}><AdminLayout base={adminBase} dark={dark} toggleTheme={toggleTheme}/></Suspense>:<div className={'app-shell '+(location.pathname==='/'?'is-feed':'')}>
  <aside className="sidebar"><NavLink className="brand" to="/"><SiteBrand/></NavLink><nav>{nav.map(([url,label,Icon])=><NavLink to={url} aria-label={label} end={url==='/'} key={url} onClick={e=>visit(e,url,label)}><Icon size={19}/><span>{label}</span></NavLink>)}<NavLink to="/notifications" aria-label="通知中心" onClick={e=>visit(e,'/notifications','通知中心')}><Bell size={19}/><span>通知中心</span>{unread>0&&<i className="unread-dot"/>}</NavLink></nav>
   <div className="sidebar-bottom"><NavLink to="/settings" aria-label="设置" onClick={e=>visit(e,'/settings','设置兴趣')}><SettingsIcon size={18}/><span>设置</span></NavLink>
    {auth?<div className="user-row"><span className="avatar">{auth.user.username.slice(0,1).toUpperCase()}</span><span>{auth.user.username}<small>{auth.user.is_admin?'本机管理员':'研究者'}</small></span><button aria-label="退出登录" className="icon-button" onClick={logout}><LogOut size={16}/></button></div>:<div className="user-row guest-user"><span className="avatar">访</span><span>游客浏览<small>先发现，再关注</small></span><button aria-label="登录或注册" onClick={()=>requireLogin('设置兴趣与收藏论文')}>登录</button></div>}
   </div>
  </aside>
  <div className="workspace"><div className="topbar"><NavLink to="/" className="brand mobile-top-brand"><SiteBrand compact/></NavLink><div className="site-toolbar-actions">{!auth&&<button className="guest-login" aria-label="登录 / 注册" onClick={()=>requireLogin('设置兴趣与收藏论文')}><span className="guest-login-full">登录 / 注册</span><span className="guest-login-compact">登录</span></button>}<SystemInfo className="toolbar-system-info"/><NavLink className="icon-button notification-top" to="/notifications" aria-label="通知" onClick={e=>visit(e,'/notifications','通知中心')}><Bell size={18}/>{unread>0&&<i className="unread-dot"/>}</NavLink><button className="icon-button" aria-label="切换暗色模式" onClick={toggleTheme}>{dark?<Sun size={18}/>:<Moon size={18}/>}</button></div></div>
   <div className={'content-layout '+(location.pathname==='/'?'feed-layout ':'')+(['/admin','/chat','/browse','/onboarding','/settings'].includes(location.pathname)?'wide':'')}><main className="main-content"><Suspense fallback={<Loading/>}><Routes>
    <Route path="/" element={<Feed/>}/><Route path="/browse" element={<Browse/>}/><Route path="/onboarding" element={<AuthGate label="兴趣设置"><Onboarding/></AuthGate>}/><Route path="/trends" element={<AuthGate label="趋势雷达"><Trends/></AuthGate>}/><Route path="/library" element={<AuthGate label="我的收藏"><Browse library/></AuthGate>}/><Route path="/chat" element={<AuthGate label="智能助手"><Chat/></AuthGate>}/><Route path="/notifications" element={<AuthGate label="通知中心"><Notifications/></AuthGate>}/><Route path="/settings" element={<AuthGate label="设置"><Settings/></AuthGate>}/><Route path="*" element={<Navigate to="/" replace/>}/>
   </Routes></Suspense></main><aside className="rightbar">{auth?<CardSurface as="div" className="panel daily-panel"><p className="eyebrow">RESEARCH AT A GLANCE</p><h3>每一次发现，都算数</h3><div className="daily-counts"><div><strong>{stats?.shown||0}</strong><span>今日已刷</span></div><div><strong>{stats?.like||0}</strong><span>喜欢</span></div><div><strong>{stats?.save||0}</strong><span>收藏</span></div></div></CardSurface>:<CardSurface as="div" className="panel daily-panel guest-panel"><p className="eyebrow">FOLLOW YOUR CURIOSITY</p><h3>从一篇论文开始</h3><p className="muted">先随便看看，找到感兴趣的方向。</p><button className="primary" onClick={()=>requireLogin('设置兴趣',()=>navigate('/settings'))}>登录并设置兴趣</button></CardSurface>}
    <Recommendations/><InterestTrends/>
   </aside></div>
  </div><nav className="mobile-tabs">{[nav[0],nav[1],nav[2],nav[4],['/settings','我的',SettingsIcon] as const].map(([url,label,Icon])=><NavLink key={url} to={url} end={url==='/'} onClick={e=>visit(e,url,label)}><Icon size={20}/><span>{label.replace('雷达','').replace('浏览','').replace('研究','')}</span></NavLink>)}</nav>
 </div>}{loginRequest&&<Modal title="登录 / 注册" className="auth-dialog" onClose={()=>setLoginRequest(null)}><Login embedded onSuccess={()=>{const resume=loginRequest.resume;setLoginRequest(null);resume?.()}}/></Modal>}{toast&&<div role="status" className="app-toast">{toast}</div>}</AppContext.Provider>
}
