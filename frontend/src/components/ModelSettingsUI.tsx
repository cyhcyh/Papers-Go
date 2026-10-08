import {useEffect,useRef,useState} from 'react'
import type {ReactNode} from 'react'
import {Bot,Cloud,Sparkles,MoreHorizontal,Pencil,Trash2,Box,ChevronRight,Link,Globe,KeyRound,ShieldCheck,Copy,Play,RefreshCw,Settings2,X,Info} from 'lucide-react'

export type Thinking='auto'|'on'|'off'
export type Caps={thinking_modes:Thinking[];reasoning_efforts:string[];note:string;capability_source?:string;type?:string;embedding_dimensions?:number[]}
export type Model=Caps&{id:string;name:string}
type Compatibility={active_version?:string;latest_version?:string;checked_at?:string;validated_at?:string;validation_model?:string;warning?:string|null;error?:string|null}
export type Catalog={models:Model[];updated_at:string|null;error?:string|null;compatibility?:Compatibility}
export type Auth={logged_in:boolean;status:string;user_code?:string;verification_url?:string}
export type Connection={id:string;name:string;kind:'ollama'|'cloud'|'codex';base_url:string;configured:boolean;api_key?:string;clear_key?:boolean;credential_revision?:string;oauth_client_id?:string;codex_auto_update?:boolean;codex_client_version?:string;catalog?:Catalog;auth?:Auth}
export type Selection={connection_id:string;model:string;thinking?:Thinking;reasoning_effort?:string}
export type Route={primary:Selection;fallback:Selection|null}
export type Feature={id:string;name:string;requirement:string}
export const thinkingNames={auto:'跟随模型默认',on:'开启思考',off:'关闭思考'}
export const effortNames:Record<string,string>={auto:'模型默认',none:'无',minimal:'最低',low:'低',medium:'中',high:'高',xhigh:'更高',max:'最高',ultra:'极高'}
export const kindNames={ollama:'本地',cloud:'云端 API',codex:'Codex'}

export function ConnectionIcon({kind}:{kind:Connection['kind']}){
 const Icon=kind==='ollama'?Bot:kind==='cloud'?Cloud:Sparkles
 return <span className={'mc-connection-icon '+kind}><Icon size={26} strokeWidth={1.8}/></span>
}

export function SettingsDialog({title,description,icon,children,footer,onClose,busy=false,error}:{title:string;description?:string;icon?:ReactNode;children:ReactNode;footer?:ReactNode;onClose:()=>void;busy?:boolean;error?:string}){
 const dialog=useRef<HTMLDialogElement>(null)
 useEffect(()=>{const element=dialog.current;element?.showModal();return()=>element?.close()},[])
 return <dialog ref={dialog} className="mc-dialog" aria-label={title} onCancel={event=>{event.preventDefault();if(!busy)onClose()}} onClick={event=>{if(event.target===event.currentTarget&&!busy)onClose()}}>
  <header className="mc-dialog-header"><div className="mc-dialog-heading">{icon}<div><h2>{title}</h2>{description&&<p>{description}</p>}</div></div><button type="button" className="icon-button" aria-label="关闭弹窗" disabled={busy} onClick={onClose}><X size={20}/></button></header>
  <div className="mc-dialog-body">{children}</div>{error&&<p role="alert" className="mc-dialog-error error-text">{error}</p>}{footer&&<footer className="mc-dialog-footer">{footer}</footer>}
 </dialog>
}

export function SettingsBlock({title,description,children}:{title:string;description?:string;children:ReactNode}){
 return <section className="mc-settings-block"><div className="mc-block-heading"><h3>{title}</h3>{description&&<p>{description}</p>}</div>{children}</section>
}

function ConnectionRow({icon,label,value,copy,onCopy}:{icon:ReactNode;label:string;value:string;copy?:boolean;onCopy:(value:string)=>void}){
 return <div className="mc-connection-row">{icon}<span>{label}</span><strong title={value}>{value}</strong>{copy&&<button type="button" className="icon-button" aria-label={'复制'+label} onClick={()=>onCopy(value)}><Copy size={14}/></button>}</div>
}

export function ConnectionCard({connection,status,busy,working,used,onEdit,onRemove,onCatalog,onCapabilities,onAdvanced,onTest,onRefresh,onLogin,onLogout,onCopy}:{connection:Connection;status?:{ok:boolean;message:string};busy:boolean;working:boolean;used:boolean;onEdit:()=>void;onRemove:()=>void;onCatalog:()=>void;onCapabilities:()=>void;onAdvanced:()=>void;onTest:()=>void;onRefresh:()=>void;onLogin:()=>void;onLogout:()=>void;onCopy:(value:string)=>void}){
 const [menu,setMenu]=useState(false)
 const c=connection,authorized=!!c.auth?.logged_in,updated=c.catalog?.updated_at
 const state=working?'正在处理':status?status.ok?'已连接':'连接异常':c.kind==='codex'?authorized?'已授权':c.auth?.status==='waiting'?'等待授权':c.auth?.status==='expired'?'授权过期':'未授权':c.configured?'已配置':'待配置'
 const tone=status&&!status.ok?'error':working||c.auth?.status==='waiting'?'pending':authorized||status?.ok?'success':'neutral'
 return <article className={'mc-connection-card '+c.kind}>
  <div className="mc-connection-heading"><ConnectionIcon kind={c.kind}/><div className="mc-connection-identity"><div><h3 title={c.name}>{c.name||'未命名连接'}</h3><span className={'mc-type-badge '+c.kind}>{kindNames[c.kind]}</span></div><p>{c.kind==='ollama'?'在本地运行的模型':c.kind==='cloud'?'通过 API 访问云端模型':'ChatGPT 账户授权接入'}</p></div><div className="mc-card-menu" onBlur={event=>{if(!event.currentTarget.contains(event.relatedTarget as Node|null))setMenu(false)}}><button type="button" className="icon-button" aria-label={'管理连接 '+c.name} aria-expanded={menu} onClick={()=>setMenu(!menu)}><MoreHorizontal size={20}/></button>{menu&&<div className="mc-menu-popover"><button type="button" disabled={busy} onClick={()=>{setMenu(false);onEdit()}}><Pencil size={15}/>编辑连接</button><button type="button" className="mc-danger-action" disabled={busy||used} title={used?'使用中的连接需要先调整功能分配':undefined} onClick={()=>{setMenu(false);onRemove()}}><Trash2 size={15}/>移除连接</button></div>}</div></div>
  <div className={'mc-connection-status '+tone}><span/>{state}</div>
  <button type="button" className="mc-catalog-summary" onClick={onCatalog}><Box size={23}/><span><strong>{c.catalog?`${c.catalog.models.length} 个可用模型`:'尚未更新模型目录'}</strong><small>{updated?'最后更新 '+new Date(updated).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'更新目录后可在功能分配中选择'}</small></span><ChevronRight size={18}/></button>
  <div className="mc-connection-details"><ConnectionRow icon={<Link size={17}/>} label="连接名称" value={c.name||'未命名连接'} copy onCopy={onCopy}/><ConnectionRow icon={<Globe size={17}/>} label={c.kind==='codex'?'服务地址':'Base URL'} value={c.base_url||'尚未填写'} copy={!!c.base_url} onCopy={onCopy}/>{c.kind==='cloud'&&<ConnectionRow icon={<KeyRound size={17}/>} label="API Key" value={c.clear_key?'保存后清除':c.api_key?'已填写，待保存':c.configured?'••••••••••••••••':'尚未配置'} onCopy={onCopy}/>}</div>
  {c.kind==='codex'&&<div className="mc-oauth"><div><ShieldCheck size={19}/><span>OAuth 授权<small className={authorized?'mc-success-text':'muted'}>{authorized?'已登录 ChatGPT':c.auth?.status==='waiting'?'等待完成授权':c.auth?.status==='expired'?'授权已过期':'尚未登录'}</small></span></div><div><button type="button" disabled={busy||working} onClick={onLogin}>{authorized?'重新授权':'登录 Codex'}</button>{(authorized||c.auth?.status==='waiting')&&<button type="button" className="mc-danger-action" disabled={busy||working} onClick={onLogout}>退出授权</button>}</div></div>}
  {c.auth?.status==='waiting'&&<div className="mc-device-login"><p>打开 <a href={c.auth.verification_url} target="_blank" rel="noreferrer">授权页面</a>，输入设备码</p><strong>{c.auth.user_code}</strong><small>完成后自动更新，设备码约 15 分钟内有效。</small></div>}
  <div className="mc-connection-actions"><button type="button" disabled={busy||working||!c.base_url} onClick={onTest}><Play size={15}/>测试连接</button><button type="button" disabled={busy||working||!c.base_url} onClick={onRefresh}><RefreshCw size={15} className={working?'animate-spin':undefined}/>更新可用模型</button></div>
  {status&&<p role="status" className={'mc-test-result '+(status.ok?'mc-success-text':'error-text')}>{status.message}</p>}
  {!status&&c.catalog?.compatibility?.warning&&<p className="mc-test-result muted">{c.catalog.compatibility.warning}</p>}{!status&&c.catalog?.compatibility?.error&&<p className="mc-test-result error-text">{c.catalog.compatibility.error}</p>}
  <div className="mc-connection-bottom">{c.kind==='codex'&&<button type="button" onClick={onAdvanced}><ChevronRight size={15}/><Settings2 size={16}/>高级配置</button>}{!!c.catalog?.models.length&&<button type="button" onClick={onCapabilities}><ChevronRight size={15}/><Box size={16}/>补充模型能力</button>}</div>
 </article>
}

export function FeatureGroup({icon,title,description,count,children}:{icon:ReactNode;title:string;description:string;count:number;children:ReactNode}){
 return <section className="mc-feature-group"><header><div className="mc-group-title"><span>{icon}</span><h3>{title}</h3><p>{description}</p></div><small>共 {count} 个功能</small></header><div className="mc-feature-grid">{children}</div></section>
}

export function CodexTaskNotice({feature,visible}:{feature:string;visible:boolean}){
 if(!visible||!['classify','brief','quality'].includes(feature))return null
 return <div className="mc-codex-notice" role="note"><Info size={17} aria-hidden="true"/><span>新论文数量大时此任务请求频繁，建议使用<strong>API方式</strong>，谨慎考虑<strong>Codex</strong>。</span></div>
}

export function FeatureCard({feature,icon,description,route,connections,onModel,onAdvanced,children,wide=false}:{feature:Feature;icon:ReactNode;description:string;route:Route;connections:Connection[];onModel:()=>void;onAdvanced:()=>void;children?:ReactNode;wide?:boolean}){
 const primary=route.primary,connection=connections.find(c=>c.id===primary.connection_id)
 const embedding=feature.id==='embedding',mode=primary.thinking||'auto',effort=primary.reasoning_effort||'auto'
 const assigned=!!connection&&!!primary.model
 const missing=!!connection?.catalog?.updated_at&&!!primary.model&&!connection.catalog.models.some(m=>m.id===primary.model)
 return <article className={'mc-feature-card'+(wide?' wide':'')}>
  <div className="mc-feature-heading"><span className={'mc-feature-icon '+feature.id}>{icon}</span><h4>{feature.name}</h4><span className="mc-requirement">{embedding?'embedding':feature.id==='chat'?'流式':feature.requirement}</span>{feature.id==='chat'&&<span className="mc-requirement">工具调用</span>}</div><p className="mc-feature-description">{description}</p>
  <div className="mc-feature-config"><div className="mc-assigned-model"><small>模型配置</small><strong title={`${connection?kindNames[connection.kind]+' · '+connection.name:'未选择连接'} / ${primary.model}`}>{assigned?kindNames[connection!.kind]+' · '+connection!.name+' / '+primary.model:'请选择'}</strong></div>{!embedding&&<><div><small>思考模式</small><span className={'mc-value-pill '+(mode==='auto'?'neutral':'success')}>{assigned?thinkingNames[mode]:"—"}</span></div><div><small>推理强度</small><span className="mc-value-pill neutral">{assigned?(effortNames[effort]||effort):"—"}</span></div></>}</div>
  {children}{route.fallback&&<p className="mc-fallback-note">备用：{connections.find(c=>c.id===route.fallback?.connection_id)?.name} / {route.fallback.model||'待选择'}</p>}
  {missing&&<p className="mc-missing-model">当前模型不在最新目录，可重新选择，或在弹窗中手动确认模型名称。</p>}
  <CodexTaskNotice feature={feature.id} visible={connection?.kind==='codex'||connections.find(c=>c.id===route.fallback?.connection_id)?.kind==='codex'}/>
  <div className="mc-feature-actions"><button type="button" onClick={onModel}><Pencil size={14}/>更改模型</button><button type="button" onClick={onAdvanced}><Settings2 size={14}/>高级设置</button></div>
 </article>
}
