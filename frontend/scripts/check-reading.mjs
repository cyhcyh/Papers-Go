import assert from 'node:assert/strict'
import {mkdir} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import {createElement} from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {build} from 'esbuild'

const output=new URL('../node_modules/.cache/reading-check/',import.meta.url)
await mkdir(output,{recursive:true})
for(const name of ['ReadingCardContent','JobProgress'])await build({entryPoints:[fileURLToPath(new URL(`../src/components/${name}.tsx`,import.meta.url))],outfile:fileURLToPath(new URL(`${name}.mjs`,output)),bundle:true,platform:'node',format:'esm',packages:'external',jsx:'automatic'})
const {ReadingCardContent}=await import(new URL('ReadingCardContent.mjs',output))
const {JobProgress}=await import(new URL('JobProgress.mjs',output))
const card={tldr:'总结',method_summary:'方法',key_results:[{claim:'结果',verified:true,evidence:{section:'page 3',quote:'Evidence in paper'}}],limitations:['限定条件 $n>2$','仅适用于给定图类'],read_priority:'worth_reading',reading_level:'L2',paper_kind:'theoretical',answers:{problem:'一个**研究问题**。',related_work:'- 相关方法一\n- 相关方法二',method:'使用公式 $x^2$。\n\n<img src=x onerror=alert(1)>',evaluation:'| 定理 | 条件 |\n| --- | --- |\n| 结论 | $n>2$ |',future:'作者明确提出的方向：未提及。\n\n基于论文的进一步建议：放宽条件。',summary:'六问总结。'}}
const html=renderToStaticMarkup(createElement(ReadingCardContent,{card}))
assert.equal((html.match(/class="reading-question"/g)||[]).length,6)
for(let i=1;i<=6;i++)assert.match(html,new RegExp(`class="reading-question-number">${i}、</span>`))
for(const title of ['研究问题','已有研究与本文定位','核心方法','关键结果与证据','后续研究方向','核心总结'])assert.ok(html.includes(title))
assert.doesNotMatch(html.replace(/<[^>]*>/g,''),/Q[1-6]|L[23]/);assert.match(html,/论文精读/)
assert.match(renderToStaticMarkup(createElement(ReadingCardContent,{card:{...card,reading_level:'L3'}})),/深度解析/)
assert.match(html,/<table>/);assert.match(html,/katex-mathml/);assert.match(html,/&lt;img/);assert.doesNotMatch(html,/<img src=x/)
assert.match(html,/<details/);assert.match(html,/引文已在全文回验/)
const limitations=html.match(/<ol class="reading-limitations">([\s\S]*?)<\/ol>/)?.[1]
assert.ok(limitations);assert.equal((limitations.match(/<li>/g)||[]).length,2);assert.match(limitations,/katex-mathml/)
const old=renderToStaticMarkup(createElement(ReadingCardContent,{card:{...card,answers:null}}))
assert.match(old,/方法要点/);assert.doesNotMatch(old,/class="reading-question-number"/)
const partial=renderToStaticMarkup(createElement(ReadingCardContent,{card:{...card,answers:{problem:'正文开始出现',related_work:'',method:'',evaluation:'',future:'',summary:''}},generating:true}))
assert.match(partial,/正文开始出现/);assert.match(partial,/生成中/);assert.doesNotMatch(partial,/class="reading-question-number">2、|引文已在全文回验/)
const interrupted=renderToStaticMarkup(createElement(ReadingCardContent,{card,generating:true,failed:true}))
assert.match(interrupted,/未完成的内容/);assert.doesNotMatch(interrupted,/引文已在全文回验/)
const stages=[{key:'embedding',label:'论文向量',total:120,completed:120,failed:0,pending:0,model:'embedding-model',unit:'篇'},{key:'quality',label:'质量评估',total:120,completed:20,failed:1,pending:99,model:'quality-model',unit:'篇',current_paper:{id:2,title:'Current paper'}}]
const progress=renderToStaticMarkup(createElement(JobProgress,{progress:{total:240,completed:140,failed:1,pending:99,stage:'quality',stages},running:true}))
assert.equal((progress.match(/<progress/g)||[]).length,1);assert.match(progress,/质量评估/);assert.doesNotMatch(progress,/论文向量进度/);assert.match(progress,/Current paper/);assert.match(progress,/失败 1/)
console.log('Reading checks passed: six questions, theory wording, legacy cards, Markdown tables, formulas, escaped HTML, evidence and current task stage.')
