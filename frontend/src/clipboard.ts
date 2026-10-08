export async function copyText(value:string):Promise<void> {
 if(navigator.clipboard?.writeText){await navigator.clipboard.writeText(value);return}
 // LAN HTTP pages lack the async clipboard API. Keep the fallback in an open
 // dialog when necessary so its focus handling does not block the copy.
 const previous=document.activeElement as HTMLElement|null
 const field=document.createElement('textarea')
 field.value=value
 field.readOnly=true
 field.style.position='fixed'
 field.style.opacity='0'
 ;(document.querySelector('dialog[open]')||document.body).appendChild(field)
 try{
  field.select()
  if(!document.execCommand('copy'))throw new Error('复制失败，请手动复制')
 }finally{field.remove();previous?.focus({preventScroll:true})}
}
