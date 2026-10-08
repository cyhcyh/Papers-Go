import {LoaderCircle,Inbox,RefreshCw,X} from 'lucide-react'
import type {ReactNode} from 'react'
export function PageTitle({eyebrow,title,description,action}:{eyebrow:string;title:string;description?:string;action?:ReactNode}) {return <header className="page-header"><div><p className="eyebrow">{eyebrow}</p><h1>{title}</h1>{description&&<p className="muted">{description}</p>}</div>{action}</header>}
export function Loading(){return <div className="empty"><LoaderCircle className="animate-spin"/><p>正在加载</p></div>}
export function Empty({title='这里还没有内容',text,action}:{title?:string;text?:string;action?:ReactNode}){return <div className="empty"><Inbox size={32}/><h3>{title}</h3>{text&&<p>{text}</p>}{action}</div>}
export function ErrorBox({error,reload}:{error:string;reload?:()=>void}){return <div className="error-box"><p>{error}</p>{reload&&<button onClick={reload}><RefreshCw size={15}/>重试</button>}</div>}
export function Modal({title,onClose,children,floatingClose=false,className='',titleIcon}:{title:string;onClose:()=>void;children:ReactNode;floatingClose?:boolean;className?:string;titleIcon?:ReactNode}){
 const content=<><div className="modal-header"><h2>{titleIcon}{title}</h2>{!floatingClose&&<button className="icon-button" onClick={onClose} aria-label="关闭"><X/></button>}</div>{children}</>
 return <div className="modal-overlay" onClick={onClose}>{floatingClose?<section role="dialog" aria-modal="true" aria-label={title} className="modal-frame" onClick={e=>e.stopPropagation()}><button className="modal-floating-close" onClick={onClose} aria-label="关闭"><X/></button><div className={'modal '+className}>{content}</div></section>:<section role="dialog" aria-modal="true" aria-label={title} className={'modal '+className} onClick={e=>e.stopPropagation()}>{content}</section>}</div>
}
export function formatTime(value:string|null|undefined){return value?new Date(value).toLocaleString('zh-CN',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'尚未运行'}
