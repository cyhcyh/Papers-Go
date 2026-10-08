import {createContext,useContext,useEffect,useState} from 'react'
import {FileText} from 'lucide-react'
import {api} from './api'

export type Site={name:string;description:string;logo_url:string;favicon_url:string}
const defaults:Site={name:'刷论文',description:'像刷视频一样发现论文，追踪趋势。',logo_url:'',favicon_url:''}
const SiteContext=createContext(defaults)
export function SiteProvider({children}:{children:React.ReactNode}){
 const [site,setSite]=useState(defaults)
 useEffect(()=>{const controller=new AbortController();const update=()=>api<Site>('/site','GET',undefined,controller.signal).then(setSite).catch(()=>{});void update();window.addEventListener('site-change',update);return()=>{controller.abort();window.removeEventListener('site-change',update)}},[])
 useEffect(()=>{document.title=site.name;document.querySelector('meta[name="description"]')?.setAttribute('content',site.description);document.querySelector('link[rel="icon"]')?.setAttribute('href',site.favicon_url||'/icon.svg')},[site])
 return <SiteContext.Provider value={site}>{children}</SiteContext.Provider>
}
export const useSite=()=>useContext(SiteContext)
export function SiteBrand({compact=false}:{compact?:boolean}){
 const site=useSite()
 return <><span className="brand-icon">{site.logo_url?<img src={site.logo_url} alt=""/>:<FileText size={21}/>}</span><span>{site.name}{!compact&&<small>FOLLOW YOUR CURIOSITY</small>}</span></>
}
