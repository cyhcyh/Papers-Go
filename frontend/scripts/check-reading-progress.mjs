import assert from 'node:assert/strict'
import {mkdir} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import {createElement} from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {build} from 'esbuild'

const output=new URL('../node_modules/.cache/reading-progress-check/',import.meta.url)
await mkdir(output,{recursive:true})
for(const name of ['ChatContent','ReadingProgress'])await build({entryPoints:[fileURLToPath(new URL(`../src/components/${name}.tsx`,import.meta.url))],outfile:fileURLToPath(new URL(`${name}.mjs`,output)),bundle:true,platform:'node',format:'esm',packages:'external',jsx:'automatic'})
const {ChatContent}=await import(new URL('ChatContent.mjs',output))
const render=text=>renderToStaticMarkup(createElement(ChatContent,{text}))
const text=String.raw`独立公式：

$$
\begin{aligned}
a&=b+c \\

d&=e+f
\end{aligned}
$$

| 数学 | 说明 |
| --- | --- |
| $|x|+\|y\|$ | **范数** |
| $\text{**literal**}$ | $ x^2 $ |

`+'`$code$` 保留代码。'
const html=render(text)
assert.equal((html.match(/class="katex-mathml"/g)||[]).length,4)
assert.equal((html.match(/<td /g)||[]).length,4)
assert.doesNotMatch(html,/katex-error|\uE000|\uE001/)
assert.match(html,/<code>\$code\$/)
const steps=render('1. 第一步\n\n$$x^2$$\n\n2. 第二步\n\n$$y^2$$\n\n3. 第三步')
assert.match(steps,/<ol start="2"/);assert.match(steps,/<ol start="3"/)
for(let i=0;i<=text.length;i++)assert.doesNotThrow(()=>render(text.slice(0,i)))
const {ReadingProgress}=await import(new URL('ReadingProgress.mjs',output))
const clock=Date.parse('2026-10-03T00:01:00Z')
const progress=values=>renderToStaticMarkup(createElement(ReadingProgress,{clock,progress:values}))
const queued=progress({stage:'queued',queued_at:'2026-10-03T00:00:00Z',queue_ahead:3})
assert.match(queued,/前面还有 3 位/);assert.match(queued,/已等待 1 分 0 秒/);assert.match(queued,/关闭后仍会继续/);assert.doesNotMatch(queued,/<progress/)
const thinking=progress({stage:'thinking',started_at:'2026-10-03T00:00:25Z'})
assert.match(thinking,/模型正在思考/);assert.match(thinking,/已用 0 分 35 秒/);assert.doesNotMatch(thinking,/<progress|%|预算/)
const generating=progress({stage:'generating',answers_completed:2,answers_total:6})
assert.match(generating,/已完成 2\/6 个问题/);assert.match(generating,/<progress max="6" value="2"/)
console.log('Reading progress checks passed: queue positions, thinking duration, completed questions, multiline math, table norms and streamed prefixes.')
