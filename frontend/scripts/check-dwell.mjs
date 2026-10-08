import assert from 'node:assert/strict'
import {mkdir} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import {build} from 'esbuild'

const output=new URL('../node_modules/.cache/dwell-check/dwell.mjs',import.meta.url)
await mkdir(new URL('.',output),{recursive:true})
await build({entryPoints:[fileURLToPath(new URL('../src/hooks/dwell.ts',import.meta.url))],outfile:fileURLToPath(output),bundle:true,platform:'node',format:'esm'})
const {PaperDwell}=await import(output.href)
let clock=0,next=0
const scheduled=new Map(),events=[]
const dwell=new PaperDwell((id,ms)=>events.push({id,ms}),()=>clock,(fn,delay)=>{scheduled.set(++next,{fn,at:clock+delay});return next},id=>scheduled.delete(id))
const advance=ms=>{clock+=ms;for(const [id,timer] of [...scheduled])if(timer.at<=clock){scheduled.delete(id);timer.fn()}}
dwell.select(1);advance(4999)
assert.equal(events.length,0)
advance(1)
assert.deepEqual(events,[{id:1,ms:5000}])
dwell.select(1);advance(10000)
assert.equal(events.length,1)
dwell.select(2);advance(3000);dwell.select(3);advance(4999)
assert.equal(events.length,1)
advance(1)
assert.deepEqual(events.at(-1),{id:3,ms:5000})
// Backgrounding, switching page, or a competing dialog deselects the paper.
dwell.select(4);advance(4000);dwell.select(null);advance(10000)
assert.equal(events.length,2)
dwell.select(4);advance(4999)
assert.equal(events.length,2)
advance(1)
assert.deepEqual(events.at(-1),{id:4,ms:5000})
dwell.select(5);advance(2000);dwell.dispose();advance(5000)
assert.equal(events.length,3)
assert.equal(scheduled.size,0)
console.log('Dwell checks passed: five seconds, one paper, focus reset, background interruption, and disposal.')
