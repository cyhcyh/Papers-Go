import assert from 'node:assert/strict'
import {mkdir} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import {spawnSync} from 'node:child_process'
import {build} from 'esbuild'

const output=new URL('../.checks/trends/TrendContent.mjs',import.meta.url)
await mkdir(new URL('.',output),{recursive:true})
await build({entryPoints:[fileURLToPath(new URL('../src/components/TrendContent.tsx',import.meta.url))],outfile:fileURLToPath(output),bundle:true,platform:'node',format:'esm',packages:'external',jsx:'automatic',loader:{'.css':'empty'}})
const analysis='本期研究有两条主线。\n\n1. **结构条件与边界**：给定条件下研究 $R(s,t)$ 的下界。\n2. **构造方法**：比较方法的适用范围。\n\n- 结论适用于所研究的参数。'
const check=`
 import assert from 'node:assert/strict';
 import {createElement} from 'react';
 import {renderToStaticMarkup} from 'react-dom/server';
 import {TrendContent} from ${JSON.stringify(output.href)};
 const render=(text,compact=false)=>renderToStaticMarkup(createElement(TrendContent,{text,compact}));
 const html=render(${JSON.stringify(analysis)});
 assert.match(html,/<ol>/);assert.match(html,/<ul>/);
 assert.equal((html.match(/<li>/g)||[]).length,3);
 assert.match(html,/<strong>.*?结构条件与边界.*?<\\/strong>/);
 assert.match(html,/katex-mathml/);assert.doesNotMatch(html,/<p[^>]*><div/);
 const short=render('近期研究主要关注**结构条件**与**构造方法**。',true);
 assert.match(short,/trend-markdown-compact/);assert.equal((short.match(/<strong>/g)||[]).length,2);
 assert.doesNotMatch(short,/\\*\\*/);
 assert.match(render('旧缓存的普通中文正文。'),/旧缓存的普通中文正文/);
 assert.match(render('<img src=x onerror=alert(1)>'),/&lt;img/);
 console.log('Trend rendering checks passed: paragraphs, ordered/unordered lists, bold, math, compact text, legacy plain text and escaped HTML.');
`
const result=spawnSync(process.execPath,['--max-old-space-size=96','--input-type=module','-e',check],{cwd:fileURLToPath(new URL('..',import.meta.url)),timeout:10000,encoding:'utf8',maxBuffer:128*1024})
assert.equal(result.status,0,`Trend renderer failed: ${result.stdout}\n${result.stderr}`)
console.log(result.stdout.trim())
