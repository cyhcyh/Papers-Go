import type {ReactNode} from 'react'
import {CircleHelp,Lightbulb} from 'lucide-react'

/** Shared presentation only; callers own their data and interactions. */
export function CardSurface({as:Tag='section',className='',children}:{as?:'article'|'section'|'div';className?:string;children:ReactNode}){
 return <Tag className={'research-card '+className}>{children}</Tag>
}

export function PaperBriefBlock({label,kind,children}:{label:string;kind:'question'|'contribution';children:ReactNode}){
 const Icon=kind==='question'?CircleHelp:Lightbulb
 return <div className={'paper-brief-block '+kind}><Icon className="paper-brief-icon" size={28} aria-hidden="true"/><span>{label}</span><p>{children}</p></div>
}

export function PaperFeedbackGroup({children}:{children:ReactNode}){
 return <div className="paper-feedback-actions">{children}</div>
}
