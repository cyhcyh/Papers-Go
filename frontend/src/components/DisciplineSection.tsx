import {useState,type ReactNode} from 'react'
import {ChevronDown,ChevronRight} from 'lucide-react'

export function DisciplineSection({label,count,forceOpen=false,initialOpen=false,expanded:controlled,onToggle,summary,selection,children}:{label:string;count:number;forceOpen?:boolean;initialOpen?:boolean;expanded?:boolean;onToggle?:()=>void;summary?:ReactNode;selection?:ReactNode;children:ReactNode}){
 const [expanded,setExpanded]=useState(initialOpen),open=controlled??(forceOpen||expanded)
 return <section className="discipline-section"><div className="discipline-heading-row">{selection}<button type="button" className="discipline-heading" aria-expanded={open} onClick={()=>onToggle?onToggle():setExpanded(value=>!value)}>{open?<ChevronDown size={15}/>:<ChevronRight size={15}/>}<span>{label}</span>{summary&&<span className="tree-selection-summary">{summary}</span>}<small>{count}</small></button></div>{open&&<div className="discipline-children">{children}</div>}</section>
}
