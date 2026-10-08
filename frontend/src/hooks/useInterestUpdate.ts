import {useEffect,useRef} from 'react'
import {useLoad} from './useLoad'
import type {ProfileResponse} from '../types'

export function useInterestUpdate(path:string|null='/profile'){
 const result=useLoad<ProfileResponse>(path),previous=useRef<number|null>(null)
 const status=result.data?.update?.status
 useEffect(()=>{if(!path)return;const reload=result.reload;window.addEventListener('interest-saved',reload);window.addEventListener('focus',reload);return()=>{window.removeEventListener('interest-saved',reload);window.removeEventListener('focus',reload)}},[path,result.reload])
 useEffect(()=>{if(!path||result.loading||(status!=='queued'&&status!=='processing'))return;const timer=setTimeout(result.reload,2000);return()=>clearTimeout(timer)},[path,status,result.data,result.loading,result.reload])
 useEffect(()=>{if(!path){previous.current=null;return}const id=result.data?(result.data.current?.id||0):undefined;if(id===undefined)return;if(previous.current!==null&&id!==previous.current){window.dispatchEvent(new Event('profile-change'));window.dispatchEvent(new Event('stats-change'))}previous.current=id},[path,result.data])
 return result
}
