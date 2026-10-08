import {useEffect,useState,useRef} from 'react'
import {Plus,Search,CheckSquare,Save,History,RotateCcw,Power,Trash2,Check,X,FileText,PanelLeft,MoreHorizontal,Send,ChevronRight} from 'lucide-react'
import {api} from '../api'
import {useLoad} from '../hooks/useLoad'
import {useViewportPanel} from '../hooks/useViewportPanel'
import {useApp} from '../context'
import {Loading,ErrorBox,Modal,formatTime} from './Common'
import {IconTile} from './PageContentUI'
import {ChatContent} from './ChatContent'
import {Pagination} from './ui/Pagination'
import '../instruction-settings.css'
import '../skills-settings.css'

type Skill={id:string;name:string;title:string;description:string;scope:'system'|'shared'|'personal';status:string;enabled:number;revision:number;owner_id:number|null;updated_at:string;system_key:string|null}
type Detail=Skill&{text:string;document:string;resources:Record<string,string>;allowed_tools:string[];bindings:string[];editable:boolean;proposer_name?:string;default?:string;customized?:boolean}
type Draft={name:string;title:string;description:string;text:string;resources:Record<string,string>;allowed_tools:string[];document?:string;expected_revision?:number}
const blank:Draft={name:'',title:'',description:'',text:'',resources:{},allowed_tools:[]}
const labels:Record<string,string>={system:'系统技能',shared:'共享技能',personal:'个人技能',pending:'待审核',rejected:'已拒绝'}
const toolNames:Record<string,string>={search_papers:'搜索论文',read_now:'论文精读',show_profile:'查看画像',update_interest:'提议修改兴趣',add_watch:'提议添加监视',list_skills:'搜索技能',get_skill:'查看技能说明',activate_skill:'加载技能',read_skill_resource:'读取参考资料',create_skill:'创建个人技能',update_skill:'更新个人技能',set_skill_enabled:'启停个人技能',propose_shared_skill:'提议共享技能'}
function draftOf(item:Detail):Draft{return {name:item.name,title:item.title,description:item.description,text:item.text,resources:item.resources,allowed_tools:item.allowed_tools,expected_revision:item.revision}}
function sourceOf(draft:Draft){return '---\nname: '+JSON.stringify(draft.name)+'\ndescription: '+JSON.stringify(draft.description)+'\nmetadata:\n  title: '+JSON.stringify(draft.title)+(draft.allowed_tools.length?'\nallowed-tools: '+JSON.stringify(draft.allowed_tools.join(' ')):'')+'\n---\n'+draft.text}

export function SkillsSettings({personal=false}:{personal?:boolean}){
 const {toast}=useApp()
 const viewportPanel=useViewportPanel()
 const [filter,setFilter]=useState('all'),[search,setSearch]=useState(''),[query,setQuery]=useState(''),[page,setPage]=useState(1)
 const [selected,setSelected]=useState<string|null>(personal?new URLSearchParams(window.location.search).get('skill'):null),[detail,setDetail]=useState<Detail|null>(null),[draft,setDraft]=useState<Draft>(blank),[creating,setCreating]=useState(false)
 const [busy,setBusy]=useState(false),[loading,setLoading]=useState(false),[error,setError]=useState(''),[mode,setMode]=useState<'edit'|'preview'|'source'>('edit'),[more,setMore]=useState(false)
 const [mobileOpen,setMobileOpen]=useState(false),[versions,setVersions]=useState<{revision:number;created_at:string}[]|null>(null),[deleting,setDeleting]=useState(false),[versionPreview,setVersionPreview]=useState<{text:string;revision:number}|null>(null)
 const drawer=useRef<HTMLDialogElement>(null),versionDrawer=useRef<HTMLDialogElement>(null)
 const base=personal?'/skills':'/admin/skills',endpoint=(id:string)=>base+(personal?'/':'/detail/')+id
 const catalog=useLoad<{items:Skill[];total:number;page_size:number}>(base+(personal?'?':'/catalog?')+new URLSearchParams({scope:filter,query,page:String(page)}))
 const tools=useLoad<string[]>(base+'/toolset')
 useEffect(()=>{const timeout=setTimeout(()=>{setQuery(search.trim());setPage(1)},250);return()=>clearTimeout(timeout)},[search])
 useEffect(()=>{if(!selected&&!creating&&catalog.data?.items.length)setSelected(catalog.data.items[0].id)},[catalog.data,selected,creating])
 useEffect(()=>{
  if(!selected)return
  const controller=new AbortController();setLoading(true);setError('');setDetail(null)
  api<Detail>(endpoint(selected),'GET',undefined,controller.signal).then(item=>{setDetail(item);setDraft(draftOf(item));setMode(item.editable?'edit':'preview')}).catch(e=>{if(e.name!=='AbortError')setError(e.message)}).finally(()=>{if(!controller.signal.aborted)setLoading(false)})
  return()=>controller.abort()
 },[selected,personal])
 useEffect(()=>{const node=drawer.current;if(mobileOpen&&!node?.open)node?.showModal();if(!mobileOpen&&node?.open)node.close()},[mobileOpen])
 useEffect(()=>{const node=versionDrawer.current;if(versions&&!node?.open)node?.showModal();if(!versions&&node?.open)node.close()},[versions])
 useEffect(()=>{const media=matchMedia('(min-width: 701px)'),close=()=>{if(media.matches)setMobileOpen(false)};media.addEventListener('change',close);return()=>media.removeEventListener('change',close)},[])
 const pick=(id:string)=>{if(busy)return;setCreating(false);setSelected(id);setMobileOpen(false);setMore(false);setVersions(null)}
 const change=(values:Partial<Draft>)=>setDraft(old=>({...old,...values,document:Object.keys(values).every(key=>key==='resources')?old.document:undefined}))
 const switchMode=async(next:typeof mode)=>{
  if(draft.document&&mode==='source'&&next!=='source'){
   setBusy(true);setError('');try{setDraft(await api<Draft>(base+'/validate','POST',draft));setMode(next)}catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }else setMode(next)
 }
 const dirty=creating||!!detail&&JSON.stringify(draft)!==JSON.stringify(draftOf(detail))
 const run=async(work:()=>Promise<unknown>,message:string)=>{setBusy(true);setError('');try{await work();catalog.reload();toast(message)}catch(e){setError((e as Error).message)}finally{setBusy(false);setMore(false)}}
 const accept=(item:Detail)=>{setCreating(false);setSelected(item.id);setDetail(item);setDraft(draftOf(item))}
 const save=()=>run(async()=>accept(await api<Detail>(creating?base:endpoint(selected!),creating?'POST':'PUT',draft)),creating?'技能已创建并启用':'技能已保存，新调用将使用新版本')
 const newSkill=()=>{setSelected(null);setDetail(null);setCreating(true);setDraft({...blank});setMode('edit');setError('');setMobileOpen(false)}
 const list=<><label className="instruction-search"><Search size={16}/><input aria-label="搜索技能" placeholder="搜索技能…" value={search} onChange={e=>setSearch(e.target.value)}/></label>
  {!personal&&<div className="skill-filter" role="group" aria-label="技能类型">{[['all','全部'],['system','系统'],['shared','共享'],['pending','待审核']].map(([id,label])=><button key={id} className={filter===id?'active':''} aria-pressed={filter===id} onClick={()=>{setFilter(id);setPage(1)}}>{label}</button>)}</div>}
  <nav aria-label="技能列表">{catalog.data?.items.map(item=><button className={'skill-list-item '+(selected===item.id?'active':'')} key={item.id} onClick={()=>pick(item.id)}><span className="instruction-list-icon"><CheckSquare size={16}/></span><span><strong>{item.title}</strong><small>{item.description}</small><i className={'skill-state '+(item.status==='pending'?'pending':item.enabled?'enabled':'disabled')}>{item.status==='pending'?'待审核':item.status==='rejected'?'已拒绝':item.enabled?'已启用':'已停用'}</i></span><ChevronRight size={14}/></button>)}{catalog.loading&&!catalog.data?<Loading/>:!catalog.data?.items.length&&<p className="instruction-empty">{query?'没有匹配的技能':filter==='pending'?'暂无待审核提议':'这里还没有技能'}</p>}</nav>
  {!!catalog.data?.total&&<Pagination page={page} pages={Math.max(1,Math.ceil(catalog.data.total/20))} total={catalog.data.total} onChange={setPage} label="技能"/>}
 </>
 return <section className={'skills-manager '+(personal?'personal-skills':'')}>
  <div className="skills-page-heading"><div><h3>{personal?'我的技能':'Skills'}</h3><p>{personal?'积累适合您的研究方法与流程。':'管理标准技能、共享发布与版本。'}</p></div><button className="primary" disabled={busy} onClick={newSkill}><Plus size={16}/>{personal?'添加个人技能':'添加共享技能'}</button></div>
  {catalog.error&&<ErrorBox error={catalog.error} reload={catalog.reload}/>}
  <div className="skill-mobile-bar"><button onClick={()=>setMobileOpen(true)}><PanelLeft size={17}/>技能列表</button><span>{creating?'新技能':detail?.title||'选择技能'}</span></div>
  <div ref={viewportPanel} className="instruction-settings skill-settings"><aside className="instruction-navigation skill-desktop-list">{list}</aside>
   <div className="instruction-workspace">
    {loading?<Loading/>:!detail&&!creating?<div className="skills-empty"><CheckSquare size={28}/><h3>沉淀可复用的研究方法</h3><p>{personal?'在智能助手中让助手制作技能，或在这里添加。':'选择左侧技能，或添加一个共享技能。'}</p></div>:<>
     <div className="skill-workspace-scroll"><header className="instruction-heading"><IconTile icon={CheckSquare} tone="purple"/><div><div className="instruction-title"><h3>{creating?'新技能':detail?.title}</h3>{detail&&<><span className="instruction-status">{labels[detail.scope]}</span><span className={'skill-state '+(detail.status==='pending'?'pending':detail.enabled?'enabled':'disabled')}>{detail.status==='pending'?'待审核':detail.status==='rejected'?'已拒绝':detail.enabled?'已启用':'已停用'}</span></>}</div><p>{creating?'填写用途与操作说明，保存后即可使用。':detail?.bindings.join(' · ')}</p>{detail?.status==='pending'&&<p>提议者 {detail.proposer_name||detail.owner_id} · 审核后发布为全站共享技能</p>}</div></header>
     <fieldset className="skill-form" disabled={busy||!!detail&&!detail.editable}>
      <div className="skill-fields"><label>显示名称<input disabled={mode==='source'} value={draft.title} maxLength={100} onChange={e=>change({title:e.target.value})} placeholder="例如：论文方法对比"/></label><label>标准名称<input value={draft.name} maxLength={64} disabled={!!detail?.system_key||mode==='source'} onChange={e=>change({name:e.target.value})} placeholder="例如：compare-paper-methods"/></label><label className="skill-purpose">用途<input disabled={mode==='source'} value={draft.description} maxLength={1024} onChange={e=>change({description:e.target.value})} placeholder="说明做什么，以及什么时候使用"/></label></div>
      <div className="instruction-document"><div className="instruction-mode" role="tablist" aria-label="技能内容视图">{(['edit','preview','source'] as const).map(m=><button key={m} role="tab" aria-selected={mode===m} className={mode===m?'active':''} onClick={()=>void switchMode(m)}>{m==='edit'?'编辑':m==='preview'?'预览':'SKILL.md'}</button>)}</div>
       {mode==='preview'?<div className="instruction-preview"><ChatContent text={draft.document?draft.document.replace(/^---[^\n]*\n[\s\S]*?\n---[^\n]*\n/,''):draft.text}/></div>:<textarea className={'instruction-editor '+(mode==='source'?'skill-source':'')} aria-label={mode==='source'?'SKILL.md 源码':'技能操作说明'} spellCheck={false} maxLength={mode==='source'?20000:16000} value={mode==='source'?(draft.document??(!dirty&&detail?detail.document:sourceOf(draft))):draft.text} onChange={e=>mode==='source'?setDraft(old=>({...old,document:e.target.value})):change({text:e.target.value})}/>}
      </div>
      <details className="skill-resources"><summary><FileText size={15}/>参考资料与工具 <span>{Object.keys(draft.resources).length} 份资料</span></summary><div className="skill-tool-tags">{(tools.data||[]).map(name=><label key={name}><input type="checkbox" disabled={mode==='source'} checked={draft.allowed_tools.includes(name)} onChange={e=>change({allowed_tools:e.target.checked?[...draft.allowed_tools,name]:draft.allowed_tools.filter(t=>t!==name)})}/>{toolNames[name]||name}</label>)}</div><p className="hint">声明技能所需的已有工具，实际权限由网站控制。</p>
       {Object.entries(draft.resources).map(([path,text])=><div className="skill-resource" key={path}><div><code>{path}</code><button type="button" className="icon-button" aria-label={'移除资料 '+path} onClick={()=>change({resources:Object.fromEntries(Object.entries(draft.resources).filter(([key])=>key!==path))})}><Trash2 size={15}/></button></div><textarea aria-label={path+' 内容'} value={text} maxLength={24000} onChange={e=>change({resources:{...draft.resources,[path]:e.target.value}})}/></div>)}
       <button type="button" disabled={Object.keys(draft.resources).length>=8} onClick={()=>{let n=1;while(draft.resources[`references/note-${n}.md`]!==undefined)n++;change({resources:{...draft.resources,[`references/note-${n}.md`]:''}})}}><Plus size={14}/>添加参考资料</button>
      </details>
     </fieldset>
     </div>
     <footer className="instruction-footer skill-footer"><span>{dirty?'有未保存的修改':detail?`版本 ${detail.revision} · ${formatTime(detail.updated_at)}`:'新技能'}</span><div>
      {detail?.status==='pending'?<><button disabled={busy||dirty} onClick={()=>void run(async()=>accept(await api<Detail>(endpoint(detail.id)+'/review','POST',{approve:false})),'提议已拒绝')}><X size={14}/>拒绝</button><button className="primary" disabled={busy||dirty} onClick={()=>void run(async()=>accept(await api<Detail>(endpoint(detail.id)+'/review','POST',{approve:true})),'共享技能已批准并启用')}><Check size={14}/>批准</button></>:detail?.editable&&detail.status==='active'&&<button disabled={busy} onClick={()=>void run(async()=>accept(await api<Detail>(endpoint(detail.id)+'/enabled','POST',{enabled:!detail.enabled})),detail.enabled?'技能已停用':'技能已启用')}><Power size={14}/>{detail.enabled?'停用':'启用'}</button>}
      <button className="primary" disabled={busy||!dirty||!!detail&&!detail.editable||!draft.text.trim()||!draft.title.trim()||!draft.name.trim()||!draft.description.trim()} onClick={()=>void save()}><Save size={14}/>{busy?'正在保存…':'保存技能'}</button>
      {detail&&<div className="skill-more"><button aria-label="更多技能操作" aria-expanded={more} disabled={busy} onClick={()=>setMore(!more)}><MoreHorizontal size={17}/></button>{more&&<div className="skill-more-menu"><button onClick={()=>void run(async()=>{setVersionPreview(null);setVersions(await api(endpoint(detail.id)+'/versions'))},'版本记录已加载')}><History size={15}/>版本记录</button>{detail.default!==undefined&&<button onClick={()=>{change({text:detail.default!});setMode('edit');setMore(false)}}><RotateCcw size={15}/>恢复默认</button>}{personal&&detail.scope==='personal'&&<button onClick={()=>void run(()=>api(endpoint(detail.id)+'/propose','POST'),'已提交共享提议，等待管理员审核')}><Send size={15}/>提议共享</button>}{detail.editable&&!detail.system_key&&<button className="danger" onClick={()=>{setDeleting(true);setMore(false)}}><Trash2 size={15}/>删除技能</button>}</div>}</div>}
     </div></footer><p className="instruction-help">保存后新调用生效，正在执行的调用保留原版本。{detail?.system_key?'系统技能已绑定流水线，停用后跳过质量评分；已有结果保留。':personal?'共享提议须经管理员批准，个人技能仍然保留。':'参考资料由助手按需读取。'}</p>
    </>}
    {error&&<ErrorBox error={error}/>}
   </div>
  </div>
  <dialog ref={drawer} className="tree-drawer skill-list-drawer" aria-label="技能列表" onCancel={()=>setMobileOpen(false)} onClose={()=>setMobileOpen(false)} onClick={e=>{if(e.target===e.currentTarget)setMobileOpen(false)}}><div className="drawer-heading"><strong>技能列表</strong><button className="icon-button" aria-label="收起技能列表" onClick={()=>setMobileOpen(false)}><X size={18}/></button></div><div className="instruction-navigation">{mobileOpen&&list}</div></dialog>
  <dialog ref={versionDrawer} className="skill-version-drawer" aria-label="技能版本记录" onCancel={()=>{setVersions(null);setVersionPreview(null)}} onClose={()=>{setVersions(null);setVersionPreview(null)}}><div className="drawer-heading"><strong>版本记录</strong><button className="icon-button" aria-label="关闭版本记录" onClick={()=>setVersions(null)}><X size={18}/></button></div><p className="muted">恢复历史内容会创建新版本。</p>{versions?.map(v=><div className="skill-version-row" key={v.revision}><span>版本 {v.revision}<small>{formatTime(v.created_at)}</small></span><div className="skill-version-actions"><button disabled={busy} onClick={()=>void run(async()=>setVersionPreview(await api(endpoint(selected!)+'/versions/'+v.revision)),'历史内容已加载')}>查看</button><button disabled={busy||!detail?.editable||v.revision===detail?.revision} onClick={()=>void run(async()=>{accept(await api<Detail>(endpoint(selected!)+'/restore','POST',{revision:v.revision}));setVersions(null)},'已从历史版本恢复')}>{v.revision===detail?.revision?'当前版本':'恢复'}</button></div></div>)}{versionPreview&&<section className="skill-version-preview"><h4>版本 {versionPreview.revision}</h4><ChatContent text={versionPreview.text}/></section>}</dialog>
  {deleting&&detail&&<Modal title={'删除「'+detail.title+'」？'} onClose={()=>!busy&&setDeleting(false)}><p>技能及其版本和参考资料会被删除，已保存的聊天记录和研究结果保留。</p><div className="dialog-actions"><button disabled={busy} onClick={()=>setDeleting(false)}>取消</button><button className="danger" disabled={busy} onClick={()=>void run(async()=>{await api(endpoint(detail.id),'DELETE');catalog.setData(old=>old?{...old,items:old.items.filter(item=>item.id!==detail.id),total:Math.max(0,old.total-1)}:old);if(catalog.data&&page>Math.max(1,Math.ceil((catalog.data.total-1)/20)))setPage(Math.max(1,page-1));setDeleting(false);setDetail(null);setSelected(null)},'技能已删除')}>删除技能</button></div></Modal>}
 </section>
}
