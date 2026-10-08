export const disciplines=[
 ['Computer Science','计算机科学'],['Mathematics','数学'],['Statistics','统计学'],['Physics','物理学'],
 ['Electrical Engineering and Systems Science','电气工程与系统科学'],['Economics','经济学'],
 ['Quantitative Biology','定量生物学'],['Quantitative Finance','定量金融'],
] as const
export const disciplineLabel=(value?:string|null)=>disciplines.find(([key])=>key===value)?.[1]||value||'未设置学科'
export type SourceNode={key:string;kind:'arxiv'|'venue';code:string;label:string;discipline?:string;label_en?:string;label_zh?:string}
export function sourceDiscipline(source:SourceNode){
 if(source.discipline)return source.discipline
 const prefix=source.code.split('.')[0]
 return ({cs:'Computer Science',math:'Mathematics',stat:'Statistics',eess:'Electrical Engineering and Systems Science',econ:'Economics','q-bio':'Quantitative Biology','q-fin':'Quantitative Finance'} as Record<string,string>)[prefix]||(source.kind==='venue'?'Computer Science':'Physics')
}
export function sourceMatches(source:SourceNode,query:string){
 const discipline=sourceDiscipline(source)
 return [source.code,source.label,source.label_en,source.label_zh,discipline,disciplineLabel(discipline)].join(' ').toLowerCase().includes(query.trim().toLowerCase())
}
export function groupSources<T extends SourceNode>(sources:T[]){
 return disciplines.map(([key,label])=>({key,label,sources:sources.filter(s=>sourceDiscipline(s)===key)})).filter(g=>g.sources.length)
}
export const topicMatches=(topic:{name_zh:string;name_en:string},query:string)=>(topic.name_zh+' '+topic.name_en).toLowerCase().includes(query.trim().toLowerCase())
export function selectAll<T>(selection:T[],filtered:T[]){return [...new Set([...selection,...filtered])]}
export function sourceGroupState(sources:SourceNode[],selected:string[],discipline:string,disabledKeys:string[]=[]){
 const available=sources.filter(s=>sourceDiscipline(s)===discipline&&!disabledKeys.includes(s.key)).map(s=>s.key)
 const selectedCount=available.filter(key=>selected.includes(key)).length
 return {available,selectedCount,checked:available.length>0&&selectedCount===available.length,mixed:selectedCount>0&&selectedCount<available.length}
}
export function toggleSourceGroup(sources:SourceNode[],selected:string[],discipline:string,disabledKeys:string[]=[]){
 const state=sourceGroupState(sources,selected,discipline,disabledKeys)
 return state.checked?selected.filter(key=>!state.available.includes(key)):selectAll(selected,state.available)
}
