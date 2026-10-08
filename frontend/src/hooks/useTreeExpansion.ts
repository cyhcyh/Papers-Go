import {useEffect,useState} from 'react'

/** Search expansion is temporary; manual choices survive filtering and reloads. */
export function useTreeExpansion(search:string){
 const [expanded,setExpanded]=useState<string[]>([]),[searchCollapsed,setSearchCollapsed]=useState<string[]>([])
 useEffect(()=>setSearchCollapsed([]),[search])
 const isOpen=(key:string)=>search?!searchCollapsed.includes(key):expanded.includes(key)
 const toggle=(key:string)=>{
  if(search)setSearchCollapsed(keys=>keys.includes(key)?keys.filter(k=>k!==key):[...keys,key])
  else setExpanded(keys=>keys.includes(key)?keys.filter(k=>k!==key):[...keys,key])
 }
 const setAll=(keys:string[],open:boolean)=>{
  if(search)setSearchCollapsed(open?[]:keys)
  else setExpanded(open?keys:[])
 }
 return {isOpen,toggle,setAll}
}
