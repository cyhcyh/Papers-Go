import {memo} from 'react'
import {ChatContent,type PaperReference} from './ChatContent'
import './trend-content.css'

export const TrendContent=memo(function TrendContent({text,compact=false,analysis=false,references,onOpenPaper}:{text:string;compact?:boolean;analysis?:boolean;references?:PaperReference[];onOpenPaper?:(id:number)=>void}){
 return <div className={'trend-markdown'+(compact?' trend-markdown-compact':'')}><ChatContent text={text} presentation={analysis?'trend-analysis':undefined} references={references} onOpenPaper={onOpenPaper}/></div>
})
