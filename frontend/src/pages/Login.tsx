import {useState,useEffect} from 'react'
import {Link,useNavigate,useLocation} from 'react-router-dom'
import {ArrowRight,FileText,UserRound,LockKeyhole,Search,Sparkles} from 'lucide-react'
import {api,storeAuth,type Auth} from '../api'
import {copyText} from '../clipboard'
import {useApp} from '../context'
import {SiteBrand,useSite} from '../site'
import {SystemInfo} from '../components/SystemInfo'
import {AdminRegistrationNotice} from '../components/AdminRegistrationNotice'
import '../auth-cards.css'

function AdminLoginBackdrop(){
 return <svg className="admin-auth-backdrop" viewBox="0 0 1440 900" preserveAspectRatio="xMidYMid slice" aria-hidden="true" focusable="false">
  <g fill="none" stroke="currentColor" strokeWidth="1">
   <path d="M0 116 68 90 138 138 212 112 302 40M68 90 84 210 138 138 226 234M0 284 84 210 226 234 316 172M212 112 226 234M1190 26 1268 112 1368 68 1440 110M1268 112 1240 236 1332 196 1440 272M1368 68 1332 196 1392 342M1240 236 1392 342 1440 414M0 634 102 584 174 692 282 630M102 584 84 770 174 692 248 824 362 858M0 842 84 770 248 824M1120 722 1212 636 1302 720 1400 636 1440 688M1212 636 1238 822 1302 720 1398 846M1238 822 1398 846 1440 798M1400 636 1398 846"/>
   <path d="M-100 586C264 584 458 902 914 870S1250 594 1520 568M-80 646C318 646 450 974 986 906S1282 666 1530 638" opacity=".45"/>
  </g>
  <g fill="currentColor">
   {[[68,90,5],[138,138,3],[212,112,3],[84,210,3],[226,234,4],[316,172,2],[1268,112,4],[1368,68,3],[1240,236,3],[1332,196,5],[1392,342,3],[102,584,4],[174,692,3],[84,770,3],[248,824,5],[1212,636,3],[1302,720,4],[1400,636,3],[1238,822,3],[1398,846,5]].map(([cx,cy,r])=><circle key={cx+'-'+cy} cx={cx} cy={cy} r={r}/>)}
  </g>
 </svg>
}

function AuthIllustration(){
 return <div className="auth-illustration" aria-hidden="true"><span className="auth-orbit"/><span className="auth-paper"><FileText/><span className="auth-paper-search"><Search/></span></span><Sparkles className="auth-spark"/></div>
}

export function Login({embedded=false,admin=false,onSuccess}:{embedded?:boolean;admin?:boolean;onSuccess?:()=>void}) {
 const location=useLocation(),navigate=useNavigate(),{toast}=useApp()
 const site=useSite()
 const [createAccount,setCreateAccount]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[config,setConfig]=useState({require_invite_code:false,first_user:false}),[adminEntry,setAdminEntry]=useState('')
 const register=!admin&&(embedded?createAccount:location.pathname==='/register')
 useEffect(()=>{if(admin)return;api<typeof config>('/auth/config').then(setConfig).catch(e=>setError(e.message))},[])
 const submit=async(event:React.FormEvent<HTMLFormElement>)=>{
  event.preventDefault();const form=new FormData(event.currentTarget);setBusy(true);setError('')
  try{const auth=await api<Auth>('/auth/'+(admin?'admin-login':register?'register':'login'),'POST',Object.fromEntries(form));storeAuth(auth);if(register&&auth.admin_entry){setAdminEntry(auth.admin_entry);return}toast(register?'账号已创建，可随时在设置中完善兴趣':'欢迎回来');if(onSuccess)onSuccess();else navigate('/')}
  catch(e){setError((e as Error).message)}finally{setBusy(false)}
 }
 const finishRegistration=()=>{setAdminEntry('');if(onSuccess)onSuccess();else navigate('/')}
 const switchMode=(next:boolean)=>{setCreateAccount(next);setError('')}
 return <><div className={embedded?'auth-embedded':'auth-page'+(admin?' admin-auth-page':'')}>
  {admin&&!embedded&&<AdminLoginBackdrop/>}
  {!embedded&&<div className="auth-story"><Link to="/" className="brand"><SiteBrand compact/></Link><p className="eyebrow">A DAILY DOSE OF DISCOVERY</p><h1>少一点检索，<br/>多一点发现。</h1><p>把下一篇值得读的论文，<br/>带入您的视线。</p>{!admin&&<div className="auth-foot"></div>}</div>}
  <div className="auth-form"><AuthIllustration/><h2>{admin?'管理员登录':'欢迎来到'+site.name}</h2><p className="muted">{admin?'登录后管理本站数据与设置。':'登录或注册，开启您的学术探索之旅。'}</p>
   <form onSubmit={submit}><label>用户名<span className="auth-input"><UserRound aria-hidden="true"/><input name="username" autoComplete="username" minLength={3} maxLength={40} required placeholder="请输入用户名"/></span></label><label>密码<span className="auth-input"><LockKeyhole aria-hidden="true"/><input name="password" type="password" autoComplete={register?'new-password':'current-password'} minLength={8} maxLength={128} required placeholder={register?'至少 8 个字符':'请输入密码'}/></span></label>
    {register&&config.require_invite_code&&!config.first_user&&<label>邀请码<input name="invite_code" required placeholder="由管理员提供"/></label>}{register&&config.first_user&&<p className="hint">首个账号将成为本机管理员。</p>}
    {error&&<p role="alert" className="error-text">{error}</p>}<button className="primary full" disabled={busy}>{busy?'请稍候':register?'创建账号':'登录'}<ArrowRight size={17}/></button>
   </form>{!admin&&<><div className="auth-divider"><span>或</span></div><p className="auth-switch">{register?'已有账号？':'还没有账号？'} {embedded?<button type="button" onClick={()=>switchMode(!register)}>{register?'立即登录':'立即注册'}</button>:<Link to={register?'/login':'/register'} onClick={()=>setError('')}>{register?'立即登录':'立即注册'}</Link>}</p></>
   }{!embedded&&<Link className="guest-return" to="/">先随便看看 →</Link>}
  </div>
  {admin&&!embedded&&<SystemInfo className="auth-system-info-top"/>}
 </div>{adminEntry&&<AdminRegistrationNotice entry={adminEntry} onClose={finishRegistration} onCopy={()=>copyText(window.location.origin+adminEntry).then(()=>toast('安全入口已复制')).catch(()=>toast('请手动复制入口地址'))}/>}</>
}
