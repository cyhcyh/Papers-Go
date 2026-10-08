import assert from 'node:assert/strict'
import {build} from 'esbuild'
import {mkdir} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
const output=new URL('../node_modules/.cache/reading-check/',import.meta.url)
await mkdir(output,{recursive:true})
await build({entryPoints:['src/api.ts'],outfile:fileURLToPath(new URL('api.mjs',output)),bundle:true,platform:'node',format:'esm',packages:'external'})
const {streamChat}=await import(new URL('api.mjs',output))
const source=': keep-alive\n\nevent: card\ndata: '+JSON.stringify({status:'pending',progress:{answers:{problem:'中文正文😀'}}})+'\n\nevent: card\ndata: '+JSON.stringify({status:'ready',card:{answers:{problem:'完整正文'}}})
const bytes=new TextEncoder().encode(source)
globalThis.fetch=async()=>new Response(new ReadableStream({start(controller){for(let i=0;i<bytes.length;i+=3)controller.enqueue(bytes.slice(i,i+3));controller.close()}}),{status:200})
const events=[]
await streamChat('/papers/1/card/stream',{level:'L2'},(event,data)=>events.push({event,data}))
assert.equal(events.length,2)
assert.equal(events[0].data.progress.answers.problem,'中文正文😀')
assert.equal(events[1].data.status,'ready')
console.log('SSE transport passed: split UTF-8, heartbeat, partial answer and final frame at EOF.')
