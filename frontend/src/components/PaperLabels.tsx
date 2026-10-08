import {useLayoutEffect,useRef,useState} from 'react'
import type {Paper} from '../types'

export function paperSourceCode(paper:Paper){
 return paper.venue?.split('.')[0]||paper.primary_category||'arXiv'
}

export function TopicTag({name}:{name:string}){
 const outer=useRef<HTMLSpanElement>(null),inner=useRef<HTMLSpanElement>(null),[distance,setDistance]=useState(0)
 useLayoutEffect(()=>{const update=()=>{const node=outer.current;if(!node)return;const style=getComputedStyle(node);setDistance(Math.max(0,(inner.current?.scrollWidth||0)-node.clientWidth+parseFloat(style.paddingLeft)+parseFloat(style.paddingRight)))};update();const observer=new ResizeObserver(update);if(outer.current)observer.observe(outer.current);return()=>observer.disconnect()},[name])
 return <span ref={outer} className={'tag topic-tag'+(distance?' topic-tag-long':'')} title={name} aria-label={name} tabIndex={distance?0:undefined} style={{'--tag-distance':`${-distance}px`,'--tag-duration':`${Math.max(8,distance/22+4)}s`} as React.CSSProperties}><span ref={inner} className="topic-tag-text" aria-hidden="true">{name}</span></span>
}
