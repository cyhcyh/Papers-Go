import {useEffect} from 'react'

export function notifyTopicsChanged(){
 localStorage.setItem('topics-updated-at',String(Date.now()))
 window.dispatchEvent(new Event('topics-change'))
}
export function useTopicRefresh(reload:()=>void){
 useEffect(()=>{const storage=(event:StorageEvent)=>{if(event.key==='topics-updated-at')reload()};window.addEventListener('topics-change',reload);window.addEventListener('storage',storage);return()=>{window.removeEventListener('topics-change',reload);window.removeEventListener('storage',storage)}},[reload])
}
