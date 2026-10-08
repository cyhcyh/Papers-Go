import assert from 'node:assert/strict'
import {mkdir} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import {build} from 'esbuild'

const output=new URL('../node_modules/.cache/discipline-check/helpers.mjs',import.meta.url)
await mkdir(new URL('.',output),{recursive:true})
await build({entryPoints:[fileURLToPath(new URL('../src/disciplines.ts',import.meta.url))],outfile:fileURLToPath(output),bundle:true,platform:'node',format:'esm'})
const {disciplines,groupSources,sourceMatches,selectAll,sourceDiscipline,topicMatches,sourceGroupState,toggleSourceGroup}=await import(output.href)
const sources=[
 {key:'arxiv:cs.AI',kind:'arxiv',code:'cs.AI',label:'人工智能',label_en:'Artificial Intelligence',discipline:'Computer Science'},
 {key:'arxiv:math.CO',kind:'arxiv',code:'math.CO',label:'组合数学',label_en:'Combinatorics',discipline:'Mathematics'},
 {key:'arxiv:stat.ML',kind:'arxiv',code:'stat.ML',label:'统计机器学习',label_en:'Machine Learning',discipline:'Statistics'},
 {key:'venue:ECONCONF',kind:'venue',code:'ECONCONF',label:'经济会议',discipline:'Economics'},
 {key:'arxiv:quant-ph',kind:'arxiv',code:'quant-ph',label:'量子物理',label_en:'Quantum Physics',discipline:'Physics'},
]
assert.equal(disciplines.length,8)
assert.equal(groupSources(sources).length,5)
assert.deepEqual(groupSources(sources).find(g=>g.key==='Statistics').sources.map(s=>s.code),['stat.ML'])
for(const term of ['MATH.co','组合','Combinatorics','数学','Mathematics'])assert(sourceMatches(sources[1],term))
assert(!sourceMatches(sources[0],'组合数学'))
assert.equal(sourceDiscipline({...sources[2],discipline:undefined}),'Statistics')
assert.equal(sourceDiscipline(sources[3]),'Economics')
assert.equal(sourceDiscipline({...sources[4],discipline:undefined}),'Physics')
assert(topicMatches({name_zh:'量子纠缠',name_en:'Quantum Entanglement'},'entanglement'))
assert(topicMatches({name_zh:'量子纠缠',name_en:'Quantum Entanglement'},'纠缠'))
const all=Array.from({length:601},(_,i)=>i+1)
assert.equal(selectAll([],all).length,601)
assert.deepEqual(selectAll([4,2],all.filter(i=>i<4)),[4,2,1,3])
assert.deepEqual(selectAll([1,500],[2,3]),[1,500,2,3])
const componentOutput=new URL('../node_modules/.cache/discipline-check/components.mjs',import.meta.url)
await build({stdin:{contents:"export {CategoryTree} from './components/CategoryTree'; export {CategoryPicker} from './components/CategoryPicker'; export {SourcePicker} from './components/SourcePicker'; export {SourceTable} from './components/SourceTable'",resolveDir:fileURLToPath(new URL('../src',import.meta.url))},outfile:fileURLToPath(componentOutput),bundle:true,platform:'node',format:'esm',external:['react','react-dom','lucide-react'],jsx:'automatic'})
const {CategoryTree,CategoryPicker,SourcePicker,SourceTable}=await import(componentOutput.href)
const {createElement}=await import('react'),{renderToStaticMarkup}=await import('react-dom/server')
const catalog=sources.map(s=>({...s,group:'',paper_count:10,topics:[{id:s.code==='math.CO'?10:20,name_zh:s.code==='math.CO'?'极值图论':'其它主题',name_en:'Test topic',parent_id:null}]}))
const tree=renderToStaticMarkup(createElement(CategoryTree,{catalog,category:null,topic:null,onSelect:()=>{},search:'组合数学'}))
assert(tree.includes('math.CO')&&tree.includes('极值图论')&&!tree.includes('cs.AI'))
assert(tree.includes('aria-expanded="true"'))
const form=renderToStaticMarkup(createElement(CategoryPicker,{catalog,selection:{categories:['arxiv:math.CO'],topics:{},weights:{'arxiv:math.CO':1}},onChange:()=>{}}))
assert(!form.includes('关注分类 math.CO')&&!form.includes('aria-expanded="true"'))
assert(form.includes('已选分类 1 · 主题 1')&&form.includes('已选 1 类'))
for(const label of ['计算机科学','数学','统计学','物理学','经济学'])assert(form.includes(label))
const picker=renderToStaticMarkup(createElement(SourcePicker,{sources,selected:['arxiv:quant-ph'],onChange:()=>{},multiple:false}))
assert(!picker.includes('type="radio"')&&!picker.includes('aria-expanded="true"')&&picker.includes('已选 quant-ph'))
const additional=[...sources,{...sources[1],key:'arxiv:math.NT',code:'math.NT',label:'数论'},{...sources[1],key:'arxiv:math.PR',code:'math.PR',label:'概率论'}]
const selected=['arxiv:cs.AI','arxiv:math.NT'],disabled=['arxiv:math.CO']
const partial=sourceGroupState(additional,selected,'Mathematics',disabled)
assert(partial.mixed&&!partial.checked&&partial.available.length===2)
const entire=toggleSourceGroup(additional,selected,'Mathematics',disabled)
assert.deepEqual(entire,['arxiv:cs.AI','arxiv:math.NT','arxiv:math.PR'])
assert(sourceGroupState(additional,entire,'Mathematics',disabled).checked)
assert.deepEqual(toggleSourceGroup(additional,entire,'Mathematics',disabled),['arxiv:cs.AI'])
// Group selection uses all sources even when searching would show only one.
assert.equal(additional.filter(s=>sourceMatches(s,'math.NT')).length,1)
assert.equal(toggleSourceGroup(additional,[], 'Mathematics',disabled).length,2)
const groupedPicker=renderToStaticMarkup(createElement(SourcePicker,{sources:additional,selected,onChange:()=>{},selectGroups:true,disabledKeys:disabled}))
assert(groupedPicker.includes('全选数学全部分类（2个可添加）')&&groupedPicker.includes('aria-checked="mixed"'))
assert(!groupedPicker.includes('选择分类 math.CO')&&groupedPicker.includes('包含搜索未显示的分类'))
const tableProps={sources:additional,pageItems:groupSources(additional).flatMap(g=>g.sources),selected:[],busy:false,search:'',onSelectPage:()=>{},onSelect:()=>{},onEdit:()=>{},onDelete:()=>{},onConfig:()=>{}}
const table=renderToStaticMarkup(createElement(SourceTable,tableProps))
assert.equal((table.match(/scope="rowgroup"/g)||[]).length,5)
assert(!table.includes('选择分类：cs.AI')&&!table.includes('aria-expanded="true"'))
assert(table.includes('展开数学来源')&&table.includes('全选当前页分类'))
const searchedTable=renderToStaticMarkup(createElement(SourceTable,{...tableProps,search:'分类'}))
assert(searchedTable.includes('选择分类：cs.AI')&&searchedTable.includes('收起数学来源'))
console.log('Discipline checks passed: grouping, bilingual search, default collapsed trees, selection summaries, whole-discipline selection, mixed state, and selection above 500.')
