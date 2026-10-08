import type {ReactNode} from 'react'
import {ChevronsDown,ChevronsUp} from 'lucide-react'
import '../tree-expansion.css'

export function TreeExpansionControls({onExpand,onCollapse,summary,disabled=false,variant='link'}:{onExpand:()=>void;onCollapse:()=>void;summary?:ReactNode;disabled?:boolean;variant?:'link'|'buttons'}){
 return <div className={'tree-expansion-controls '+(variant==='buttons'?'tree-expansion-buttons':'')}>{summary&&<span>{summary}</span>}<div><button type="button" disabled={disabled} onClick={onExpand}>{variant==='buttons'&&<ChevronsDown size={14} aria-hidden="true"/>}全部展开</button><button type="button" disabled={disabled} onClick={onCollapse}>{variant==='buttons'&&<ChevronsUp size={14} aria-hidden="true"/>}全部收起</button></div></div>
}
