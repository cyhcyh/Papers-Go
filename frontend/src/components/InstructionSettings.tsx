import {useEffect,useState} from 'react'
import {Save,RotateCcw,Search,FileText,CheckSquare,MessageSquare,ChevronRight} from 'lucide-react'
import {api} from '../api'
import {useLoad} from '../hooks/useLoad'
import {useViewportPanel} from '../hooks/useViewportPanel'
import {useApp} from '../context'
import {Loading,ErrorBox} from './Common'
import {IconTile} from './PageContentUI'
import {ChatContent} from './ChatContent'
import '../instruction-settings.css'

type Instruction={id:string;name:string;description:string;text:string;default:string;customized:boolean;bindings?:string[]}
const groups=[{name:'论文处理',ids:['brief','classify','topic_proposal']},{name:'用户与趋势',ids:['interest_init','reflect','trend_report','trend_rewrite']},{name:'研究助手',ids:['chat','reading_card','reading_section','why_you_care']},{name:'Skills 使用与工具',ids:['skills_runtime','tool_list_skills','tool_get_skill','tool_activate_skill','tool_read_skill_resource','tool_create_skill','tool_update_skill','tool_set_skill_enabled','tool_propose_shared_skill']},{name:'工具与评估',ids:['audit','tool_update_interest','tool_add_watch','tool_search_papers','tool_read_now','tool_show_profile','codex_connection_test']}]

export function InstructionSettings({kind='prompt'}:{kind?:'prompt'|'skill'}){
 const skill=kind==='skill',label=skill?'Skill':'Prompt',path=skill?'/admin/skills':'/admin/prompts'
 const result=useLoad<Instruction[]>(path),{toast}=useApp()
 const viewportPanel=useViewportPanel()
 const [selected,setSelected]=useState(skill?'quality':'chat'),[drafts,setDrafts]=useState<Record<string,string>>({}),[busy,setBusy]=useState(false),[error,setError]=useState(''),[search,setSearch]=useState(''),[mode,setMode]=useState<'edit'|'preview'>('edit')
 useEffect(()=>{if(result.data)setDrafts(old=>Object.fromEntries(result.data!.map(p=>[p.id,old[p.id]??p.text])))},[result.data])
 const item=result.data?.find(p=>p.id===selected)||result.data?.[0]
 if(result.error)return <ErrorBox error={result.error} reload={result.reload}/>
 if(!item)return <Loading/>
 const text=drafts[item.id]??item.text,dirty=text!==item.text,term=search.trim().toLocaleLowerCase()
 const visible=(result.data||[]).filter(p=>(p.name+' '+p.description).toLocaleLowerCase().includes(term))
 const sections=skill?[{name:'评分规范',items:visible}]:[...groups.map(g=>({name:g.name,items:visible.filter(p=>g.ids.includes(p.id))})),{name:'其他功能',items:visible.filter(p=>!groups.some(g=>g.ids.includes(p.id)))}]
 const save=async()=>{setBusy(true);setError('');try{const saved=await api<Instruction>(path+'/'+item.id,'PUT',{text});result.setData((result.data||[]).map(p=>p.id===saved.id?saved:p));toast(label+' 已保存，新调用将使用新内容')}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
 return <div ref={viewportPanel} className="instruction-panel"><section className={'instruction-settings '+(skill?'skill-settings':'prompt-settings-pane')} aria-label={skill?'Skills 管理':'Prompts 管理'}>
  <aside className="instruction-navigation"><label className="instruction-search"><Search size={16}/><input aria-label={skill?'搜索 Skill':'搜索提示词'} placeholder={skill?'搜索 Skill':'搜索提示词'} value={search} onChange={e=>setSearch(e.target.value)}/></label><nav tabIndex={0} aria-label={skill?'选择 Skill':'选择 Prompt'}>{sections.filter(g=>g.items.length).map(group=><div className="instruction-group" key={group.name}><h3>{group.name}</h3>{group.items.map(p=><button key={p.id} className={p.id===item.id?'active':''} aria-current={p.id===item.id?'true':undefined} onClick={()=>{setSelected(p.id);setError('')}}><span className="instruction-list-icon">{skill?<CheckSquare size={16}/>:p.id==='chat'?<MessageSquare size={16}/>:<FileText size={16}/>}</span><span>{p.name}</span>{drafts[p.id]!==undefined&&drafts[p.id]!==p.text?<i className="instruction-draft-dot" title="未保存"/>:<ChevronRight size={13}/>}</button>)}</div>)}</nav>{!visible.length&&<p className="instruction-empty">没有匹配的{skill?'规范':'提示词'}。</p>}</aside>
  <div className="instruction-workspace"><header className="instruction-heading"><IconTile icon={skill?CheckSquare:MessageSquare} tone={skill?'purple':'blue'}/><div><div className="instruction-title"><h3>{item.name}</h3><span className={'instruction-status '+(item.customized?'customized':'')}>{item.customized?'已自定义':skill?'默认规范':'默认文本'}</span></div><p>{item.description}</p>{!!item.bindings?.length&&<p className="instruction-bindings">适用于：{item.bindings.join('、')}</p>}</div></header>
   <div className="instruction-document"><div className="instruction-mode" role="tablist" aria-label={label+' 内容视图'}>{(['edit','preview'] as const).map(m=><button key={m} role="tab" aria-selected={mode===m} className={mode===m?'active':''} onClick={()=>setMode(m)}>{m==='edit'?'编辑':'预览'}</button>)}</div>{mode==='edit'?<textarea className="instruction-editor" aria-label={item.name+' · 指令文本'} value={text} onChange={e=>setDrafts(d=>({...d,[item.id]:e.target.value}))} maxLength={16000} spellCheck={false}/>:<div className="instruction-preview" role="region" aria-label={item.name+'预览'}><ChatContent text={text}/></div>}</div>
   {error&&<ErrorBox error={error}/>}<footer className="instruction-footer"><span className={dirty?'instruction-unsaved':''}>{dirty?'未保存':item.customized?'已保存':'当前使用默认'+(skill?'规范':'文本')}<small>{text.length.toLocaleString()} 字</small></span><div><button disabled={busy} onClick={()=>{setDrafts(d=>({...d,[item.id]:item.default}));setError('')}}><RotateCcw size={14}/>恢复默认</button><button className="primary" disabled={busy||!text.trim()||!dirty} onClick={()=>void save()}><Save size={14}/>{busy?'正在保存…':'保存 '+label}</button></div></footer><p className="instruction-help">保存后新调用生效，正在执行的任务使用启动时的文本。已有论文结果可在任务中心选择重做。</p>
  </div>
 </section></div>
}
