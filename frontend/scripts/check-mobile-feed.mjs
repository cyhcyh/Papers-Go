import assert from 'node:assert/strict'
import {mkdir,readFile} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import {build} from 'esbuild'
import postcss from 'postcss'

const output=new URL('../node_modules/.cache/mobile-feed-check/',import.meta.url)
await mkdir(output,{recursive:true})
await build({entryPoints:[fileURLToPath(new URL('../src/hooks/useMobileFeedChrome.ts',import.meta.url))],outfile:fileURLToPath(new URL('chrome.mjs',output)),bundle:true,platform:'node',format:'esm',packages:'external'})
const {installMobileFeedChrome}=await import(new URL('chrome.mjs',output))

class Node extends EventTarget {
 constructor(kind,parent=null){super();this.kind=kind;this.parent=parent;this.listeners=new Map();this.writes=0;this.dataset=new Proxy({},{set:(obj,key,value)=>{this.writes++;obj[key]=value;return true}})}
 addEventListener(type,callback,options){super.addEventListener(type,callback,options);this.listeners.set(callback,type)}
 removeEventListener(type,callback,options){super.removeEventListener(type,callback,options);this.listeners.delete(callback)}
 closest(selector){return selector.split(',').some(s=>this.matches(s))?this:this.parent?.closest(selector)||null}
 matches(selector){return selector===':focus-visible'?!!this.keyboard:({'.feed-page':'feed','.topbar':'bar','.mobile-tabs':'bar','.feed-toolbar':'toolbar','button':'button','a':'link','summary':'summary','.modal-overlay':'modal','dialog[open]':'modal','[role="dialog"]':'modal','[role="menu"]':'modal','.feed-toolbar button[aria-label="刷新信息流"]':'refresh'}[selector]===this.kind)}
 querySelector(){return null}
}
globalThis.Element=Node
function setup(){
 let clock=0,id=0;const timers=new Map(),root=new Node('root'),doc=new Node('document')
 const win={performance:{now:()=>clock},setTimeout:(fn,ms)=>{timers.set(++id,{at:clock+ms,fn});return id},clearTimeout:id=>timers.delete(id)}
 doc.defaultView=win;doc.hidden=false;doc.activeElement=null;doc.selection='';doc.modal=false
 doc.getSelection=()=>({toString:()=>doc.selection});doc.querySelector=()=>doc.modal?{}:null;root.ownerDocument=doc
 const feed=new Node('feed',root),text=new Node('text',feed),bar=new Node('bar',root),button=new Node('button',feed),link=new Node('link',feed),summary=new Node('summary',feed),toolbar=new Node('toolbar',feed),refresh=new Node('refresh',toolbar)
 const emit=(type,target=text,values={})=>{const event=new Event(type);for(const [key,value] of Object.entries({target,pointerId:1,isPrimary:true,button:0,clientX:100,clientY:200,...values}))Object.defineProperty(event,key,{value});root.dispatchEvent(event)}
 const advance=ms=>{const end=clock+ms;while(true){const entry=[...timers].sort((a,b)=>a[1].at-b[1].at)[0];if(!entry||entry[1].at>end)break;timers.delete(entry[0]);clock=entry[1].at;entry[1].fn()}clock=end}
 const tap=(target=text)=>{emit('pointerdown',target);advance(40);emit('pointerup',target)}
 const cleanup=installMobileFeedChrome(root)
 return {root,doc,win,timers,text,bar,button,link,summary,refresh,emit,advance,tap,cleanup}
}

{
 const s=setup();assert.equal(s.root.dataset.mobileFeedChrome,'visible');assert.equal(s.timers.size,1)
 s.advance(2999);assert.equal(s.root.dataset.mobileFeedChrome,'visible');s.advance(1);assert.equal(s.root.dataset.mobileFeedChrome,'hidden');assert.equal(s.timers.size,0)
 s.tap();assert.equal(s.root.dataset.mobileFeedChrome,'visible');s.tap();assert.equal(s.root.dataset.mobileFeedChrome,'hidden')
 s.tap();s.advance(3000);assert.equal(s.root.dataset.mobileFeedChrome,'hidden');s.cleanup()
}
{
 const s=setup();s.advance(3000);const writes=s.root.writes
 s.emit('pointerdown');for(let y=200;y<400;y++)s.emit('pointermove',s.text,{clientY:y});s.emit('pointerup',s.text,{clientY:400})
 assert.equal(s.root.writes,writes,'swipes must not redraw or change chrome')
 s.emit('pointerdown');s.emit('scroll');s.emit('pointerup');assert.equal(s.root.dataset.mobileFeedChrome,'hidden')
 s.tap();assert.equal(s.root.dataset.mobileFeedChrome,'hidden','a tap stopping momentum must not reveal chrome')
 s.advance(200);s.tap();assert.equal(s.root.dataset.mobileFeedChrome,'visible');s.cleanup()
}
{
 const s=setup();s.advance(3000)
 for(const target of [s.button,s.link,s.summary]){s.tap(target);assert.equal(s.root.dataset.mobileFeedChrome,'hidden')}
 s.emit('pointerdown');s.advance(600);s.emit('pointerup');assert.equal(s.root.dataset.mobileFeedChrome,'hidden')
 s.emit('pointerdown');s.emit('pointercancel');s.emit('pointerup');assert.equal(s.root.dataset.mobileFeedChrome,'hidden')
 s.doc.selection='selected text';s.tap();assert.equal(s.root.dataset.mobileFeedChrome,'hidden');s.doc.selection=''
 s.emit('click',s.refresh);assert.equal(s.root.dataset.mobileFeedChrome,'visible');s.advance(2500);s.emit('click',s.bar);s.advance(1000);assert.equal(s.root.dataset.mobileFeedChrome,'visible');s.advance(2000);assert.equal(s.root.dataset.mobileFeedChrome,'hidden');s.cleanup()
}
{
 const s=setup();s.emit('pointerdown',s.bar);s.advance(4000);assert.equal(s.root.dataset.mobileFeedChrome,'visible','a held navigation control must not slide away')
 s.emit('pointerup',s.bar);s.advance(3000);assert.equal(s.root.dataset.mobileFeedChrome,'hidden')
 s.emit('focusin',s.bar);s.emit('pointerdown',s.bar);s.emit('pointerdown',s.bar,{isPrimary:false,pointerId:2});s.advance(3000);assert.equal(s.root.dataset.mobileFeedChrome,'hidden','a second touch must not leave navigation stuck open');s.cleanup()
}
{
 const s=setup();s.doc.modal=true;s.advance(6000);assert.equal(s.root.dataset.mobileFeedChrome,'visible');assert.equal(s.timers.size,1)
 s.doc.modal=false;s.advance(3000);assert.equal(s.root.dataset.mobileFeedChrome,'hidden')
 s.bar.keyboard=true;s.doc.activeElement=s.bar;s.emit('focusin',s.bar);s.advance(6000);assert.equal(s.root.dataset.mobileFeedChrome,'visible','keyboard navigation must stay available')
 s.doc.activeElement=null;s.emit('focusout',s.bar);s.advance(3000);assert.equal(s.root.dataset.mobileFeedChrome,'hidden')
 s.doc.hidden=true;s.doc.dispatchEvent(new Event('visibilitychange'));assert.equal(s.timers.size,0)
 s.doc.hidden=false;s.doc.dispatchEvent(new Event('visibilitychange'));assert.equal(s.root.dataset.mobileFeedChrome,'visible')
 s.cleanup();assert.equal(s.timers.size,0);assert.equal(s.root.listeners.size,0);assert.equal(s.doc.listeners.size,0);assert.equal(s.root.dataset.mobileFeedChrome,undefined)
 s.tap();assert.equal(s.root.dataset.mobileFeedChrome,undefined,'leaving the mobile feed must remove its behavior')
}

const css=postcss.parse(await readFile(new URL('../src/mobile-layout.css',import.meta.url),'utf8'))
css.walkDecls(decl=>{let node=decl.parent;while(node&&!(node.type==='atrule'&&node.name==='media'&&node.params.includes('max-width:600px')))node=node.parent;assert.ok(node,'every layout change must be scoped to mobile')})
console.log('Mobile feed checks passed: 3s idle, deliberate tap toggles, swipe/momentum/long-press exclusion, functional controls, refresh, modal and keyboard access, cleanup, zero idle timers when hidden, mobile-only CSS.')
