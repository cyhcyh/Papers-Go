import assert from 'node:assert/strict'
import {mkdir} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import {build} from 'esbuild'
import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'

const output=new URL('../node_modules/.cache/paper-feedback-check/buttons.mjs',import.meta.url)
await mkdir(new URL('.',output),{recursive:true})
await build({entryPoints:[fileURLToPath(new URL('../src/components/PaperFeedbackButtons.tsx',import.meta.url))],outfile:fileURLToPath(output),bundle:true,platform:'node',format:'esm',jsx:'automatic',external:['react','react-dom','react/jsx-runtime','lucide-react']})
const {PaperFeedbackButtons}=await import(output.href)
for(const [likes,saves,labels] of [[0,0,['喜欢','收藏']],[3,0,['3','收藏']],[0,2,['喜欢','2']],[3,2,['3','2']]]){
 const html=renderToStaticMarkup(React.createElement(PaperFeedbackButtons,{paper:{id:1,like_count:likes,save_count:saves,liked:likes>0,saved:saves>0},onAction:async()=>{}}))
 assert.deepEqual([...html.matchAll(/<span>(.*?)<\/span>/g)].map(match=>match[1]),labels)
 assert.equal((html.match(/<button/g)||[]).length,2)
 assert.match(html,/aria-label="(?:取消喜欢|喜欢论文)"/)
 assert.match(html,/aria-label="(?:取消收藏|收藏论文)"/)
}
console.log('Paper feedback checks passed: each count independently falls back to its text label at zero.')
