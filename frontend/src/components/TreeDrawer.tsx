import {useEffect,useRef,useState,type ReactNode} from 'react'
import {PanelLeft,X} from 'lucide-react'

export function TreeDrawer({title,label,children}:{title:string;label:string;children:ReactNode}){
 const [open,setOpen]=useState(false),dialog=useRef<HTMLDialogElement>(null)
 useEffect(()=>{const node=dialog.current;if(open&&!node?.open)node?.showModal();if(!open&&node?.open)node.close()},[open])
 useEffect(()=>{const media=matchMedia('(min-width: 601px)'),close=()=>{if(media.matches)setOpen(false)};media.addEventListener('change',close);return()=>media.removeEventListener('change',close)},[])
 return <div className="mobile-tree-control"><button onClick={()=>setOpen(true)} aria-label={'打开'+title}><PanelLeft size={17}/>{title}</button><span>{label}</span><dialog ref={dialog} className="tree-drawer" aria-label={title} onCancel={()=>setOpen(false)} onClose={()=>setOpen(false)} onClick={e=>{if(e.target===e.currentTarget)setOpen(false)}}><div className="drawer-heading"><strong>{title}</strong><button className="icon-button" aria-label={'收起'+title} onClick={()=>setOpen(false)}><X size={18}/></button></div><div className="tree-drawer-content">{open&&children}</div><button className="tree-drawer-done" onClick={()=>setOpen(false)}>完成选择</button></dialog></div>
}
