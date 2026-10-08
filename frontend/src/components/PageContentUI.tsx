import type {ReactNode} from 'react'
import {Sparkles,type LucideIcon} from 'lucide-react'
import {CardSurface} from './CardUI'

/** Content-area presentation only: data, actions and page frames stay with callers. */
export function ContentCard({as='section',className='',children}:{as?:'section'|'article'|'div';className?:string;children:ReactNode}){
 return <CardSurface as={as} className={'content-card '+className}>{children}</CardSurface>
}

export function IconTile({icon:Icon,tone='blue',className=''}:{icon:LucideIcon;tone?:'blue'|'purple'|'green'|'amber'|'red';className?:string}){
 return <span className={'content-icon '+tone+' '+className} aria-hidden="true"><Icon size={20}/></span>
}

export function ContentHeading({icon,title,action,level='h2',tone='blue'}:{icon:LucideIcon;title:ReactNode;action?:ReactNode;level?:'h2'|'h3';tone?:'blue'|'purple'|'green'|'amber'|'red'}){
 const Heading=level
 return <div className="content-heading"><Heading><IconTile icon={icon} tone={tone}/><span>{title}</span></Heading>{action}</div>
}

export function StatusBadge({tone,children}:{tone:'green'|'amber'|'red'|'neutral';children:ReactNode}){
 return <span className={'content-status '+tone}><span aria-hidden="true"/>{children}</span>
}

export function ConversationGraphic(){
 return <div className="conversation-graphic" aria-hidden="true"><span className="conversation-bubble conversation-back"/><span className="conversation-bubble conversation-front"><span className="conversation-dots"><i/><i/><i/></span></span><Sparkles className="conversation-spark"/></div>
}

export function TrendSignalGraphic(){
 return <svg className="trend-signal-graphic" viewBox="0 0 200 130" aria-hidden="true"><ellipse cx="104" cy="90" rx="85" ry="23" fill="none" stroke="currentColor" strokeWidth="6" opacity=".13" transform="rotate(-16 104 90)"/><path d="M55 40 Q55 30 65 27 L135 9 Q145 7 145 20 V102 Q145 110 135 111 L64 122 Q55 123 55 113Z" fill="currentColor" opacity=".08"/><path d="M90 89V69 M112 83V48 M134 78V30" fill="none" stroke="currentColor" strokeWidth="9" strokeLinecap="round" opacity=".62"/><circle cx="37" cy="59" r="4" fill="currentColor" opacity=".3"/><circle cx="175" cy="31" r="3" fill="currentColor" opacity=".5"/></svg>
}
