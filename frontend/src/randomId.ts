// getRandomValues also works on LAN HTTP pages where randomUUID is unavailable.
export function randomId():string {
 const bytes=crypto.getRandomValues(new Uint8Array(16))
 return Array.from(bytes,byte=>byte.toString(16).padStart(2,'0')).join('')
}
