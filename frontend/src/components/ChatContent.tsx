import {Fragment} from 'react'
import {MathText,splitMath} from './MathText'

export type PaperReference={id:number|string;title:string;url?:string;direction?:string}
type MathContext={formulas:ReturnType<typeof splitMath>;prefix:string;references?:Map<string,PaperReference>;sources?:Map<string,PaperReference>;onOpenPaper?:(id:number)=>void}
function Inline({text,math}:{text:string;math:MathContext}){
 const renderText=(value:string)=>value.split(new RegExp('('+math.prefix+'\\d+\uE001)','g')).map((part,i)=>{
  const match=part.startsWith(math.prefix)?part.match(new RegExp('^'+math.prefix+'(\\d+)\uE001$')):null
  const formula=match?math.formulas[Number(match[1])]:undefined
  return <Fragment key={i}>{formula?<MathText>{(formula.display?'\\[':'\\(')+formula.text+(formula.display?'\\]':'\\)')}</MathText>:<MathText>{part}</MathText>}</Fragment>
 })
 const shortPaperLabel=(label:string)=>{
  const plain=label.replace(/\*\*/g,'').replace(/\s+/g,' ').trim(),characters=Array.from(plain)
  // Formula placeholders must stay intact. They can wrap with the inline text.
  if(characters.length<=48||plain.includes(math.prefix))return label
  let value=characters.slice(0,47).join('')
  const boundary=value.lastIndexOf(' ')
  if(boundary>=24)value=value.slice(0,boundary)
  const shortened=value.trimEnd()+'…'
  return label.startsWith('**')&&label.endsWith('**')?'**'+shortened+'**':shortened
 }
 const paperLink=(paper:PaperReference|undefined,label:string,key:number)=>{
  const id=Number(paper?.id)
  if(!paper||!Number.isSafeInteger(id)||id<=0||!math.onOpenPaper)return <Fragment key={key}><Inline text={label} math={math}/></Fragment>
  return <span role="button" tabIndex={0} aria-haspopup="dialog" aria-label={paper.title} className="trend-paper-reference" title={paper.title} onClick={()=>math.onOpenPaper?.(id)} onKeyDown={event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();math.onOpenPaper?.(id)}}} key={key}><Inline text={shortPaperLabel(label)} math={math}/></span>
 }
 const parts=text.split(math.references?/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\((?:https?:\/\/[^)]+|paper:\d+)\))/g:/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\(https?:\/\/[^)]+\))/g)
 return <>{parts.map((part,i)=>{
  if(part.startsWith('**')&&part.endsWith('**'))return <strong key={i}>{math.references?<Inline text={part.slice(2,-2)} math={math}/>:renderText(part.slice(2,-2))}</strong>
  if(part.startsWith('`')&&part.endsWith('`'))return <code key={i}>{part.slice(1,-1)}</code>
  const citation=math.references?part.match(/^\[([^\]]+)\]\(paper:(\d+)\)$/):null
  if(citation){
   const paper=math.references?.get(String(Number(citation[2])))
   return paperLink(paper,citation[1],i)
  }
  const link=part.match(/^\[([^\]]+)\]\((https?:\/\/[^)]+)\)$/)
  if(link){
   const paper=math.sources?.get(link[2])
   return paper?paperLink(paper,link[1],i):<a key={i} href={link[2]} target="_blank" rel="noreferrer">{renderText(link[1])}</a>
  }
  return <Fragment key={i}>{renderText(part)}</Fragment>
 })}</>
}

// GFM pipe tables allow optional outer pipes and escaped pipes in cells.
function tableRow(line:string){
 const value=line.trim(),cells:string[]=[];let cell='',pipes=0,lastPipe=false
 for(let i=0;i<value.length;i++){
  const char=value[i];lastPipe=false
  if(char==='\\'&&i+1<value.length){const next=value[++i];cell+=next==='|'?'|':'\\'+next;continue}
  if(char==='|'){cells.push(cell.trim());cell='';pipes++;lastPipe=true}else cell+=char
 }
 cells.push(cell.trim())
 if(value.startsWith('|'))cells.shift()
 if(lastPipe)cells.pop()
 return {cells,pipes}
}

function tableHeader(lines:string[],i:number){
 if(i+1>=lines.length)return null
 const header=tableRow(lines[i]),separator=tableRow(lines[i+1])
 if(!header.pipes||!header.cells.length||header.cells.length!==separator.cells.length||!separator.cells.every(cell=>/^:?-+:?$/.test(cell)))return null
 const align=separator.cells.map(cell=>cell.startsWith(':')&&cell.endsWith(':')?'center':cell.endsWith(':')?'right':'left') as ('left'|'center'|'right')[]
 return {header:header.cells,align}
}

export function ChatContent({text,presentation,references,onOpenPaper}:{text:string;presentation?:'trend-analysis';references?:PaperReference[];onOpenPaper?:(id:number)=>void}){
 // Protect complete formulas before parsing Markdown: their newlines, stars and
 // vertical bars belong to mathematics, including inside table cells.
 let prefix='\uE000math';while(text.includes(prefix))prefix+='m'
 const papers=presentation==='trend-analysis'?new Map((references||[]).map(p=>[String(p.id),p])):undefined
 const math:MathContext={formulas:[],prefix,references:papers,sources:papers?new Map([...papers.values()].filter(p=>p.url).map(p=>[p.url!,p])):undefined,onOpenPaper}
 const protectedText=splitMath(text).map(part=>{
  if(!part.math)return part.text
  math.formulas.push(part);return prefix+(math.formulas.length-1)+'\uE001'
 }).join('')
 const lines=protectedText.split('\n'),blocks:React.ReactNode[]=[]
 let activeTrend:{number:number;title:string;body:React.ReactNode[]}|undefined
 let trendNumber=0,blockKey=0
 const trendTitle=(line:string)=>presentation==='trend-analysis'?line.match(/^\s*\d+[.)]\s+\*\*([^*]+)\*\*\s*[：:。.]?\s*(.*)$/):null
 const append=(block:React.ReactNode)=>{if(activeTrend)activeTrend.body.push(block);else blocks.push(block)}
 const flushTrend=()=>{
  if(!activeTrend)return
  const point=activeTrend;activeTrend=undefined
  blocks.push(<ol start={point.number===1?undefined:point.number} key={blockKey++}><li className="trend-progress"><div className="trend-progress-heading"><span className="trend-progress-number">{String(point.number).padStart(2,'0')}</span><h4><Inline text={point.title} math={math}/></h4></div>{point.body.length>0&&<div className="trend-progress-body">{point.body}</div>}</li></ol>)
 }
 for(let i=0;i<lines.length;){
  if(!lines[i].trim()){i++;continue}
  const titled=trendTitle(lines[i])
  if(titled){
   flushTrend();activeTrend={number:++trendNumber,title:titled[1].replace(/[：:]$/,''),body:[]}
   if(titled[2])append(<p key={blockKey++}><Inline text={titled[2]} math={math}/></p>)
   i++;continue
  }
  if(/^\s*```/.test(lines[i])){
   const language=lines[i].trim().slice(3),code:string[]=[];i++
   while(i<lines.length&&!/^\s*```/.test(lines[i]))code.push(lines[i++])
   if(i<lines.length)i++
   append(<pre key={blockKey++} aria-label={language?language+' 代码':'代码'}><code>{code.join('\n')}</code></pre>);continue
  }
  const heading=lines[i].match(/^#{1,4}\s+(.+)/)
  if(heading){append(<h4 key={blockKey++}><Inline text={heading[1]} math={math}/></h4>);i++;continue}
  const table=tableHeader(lines,i)
  if(table){
   const rows:string[][]=[];i+=2
   while(i<lines.length&&lines[i].trim()&&!/^\s*```|^#{1,4}\s|^\s*(?:[-*]|\d+[.)])\s/.test(lines[i])){
    const row=tableRow(lines[i]);if(!row.pipes)break;rows.push(row.cells);i++
   }
   append(<div className="chat-table-wrap" key={blockKey++} role="region" aria-label="回复表格" tabIndex={0}><table><thead><tr>{table.header.map((cell,column)=><th key={column} scope="col" style={{textAlign:table.align[column]}}><Inline text={cell} math={math}/></th>)}</tr></thead><tbody>{rows.map((row,r)=><tr key={r}>{table.header.map((_,column)=><td key={column} style={{textAlign:table.align[column]}}><Inline text={row[column]||''} math={math}/></td>)}</tr>)}</tbody></table></div>);continue
  }
  const list=lines[i].match(/^\s*(?:([-*])|\d+[.)])\s+(.+)/)
  if(list){
   const ordered=!list[1],items:React.ReactNode[]=[]
   const start=ordered?Number(lines[i].match(/^\s*(\d+)/)?.[1]||1):1
   while(i<lines.length){
    if(trendTitle(lines[i]))break
    const item=lines[i].match(/^\s*(?:([-*])|\d+[.)])\s+(.+)/);if(!item||(!item[1])!==ordered)break
    items.push(<li key={items.length}><Inline text={item[2]} math={math}/></li>);i++
   }
   append(ordered?<ol start={start===1?undefined:start} key={blockKey++}>{items}</ol>:<ul key={blockKey++}>{items}</ul>);continue
  }
  // A streamed chunk may end at "# ", "- " or "1. ". Those prefixes
  // stop paragraph scanning but are not complete headings/list items yet.
  // Consume the current line first so every parser pass makes progress.
  const paragraph:string[]=[lines[i++]]
  while(i<lines.length&&lines[i].trim()&&!/^\s*```|^#{1,4}\s|^\s*(?:[-*]|\d+[.)])\s/.test(lines[i])&&!tableHeader(lines,i))paragraph.push(lines[i++])
  const overview=presentation==='trend-analysis'&&!activeTrend&&blocks.length===0
  append(<p className={'pre-wrap'+(overview?' trend-summary':'')} key={blockKey++}>{overview&&<span className="trend-summary-label">本期概述</span>}<Inline text={paragraph.join('\n')} math={math}/></p>)
 }
 flushTrend()
 return <div className="chat-content">{blocks}</div>
}
