import {useEffect,useRef} from 'react'

const CONTROLS='button,a,input,select,textarea,summary,label,[role="button"],[role="link"],[contenteditable]:not([contenteditable="false"])'
const OVERLAYS='.modal-overlay,dialog[open],[role="dialog"],[role="menu"]'
const BARS='.topbar,.mobile-tabs'

// One idle timer; pointer/scroll events update local values, never React state.
export function installMobileFeedChrome(root:HTMLElement) {
 const doc=root.ownerDocument,win=doc.defaultView!
 let visible=true,timer:number|undefined,lastScroll=-Infinity
 let gesture:{id:number;x:number;y:number;at:number;bar:boolean}|null=null
 const clear=()=>{if(timer!==undefined)win.clearTimeout(timer);timer=undefined}
 const setVisible=(value:boolean)=>{visible=value;root.dataset.mobileFeedChrome=value?'visible':'hidden'}
 const hasKeyboardFocus=()=>{const active=doc.activeElement;return active instanceof Element&&!!active.closest(BARS)&&active.matches(':focus-visible')}
 const schedule=()=>{clear();if(!visible||doc.hidden)return;timer=win.setTimeout(()=>{timer=undefined;if(gesture?.bar||doc.querySelector(OVERLAYS)||root.querySelector('.topbar details[open]')||hasKeyboardFocus()){schedule();return}setVisible(false)},3000)}
 const show=()=>{setVisible(true);schedule()}
 const element=(target:EventTarget|null)=>target instanceof Element?target:null
 const down=(event:PointerEvent)=>{
  const previousBar=gesture?.bar;gesture=null
  if(!event.isPrimary||event.button!==0){if(previousBar)schedule();return}
  const target=element(event.target);if(!target||target.closest(OVERLAYS))return
  const bar=!!target.closest(BARS)
  if(bar)clear()
  if(!bar&&(target.closest(CONTROLS)||target.closest('.feed-toolbar')||!target.closest('.feed-page')))return
  gesture={id:event.pointerId,x:event.clientX,y:event.clientY,at:win.performance.now(),bar}
 }
 const move=(event:PointerEvent)=>{if(gesture&&gesture.id===event.pointerId&&Math.hypot(event.clientX-gesture.x,event.clientY-gesture.y)>10){const bar=gesture.bar;gesture=null;if(bar)schedule()}}
 const cancel=()=>{const bar=gesture?.bar;gesture=null;if(bar)schedule()}
 const scrolled=()=>{lastScroll=win.performance.now();if(!gesture?.bar)gesture=null}
 const up=(event:PointerEvent)=>{
  const tap=gesture;gesture=null
  if(!tap||tap.id!==event.pointerId)return
  if(tap.bar){schedule();return}
  const target=element(event.target),now=win.performance.now()
  if(!target||target.closest(CONTROLS+','+OVERLAYS+','+BARS+',.feed-toolbar')||!target.closest('.feed-page'))return
  if(now-tap.at>450||now-lastScroll<180||Math.hypot(event.clientX-tap.x,event.clientY-tap.y)>10||doc.getSelection()?.toString())return
  clear();setVisible(!visible);schedule()
 }
 const clicked=(event:MouseEvent)=>{
  const target=element(event.target);if(!target)return
  if(target.closest('.feed-toolbar button[aria-label="刷新信息流"]'))show()
  else if(target.closest(BARS)||target.closest(OVERLAYS))schedule()
 }
 const focused=(event:FocusEvent)=>{if(element(event.target)?.closest(BARS))show()}
 const visibility=()=>{gesture=null;if(doc.hidden)clear();else show()}
 root.addEventListener('pointerdown',down,{passive:true})
 root.addEventListener('pointermove',move,{passive:true})
 root.addEventListener('pointerup',up,{passive:true})
 root.addEventListener('pointercancel',cancel,{passive:true})
 root.addEventListener('scroll',scrolled,{capture:true,passive:true})
 root.addEventListener('click',clicked,{capture:true})
 root.addEventListener('focusin',focused)
 root.addEventListener('focusout',schedule)
 doc.addEventListener('visibilitychange',visibility)
 show()
 return()=>{
  clear();gesture=null;delete root.dataset.mobileFeedChrome
  root.removeEventListener('pointerdown',down);root.removeEventListener('pointermove',move)
  root.removeEventListener('pointerup',up);root.removeEventListener('pointercancel',cancel)
  root.removeEventListener('scroll',scrolled,true);root.removeEventListener('click',clicked,true)
  root.removeEventListener('focusin',focused);root.removeEventListener('focusout',schedule)
  doc.removeEventListener('visibilitychange',visibility)
 }
}

export function useMobileFeedChrome(enabled:boolean) {
 const ref=useRef<HTMLDivElement>(null)
 useEffect(()=>{
  const root=ref.current;if(!enabled||!root)return
  const query=matchMedia('(max-width:600px)');let cleanup:(()=>void)|undefined
  const update=()=>{cleanup?.();cleanup=undefined;if(query.matches)cleanup=installMobileFeedChrome(root)}
  update();query.addEventListener('change',update)
  return()=>{query.removeEventListener('change',update);cleanup?.()}
 },[enabled])
 return ref
}
