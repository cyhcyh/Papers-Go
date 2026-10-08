import {useCallback,useEffect,useState} from 'react'
import {api,readAuth} from '../api'
import {useTopicRefresh} from './useTopicRefresh'
export function useLoad<T>(path:string|null) {
  const identity=readAuth()?.user.id
  const [data,setData]=useState<T|null>(null),[error,setError]=useState(''),[loading,setLoading]=useState(true),[key,setKey]=useState(0)
  const reload=useCallback(()=>setKey(k=>k+1),[])
  useTopicRefresh(reload)
  useEffect(()=>{if(!path){setData(null);setError('');setLoading(false);return}const controller=new AbortController();setLoading(true);setError('');api<T>(path,'GET',undefined,controller.signal).then(value=>{if(!controller.signal.aborted)setData(value)}).catch(e=>{if(e.name!=='AbortError')setError(e.message)}).finally(()=>{if(!controller.signal.aborted)setLoading(false)});return ()=>controller.abort()},[path,key,identity])
  return {data,setData,error,loading,reload}
}
