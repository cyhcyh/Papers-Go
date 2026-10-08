// Cache only the public application shell. Authenticated data is always fetched online.
const CACHE = 'shualunwen-shell-v1';
self.addEventListener('install', event => { event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(['/icon.svg','/manifest.webmanifest']))); self.skipWaiting(); });
self.addEventListener('activate', event => {event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k))))); self.clients.claim();});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if(url.origin!==location.origin || event.request.method!=='GET' || url.pathname.startsWith('/api')) return;
  if(url.pathname.startsWith('/assets/') || ['/icon.svg','/manifest.webmanifest'].includes(url.pathname)) {
    event.respondWith(caches.match(event.request).then(hit=>hit || fetch(event.request).then(response=>{if(response.ok){const clone=response.clone(); caches.open(CACHE).then(cache=>cache.put(event.request,clone));}return response;})));
  }
});
