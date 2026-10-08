import assert from 'node:assert/strict'
import {mkdir} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import {spawnSync} from 'node:child_process'
import {build} from 'esbuild'

const output=new URL('../node_modules/.cache/chat-check/ChatContent.mjs',import.meta.url)
const table='下面比较方法：\n| 方法 | 复杂度 | 说明 |\n| :--- | :---: | ---: |\n| **检索** | $O(n)$ | `a\\|b` 与 A\\|B |\n| [原文](https://arxiv.org/abs/2609.12345) | $x^2$ | <img src=x> |\n\n最后保留正文。'
const unevenTable='名称 | 结果\n--- | ---\n短行 |\n完整行 | 保留 | 忽略多余单元格'
const codeTable='```text\n| A | B |\n| --- | --- |\n| C | D |\n```'
await mkdir(new URL('.',output),{recursive:true})
await build({entryPoints:[fileURLToPath(new URL('../src/components/ChatContent.tsx',import.meta.url))],outfile:fileURLToPath(output),bundle:true,platform:'node',format:'esm',packages:'external',jsx:'automatic'})
// A non-advancing parser used to exhaust the browser heap. Run these checks in
// a small, time-bounded child process so a regression cannot hang the test host.
const check=`
 import assert from 'node:assert/strict';
 import {createElement} from 'react';
 import {renderToStaticMarkup} from 'react-dom/server';
 import {ChatContent} from ${JSON.stringify(output.href)};
 const render=text=>renderToStaticMarkup(createElement(ChatContent,{text}));
 const incomplete=['# ','## ','### ','#### ','- ','* ','1. ','2) ','  - ','标题\\n\\n- ','正文\\n## '];
 for(const text of incomplete){console.log('prefix',JSON.stringify(text));assert.doesNotThrow(()=>render(text));}
 const answer=${JSON.stringify('## 研究方法\n\n先比较**图结构**。\n\n1. 用 $O(n)$ 描述复杂度。\n2. 检查结论。\n\n- 第一个方法\n- 第二个方法\n\n```python\nprint("research")\n```\n\n### 主要结果\n最终保留公式 $x^2$ 与 [原文](https://arxiv.org/abs/2609.12345)。')};
 for(let n=0;n<=answer.length;n++)assert.doesNotThrow(()=>render(answer.slice(0,n)));
 const html=render(answer);assert.match(html,/<h4>/);assert.match(html,/研究方法/);assert.match(html,/<ol>/);assert.match(html,/<ul>/);assert.match(html,/katex-mathml/);assert.match(html,/<pre/);assert.match(html,/href="https:\\/\\/arxiv.org/);
 assert.match(render('<img src=x onerror=alert(1)>'),/&lt;img/);
 const table=${JSON.stringify(table)}, unevenTable=${JSON.stringify(unevenTable)};
 for(let n=0;n<=table.length;n++)assert.doesNotThrow(()=>render(table.slice(0,n)));
 const tableHtml=render(table);assert.match(tableHtml,/<table>/);assert.match(tableHtml,/<thead>/);assert.equal((tableHtml.match(/<th /g)||[]).length,3);assert.equal((tableHtml.match(/<td /g)||[]).length,6);assert.match(tableHtml,/text-align:center/);assert.match(tableHtml,/text-align:right/);assert.match(tableHtml,/<strong>/);assert.match(tableHtml,/katex-mathml/);assert.match(tableHtml,/<code>a\\|b/);assert.match(tableHtml,/A\\|B/);assert.match(tableHtml,/&lt;img/);assert.match(tableHtml,/最后保留正文/);
 const uneven=render(unevenTable);assert.equal((uneven.match(/<td /g)||[]).length,4);assert.doesNotMatch(uneven,/忽略多余单元格/);
 assert.doesNotMatch(render('A | B\\n--- | --- | ---\\nC | D'),/<table>/);
 assert.doesNotMatch(render(${JSON.stringify(codeTable)}),/<table>/);
 assert.doesNotMatch(render('|\\n|'),/<table>/);
 console.log('Chat rendering checks passed: every streamed prefix, Markdown tables/alignment/escaped pipes, uneven rows, math, code and escaped HTML.');
`
const result=spawnSync(process.execPath,['--max-old-space-size=96','--input-type=module','-e',check],{cwd:fileURLToPath(new URL('..',import.meta.url)),timeout:10000,encoding:'utf8',maxBuffer:128*1024})
assert.equal(result.status,0,`Chat renderer did not finish (status=${result.status}, signal=${result.signal}).\n${result.stdout}\n${result.stderr?.slice(-1400)}`)
console.log(result.stdout.trim().split('\n').at(-1))
