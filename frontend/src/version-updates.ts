import {api} from './api'

export type ChangelogEntry={version:string;date:string;added:string[];changed:string[];fixed:string[]}
export type InstalledVersion={current_version:string;changelog:ChangelogEntry[];repository:string}
export type VersionCheck={current_version:string;latest_version:string|null;has_update:boolean;status:'ok'|'failed';checked_at:string}
export type RemoteChangelog={changelog:ChangelogEntry[];status:'ok'|'failed';checked_at:string}

let cached:{value:VersionCheck;expires:number}|null=null
let pending:Promise<VersionCheck>|null=null

/** Shared across toolbar mounts. No polling and no network calls for visitors. */
export function checkVersion(force=false):Promise<VersionCheck>{
 if(pending)return pending
 if(!force&&cached&&cached.expires>Date.now())return Promise.resolve(cached.value)
 pending=api<VersionCheck>('/admin/version'+(force?'?force=true':'')).then(value=>{
  cached={value,expires:Date.now()+6*60*60*1000}
  return value
 }).finally(()=>{pending=null})
 return pending
}
