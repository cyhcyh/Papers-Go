import {Fragment,useMemo} from 'react'
import katex from 'katex'

type Part={text:string;math?:boolean;display?:boolean}
const delimiters=[['$$','$$',true],['\\[','\\]',true],['\\(','\\)',false],['$','$',false]] as const
const escaped=(text:string,index:number)=>{let slashes=0;while(index>0&&text[--index]==='\\')slashes++;return slashes%2===1}

export function splitMath(text:string):Part[]{
 const parts:Part[]=[];let plain=0,i=0
 while(i<text.length){
  // Code examples stay literal, including while a streamed code fence is incomplete.
  if(text[i]==='`'&&!escaped(text,i)){
   let length=1;while(text[i+length]==='`')length++
   const end=text.indexOf('`'.repeat(length),i+length)
   i=end<0?text.length:end+length;continue
  }
  const delimiter=delimiters.find(([left])=>text.startsWith(left,i)&&!escaped(text,i))
  if(!delimiter){i++;continue}
  const [left,right,display]=delimiter
  let end=text.indexOf(right,i+left.length)
  while(end>=0&&escaped(text,end))end=text.indexOf(right,end+right.length)
  if(end<0){i+=left.length;continue}
  const value=text.slice(i+left.length,end)
  // Avoid interpreting ordinary prices or unmatched streamed delimiters as math.
  if(!value.trim()||(left==='$'&&(/\d/.test(text[end+1]||'')||text[end+1]==='$'))){i+=left.length;continue}
  if(i>plain)parts.push({text:text.slice(plain,i)})
  parts.push({text:value,math:true,display});i=end+right.length;plain=i
 }
 if(plain<text.length)parts.push({text:text.slice(plain)})
 return parts
}

export function MathText({children,inline=false}:{children:string|null|undefined;inline?:boolean}){
 const content=useMemo(()=>splitMath(children||'').map(part=>part.math?{...part,html:katex.renderToString(part.text,{displayMode:!!part.display&&!inline,throwOnError:false,trust:false,strict:'ignore',maxSize:20,maxExpand:1000})}:part),[children,inline])
 return <span className="math-text">{content.map((part,i)=>'html' in part?<span key={i} className={part.display&&!inline?'math-display':'math-inline'} dangerouslySetInnerHTML={{__html:part.html}}/>:<Fragment key={i}>{part.text}</Fragment>)}</span>
}
