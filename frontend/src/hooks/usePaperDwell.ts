import {useEffect,useRef} from 'react'
import {api} from '../api'
import {useApp} from '../context'
import {PaperDwell} from './dwell'

const pending=new Set<string>()
export function usePaperDwell(paperId:number|null,context:string,modal=false){
 const {auth}=useApp(),tracker=useRef<PaperDwell|null>(null)
 useEffect(()=>{
  if(!auth||!paperId)return
  const user=auth.user.id,day=new Date().toLocaleDateString('en-CA',{timeZone:'Asia/Shanghai'}),key=user+':'+day+':'+paperId
  const dwell=new PaperDwell((id,ms)=>{
   if(pending.has(key))return
   pending.add(key)
   void api<{recorded?:boolean}>('/interactions','POST',{paper_id:id,action:'view',dwell_ms:ms,feed_context:context}).then(result=>{if(result.recorded!==false)window.dispatchEvent(new Event('stats-change'))}).catch(()=>{}).finally(()=>pending.delete(key))
  });tracker.current=dwell
  const update=()=>{
   const dialogs=document.querySelectorAll('[role="dialog"]').length
   const available=document.visibilityState==='visible'&&document.hasFocus()&&(modal?dialogs===1:dialogs===0)
   dwell.select(available?paperId:null)
  }
  update();document.addEventListener('visibilitychange',update);window.addEventListener('focus',update);window.addEventListener('blur',update)
  const observer=new MutationObserver(update);observer.observe(document.body,{childList:true,subtree:true})
  return()=>{dwell.dispose();observer.disconnect();document.removeEventListener('visibilitychange',update);window.removeEventListener('focus',update);window.removeEventListener('blur',update);if(tracker.current===dwell)tracker.current=null}
 },[paperId,auth?.user.id,context,modal])
 return (id:number)=>tracker.current?.elapsed(id)||0
}
