import assert from 'node:assert/strict'
import {mkdir} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import {build} from 'esbuild'
import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'

const output=new URL('../node_modules/.cache/job-progress-check/component.mjs',import.meta.url)
await mkdir(new URL('.',output),{recursive:true})
await build({entryPoints:[fileURLToPath(new URL('../src/components/JobProgress.tsx',import.meta.url))],outfile:fileURLToPath(output),bundle:true,platform:'node',format:'esm',jsx:'automatic',external:['react','react-dom','react/jsx-runtime']})
const {JobProgress}=await import(output.href)
const done={key:'papers',label:'论文向量',total:10,completed:10,failed:0,pending:0}
const current={key:'profiles',label:'当前兴趣向量',total:2,completed:1,failed:0,pending:1}
const render=extra=>renderToStaticMarkup(React.createElement(JobProgress,{progress:{stage:'profiles',stages:[done,current],...extra},running:true}))
let html=render({})
assert.equal((html.match(/<progress/g)||[]).length,1)
assert.ok(!html.includes('论文向量'))
assert.ok(html.includes('当前兴趣向量'))
html=render({phase:'indexing',phase_label:'写入新向量索引',phase_completed:3,phase_total:10})
assert.equal((html.match(/<progress/g)||[]).length,1)
assert.ok(html.includes('已写入 3 / 10'))
html=render({phase:'applying',phase_label:'切换向量模型与索引'})
assert.equal((html.match(/<progress/g)||[]).length,1)
assert.ok(!html.includes('value='))
assert.equal(render({phase:'complete'}),'')
assert.equal(renderToStaticMarkup(React.createElement(JobProgress,{progress:{phase:'applying'},running:false})), '')
assert.equal(render({stages:[done,{...current,completed:2,pending:0}]}),'')
console.log('Job progress checks passed: one current stage, honest switching state, completed bars hidden.')
