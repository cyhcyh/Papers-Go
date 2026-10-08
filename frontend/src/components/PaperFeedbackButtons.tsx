import {useEffect,useState} from 'react'
import {Heart,Bookmark} from 'lucide-react'
import type {InteractionResult,Paper} from '../types'

export type PaperFeedbackAction='like'|'save'|'remove_like'|'remove_save'
export function PaperFeedbackButtons({paper,onAction,disabled=false}:{paper:Paper;onAction:(paper:Paper,action:PaperFeedbackAction)=>Promise<void>;disabled?:boolean}){
 const [update,setUpdate]=useState<InteractionResult|null>(null),[pending,setPending]=useState(false)
 useEffect(()=>setUpdate(null),[paper.id,paper.like_count,paper.save_count,paper.liked,paper.saved])
 useEffect(()=>{const changed=(event:Event)=>{const state=(event as CustomEvent<InteractionResult>).detail;if(state.paper_id===paper.id)setUpdate(state)};window.addEventListener('paper-feedback',changed);return()=>window.removeEventListener('paper-feedback',changed)},[paper.id])
 const current=update?{...paper,like_count:update.like_count,save_count:update.save_count,liked:update.liked,saved:update.saved,expires_at:update.expires_at}:paper
 const act=async(action:PaperFeedbackAction)=>{if(pending||disabled)return;setPending(true);try{await onAction(current,action)}finally{setPending(false)}}
 return <><button disabled={disabled||pending} aria-label={current.liked?'取消喜欢':'喜欢论文'} aria-pressed={!!current.liked} title={`告诉系统您偏好这类研究 · ${current.like_count||0} 人喜欢`} className={current.liked?'feedback-button is-active':'feedback-button'} onClick={()=>void act(current.liked?'remove_like':'like')}><Heart size={16} fill={current.liked?'currentColor':'none'}/><span>{current.like_count||'喜欢'}</span></button><button disabled={disabled||pending} aria-label={current.saved?'取消收藏':'收藏论文'} aria-pressed={!!current.saved} title={`保存到书架，方便以后阅读和追踪 · ${current.save_count||0} 人收藏`} className={current.saved?'feedback-button is-active':'feedback-button'} onClick={()=>void act(current.saved?'remove_save':'save')}><Bookmark size={16} fill={current.saved?'currentColor':'none'}/><span>{current.save_count||'收藏'}</span></button></>
}
