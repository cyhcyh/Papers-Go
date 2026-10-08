import {memo} from 'react'
import {ChatContent} from './ChatContent'
import './trend-content.css'

export const TrendContent=memo(function TrendContent({text,compact=false,analysis=false}:{text:string;compact?:boolean;analysis?:boolean}){
 return <div className={'trend-markdown'+(compact?' trend-markdown-compact':'')}><ChatContent text={text} presentation={analysis?'trend-analysis':undefined}/></div>
})
