// Adapted from shadcn/ui Pagination (MIT):
// https://github.com/shadcn-ui/ui/blob/main/apps/v4/registry/new-york-v4/ui/pagination.tsx
// Native buttons support disabled states and in-place table pagination.
import {ChevronLeft,ChevronRight,MoreHorizontal} from 'lucide-react'

export function Pagination({page,pages,total,onChange,label='主题',pageSize=20}:{page:number;pages:number;total:number;onChange:(page:number)=>void;label?:string;pageSize?:number}){
 const numbers=[...new Set([1,page-1,page,page+1,pages])].filter(n=>n>=1&&n<=pages).sort((a,b)=>a-b)
 return <nav className="topic-pagination" aria-label={label+'列表分页'} data-slot="pagination"><span>共 {total} 条 · 每页 {pageSize} 条</span><ul data-slot="pagination-content"><li><button aria-label={'上一页'+label} disabled={page<=1} onClick={()=>onChange(page-1)}><ChevronLeft size={16}/></button></li>{numbers.flatMap((n,i)=>[...(i&&n-numbers[i-1]>1?[<li key={'gap'+n}><span className="pagination-ellipsis" aria-label="更多页"><MoreHorizontal size={16}/></span></li>]:[]),<li key={n}><button aria-label={'第'+n+'页'+label} aria-current={n===page?'page':undefined} onClick={()=>onChange(n)}>{n}</button></li>])}<li><button aria-label={'下一页'+label} disabled={page>=pages} onClick={()=>onChange(page+1)}><ChevronRight size={16}/></button></li></ul></nav>
}
