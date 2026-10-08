import {useLayoutEffect,useRef} from 'react'

// Measure the actual space after the page header, filters and admin tabs.
export function useViewportPanel() {
 const ref=useRef<HTMLDivElement>(null)
 useLayoutEffect(()=>{
  const node=ref.current;if(!node)return
  const update=()=>{node.style.setProperty('--panel-height',`${Math.max(180,window.innerHeight-node.getBoundingClientRect().top-24)}px`)}
  update()
  const observer=new ResizeObserver(update)
  if(node.parentElement)observer.observe(node.parentElement)
  const header=document.querySelector('.admin-topbar')||document.querySelector('.topbar')
  if(header)observer.observe(header)
  window.addEventListener('resize',update)
  window.visualViewport?.addEventListener('resize',update)
  return()=>{observer.disconnect();window.removeEventListener('resize',update);window.visualViewport?.removeEventListener('resize',update)}
 })
 return ref
}
