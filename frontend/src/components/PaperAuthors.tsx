import {useLayoutEffect,useRef,useState} from 'react'

export function PaperAuthors({authors}:{authors:string[]}){
 const container=useRef<HTMLParagraphElement>(null),measure=useRef<HTMLSpanElement>(null)
 const full=authors.join(' · '),[text,setText]=useState(full)
 useLayoutEffect(()=>{
  const node=container.current,probe=measure.current;if(!node||!probe)return
  const fit=()=>{
   probe.style.width=node.clientWidth+'px'
   const style=getComputedStyle(node),limit=(parseFloat(style.lineHeight)||parseFloat(style.fontSize)*1.6)*2+.5
   const fits=(value:string)=>{probe.textContent=value;return probe.getBoundingClientRect().height<=limit}
   if(fits(full)){setText(full);return}
   let lo=0,hi=Math.max(0,authors.length-1)
   while(lo<hi){const count=Math.ceil((lo+hi)/2);if(fits(authors.slice(0,count).join(' · ')+' 等'))lo=count;else hi=count-1}
   setText((authors.slice(0,lo).join(' · ')+' 等').trim())
  }
  fit();const resize=new ResizeObserver(fit);resize.observe(node)
  let alive=true;void document.fonts.ready.then(()=>{if(alive)fit()})
  return()=>{alive=false;resize.disconnect()}
 },[full,authors])
 return <p className="paper-authors card-authors" ref={container} title={full}><span>{text}</span><span className="authors-measure" ref={measure} aria-hidden="true"/></p>
}
