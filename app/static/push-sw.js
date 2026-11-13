/* BFH Web Push only: no page caching or offline interception. */
self.addEventListener('install',event=>event.waitUntil(self.skipWaiting()));
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
async function setBadge(count){
  try{if(count>0&&self.navigator.setAppBadge)await self.navigator.setAppBadge(count);else if(self.navigator.clearAppBadge)await self.navigator.clearAppBadge();}catch(_error){}
}
self.addEventListener('push',event=>{
  let data={};try{data=event.data?.json()||{};}catch(_error){}
  // Apple requires a visible alert for every push; badge-only silent pushes are not sent.
  event.waitUntil(Promise.all([
    self.registration.showNotification(data.title||'BannedFromHeaven',{
      body:data.body||'You have an update on BannedFromHeaven.',
      icon:'/static/apple-touch-icon.png',tag:data.tag||'bfh-update',
      data:{url:typeof data.url==='string'?data.url:'/notifications'},
    }),setBadge(Math.max(0,Number(data.badge)||0)),
  ]));
});
self.addEventListener('notificationclick',event=>{
  event.notification.close();
  event.waitUntil((async()=>{
    let target=new URL('/notifications',self.location.origin);
    try{const requested=new URL(event.notification.data?.url||'/notifications',self.location.origin);if(requested.origin===self.location.origin)target=requested;}catch(_error){}
    const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});
    for(const client of windows){if(new URL(client.url).origin===self.location.origin){await client.navigate(target.href);return client.focus();}}
    return self.clients.openWindow(target.href);
  })());
});
