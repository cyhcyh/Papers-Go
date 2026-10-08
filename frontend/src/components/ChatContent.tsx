import {Fragment} from 'react'
import {MathText,splitMath} from './MathText'

type MathContext={formulas:ReturnType<typeof splitMath>;prefix:string}
function Inline({text,math}:{text:string;math:MathContext}){
 const renderText=(value:string)=>value.split(new RegExp('('+math.prefix+'\\d+\uE001)','g')).map((part,i)=>{
  const match=part.startsWith(math.prefix)?part.match(new RegExp('^'+math.prefix+'(\\d+)\uE001$')):null
  const formula=match?math.formulas[Number(match[1])]:undefined
  return <Fragment key={i}>{formula?<MathText>{(formula.display?'\\[':'\\(')+formula.text+(formula.display?'\\]':'\\)')}</MathText>:<MathText>{part}</MathText>}</Fragment>
 })
 const parts=text.split(/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\(https?:\/\/[^)]+\))/g)
 return <>{parts.map((part,i)=>{
  if(part.startsWith('**')&&part.endsWith('**'))return <strong key={i}>{renderText(part.slice(2,-2))}</strong>
  if(part.startsWith('`')&&part.endsWith('`'))return <code key={i}>{part.slice(1,-1)}</code>
  const link=part.match(/^\[([^\]]+)\]\((https?:\/\/[^)]+)\)$/)
  if(link)return <a key={i} href={link[2]} target="_blank" rel="noreferrer">{renderText(link[1])}</a>
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

export function ChatContent({text,presentation}:{text:string;presentation?:'trend-analysis'}){
 // Protect complete formulas before parsing Markdown: their newlines, stars and
 // vertical bars belong to mathematics, including inside table cells.
 let prefix='\uE000math';while(text.includes(prefix))prefix+='m'
 const math:MathContext={formulas:[],prefix}
 const protectedText=splitMath(text).map(part=>{
  if(!part.math)return part.text
  math.formulas.push(part);return prefix+(math.formulas.length-1)+'\uE001'
 }).join('')
 const lines=protectedText.split('\n'),blocks:React.ReactNode[]=[]
 for(let i=0;i<lines.length;){
  if(!lines[i].trim()){i++;continue}
  if(/^\s*```/.test(lines[i])){
   const language=lines[i].trim().slice(3),code:string[]=[];i++
   while(i<lines.length&&!/^\s*```/.test(lines[i]))code.push(lines[i++])
   if(i<lines.length)i++
   blocks.push(<pre key={blocks.length} aria-label={language?language+' 代码':'代码'}><code>{code.join('\n')}</code></pre>);continue
  }
  const heading=lines[i].match(/^#{1,4}\s+(.+)/)
  if(heading){blocks.push(<h4 key={blocks.length}><Inline text={heading[1]} math={math}/></h4>);i++;continue}
  const table=tableHeader(lines,i)
  if(table){
   const rows:string[][]=[];i+=2
   while(i<lines.length&&lines[i].trim()&&!/^\s*```|^#{1,4}\s|^\s*(?:[-*]|\d+[.)])\s/.test(lines[i])){
    const row=tableRow(lines[i]);if(!row.pipes)break;rows.push(row.cells);i++
   }
   blocks.push(<div className="chat-table-wrap" key={blocks.length} role="region" aria-label="回复表格" tabIndex={0}><table><thead><tr>{table.header.map((cell,column)=><th key={column} scope="col" style={{textAlign:table.align[column]}}><Inline text={cell} math={math}/></th>)}</tr></thead><tbody>{rows.map((row,r)=><tr key={r}>{table.header.map((_,column)=><td key={column} style={{textAlign:table.align[column]}}><Inline text={row[column]||''} math={math}/></td>)}</tr>)}</tbody></table></div>);continue
  }
  const list=lines[i].match(/^\s*(?:([-*])|\d+[.)])\s+(.+)/)
  if(list){
   const ordered=!list[1],items:React.ReactNode[]=[]
   const start=ordered?Number(lines[i].match(/^\s*(\d+)/)?.[1]||1):1
   while(i<lines.length){
    const item=lines[i].match(/^\s*(?:([-*])|\d+[.)])\s+(.+)/);if(!item||(!item[1])!==ordered)break
    const titled=presentation==='trend-analysis'&&ordered?item[2].match(/^\*\*([^*]+)\*\*\s*[：:]?\s*(.*)$/):null
    items.push(titled?<li className="trend-progress" key={items.length}><div className="trend-progress-heading"><span className="trend-progress-number">{String(start+items.length).padStart(2,'0')}</span><h4><Inline text={titled[1].replace(/[：:]$/,'')} math={math}/></h4></div>{titled[2]&&<div className="trend-progress-body"><Inline text={titled[2]} math={math}/></div>}</li>:<li key={items.length}><Inline text={item[2]} math={math}/></li>);i++
   }
   blocks.push(ordered?<ol start={start===1?undefined:start} key={blocks.length}>{items}</ol>:<ul key={blocks.length}>{items}</ul>);continue
  }
  // A streamed chunk may end at "# ", "- " or "1. ". Those prefixes
  // stop paragraph scanning but are not complete headings/list items yet.
  // Consume the current line first so every parser pass makes progress.
  const paragraph:string[]=[lines[i++]]
  while(i<lines.length&&lines[i].trim()&&!/^\s*```|^#{1,4}\s|^\s*(?:[-*]|\d+[.)])\s/.test(lines[i])&&!tableHeader(lines,i))paragraph.push(lines[i++])
  const overview=presentation==='trend-analysis'&&blocks.length===0
  blocks.push(<p className={'pre-wrap'+(overview?' trend-summary':'')} key={blocks.length}>{overview&&<span className="trend-summary-label">本期概述</span>}<Inline text={paragraph.join('\n')} math={math}/></p>)
 }
 return <div className="chat-content">{blocks}</div>
}
