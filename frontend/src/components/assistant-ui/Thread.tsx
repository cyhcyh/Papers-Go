// Adapted from assistant-ui's official Thread (MIT):
// https://github.com/assistant-ui/assistant-ui/blob/main/packages/ui/src/components/react/assistant-ui/elements/thread.aui.tsx
// Preserve the upstream viewport, user bubble, action bar and composer layout.
// Math rendering and administrator-confirmed research actions use app slots.
import {ThreadPrimitive,MessagePrimitive,ComposerPrimitive,ActionBarPrimitive,type TextMessagePartProps} from '@assistant-ui/react'
import {ArrowUp,ArrowDown,ArrowRight,Square,Copy,FileText,ChartNoAxesCombined,Sparkles,CircleHelp} from 'lucide-react'
import type {ReactNode} from 'react'
import {ChatContent} from '../ChatContent'
import {ConversationGraphic} from '../PageContentUI'
import type {Proposal,SkillEvent} from '../../types'
import './thread.css'

function MarkdownText({text}:TextMessagePartProps){return <ChatContent text={text}/>}
const parts={Text:MarkdownText}

export function ResearchThread({empty,running,error,context,proposal,skillEvent}:{empty:boolean;running:boolean;error:string;context:ReactNode;proposal:(p:Proposal)=>ReactNode;skillEvent?:(event:SkillEvent)=>ReactNode}){
 return <ThreadPrimitive.Root className="aui-root aui-thread-root">
  {context}
  <ThreadPrimitive.Viewport className="aui-thread-viewport">
   <div className={'aui-thread-body'+(empty?' aui-thread-empty':'')}>
    {empty&&<div className="aui-thread-welcome"><ConversationGraphic/><h2>想从哪里开始？</h2><p>追问论文、探索方向，或调整您的研究兴趣。</p><div className="aui-thread-suggestions">{['帮我找近期智能体记忆论文','把评测方法加入近期关注','你能做什么？'].map((text,index)=><ThreadPrimitive.Suggestion key={text} prompt={text} send={false}>{index===0?<FileText size={17}/>:index===1?<ChartNoAxesCombined size={17}/>:<CircleHelp size={17}/>}<span>{text}</span><ArrowRight size={15}/></ThreadPrimitive.Suggestion>)}</div></div>}
    <ThreadPrimitive.Messages>{({message})=>{
     // The external-store adapter updates after React commits. Read both text
     // and proposals from this runtime snapshot, never a second list by ID.
     const proposals=message.metadata.custom.proposals as Proposal[]|undefined
     const skillEvents=message.metadata.custom.skill_events as SkillEvent[]|undefined
     const hasText=message.content.some(part=>part.type==='text'&&!!part.text)
     const isRunning=message.role==='assistant'&&message.status.type==='running'
     if(!hasText&&!proposals?.length&&!skillEvents?.length&&!isRunning)return null
     return <MessagePrimitive.Root className={message.role==='user'?'aui-user-message-root':'aui-assistant-message-root'} data-role={message.role}>
      {message.role==='assistant'&&<span className="aui-assistant-mark" aria-hidden="true"><Sparkles size={16}/></span>}
      <div className={message.role==='user'?'aui-user-message-content':'aui-assistant-message-content'}><MessagePrimitive.Parts components={parts}/>{proposals?.map(proposal)}{skillEvents?.map(event=>skillEvent?.(event))}{!hasText&&isRunning&&<div className="aui-typing" role="status" aria-label="正在整理回答"><span/><span/><span/></div>}</div>
      {message.role==='assistant'&&hasText&&<ActionBarPrimitive.Root className="aui-assistant-action-bar" hideWhenRunning autohide="not-last"><ActionBarPrimitive.Copy className="aui-icon-button" aria-label="复制回复" title="复制回复"><Copy size={16}/></ActionBarPrimitive.Copy></ActionBarPrimitive.Root>}
     </MessagePrimitive.Root>
    }}</ThreadPrimitive.Messages>
   </div>
  </ThreadPrimitive.Viewport>
  <div className="aui-thread-footer"><ThreadPrimitive.ScrollToBottom className="aui-scroll-to-bottom aui-icon-button" aria-label="滚动到最新回复"><ArrowDown size={17}/></ThreadPrimitive.ScrollToBottom>
   {error&&<div className="aui-message-error" role="alert">{error}</div>}
   <ComposerPrimitive.Root className="aui-composer-root"><div className="aui-composer-shell"><ComposerPrimitive.Input className="aui-composer-input" rows={1} maxLength={6000} placeholder="聊聊您的研究，或追问这篇论文…" aria-label="对话消息" enterKeyHint="send"/><div className="aui-composer-action-wrapper"><span>Enter 发送 · Shift + Enter 换行</span>{running?<ComposerPrimitive.Cancel className="aui-composer-cancel" aria-label="停止回复" title="停止回复"><Square size={14} fill="currentColor"/></ComposerPrimitive.Cancel>:<ComposerPrimitive.Send className="aui-composer-send" aria-label="发送消息" title="发送消息"><ArrowUp size={18}/></ComposerPrimitive.Send>}</div></div></ComposerPrimitive.Root>
  </div>
 </ThreadPrimitive.Root>
}
