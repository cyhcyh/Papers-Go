export const VIEW_DWELL_MS=5000

// One timer follows one selected paper. Changing focus always restarts the dwell.
export class PaperDwell {
 private paper:number|null=null
 private started=0
 private timer:ReturnType<typeof setTimeout>|undefined
 constructor(private finish:(id:number,ms:number)=>void,private now=()=>performance.now(),private schedule:(fn:()=>void,delay:number)=>ReturnType<typeof setTimeout>=(fn,delay)=>setTimeout(fn,delay),private cancel:(id:ReturnType<typeof setTimeout>)=>void=id=>clearTimeout(id)){}
 select(id:number|null){
  if(id===this.paper)return
  if(this.timer!==undefined)this.cancel(this.timer)
  this.paper=id;this.timer=undefined
  if(id===null)return
  this.started=this.now()
  this.timer=this.schedule(()=>{if(this.paper===id){this.timer=undefined;this.finish(id,Math.max(VIEW_DWELL_MS,Math.round(this.now()-this.started)))}},VIEW_DWELL_MS)
 }
 elapsed(id:number){return this.paper===id?Math.max(0,Math.round(this.now()-this.started)):0}
 dispose(){this.select(null)}
}
