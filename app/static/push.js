(() => {
  'use strict';
  const node=document.getElementById('bfh-push-config');if(!node)return;
  const config=JSON.parse(node.textContent),enable=document.getElementById('push-enable'),disable=document.getElementById('push-disable'),status=document.getElementById('push-status');
  let registration,subscription,state,csrf='',busy=false,statusGeneration=0;
  const supported=window.isSecureContext&&'serviceWorker' in navigator&&'PushManager' in window&&'Notification' in window;
  const ios=/iPad|iPhone|iPod/.test(navigator.userAgent)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
  const standalone=window.matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;
  function text(message){if(status)status.textContent=message;}
  function controls(){if(enable)enable.disabled=busy||!supported||!registration||!state?.public_key||(ios&&!standalone)||Boolean(state?.enabled);if(disable)disable.disabled=busy||!state?.enabled;}
  function toBytes(key){const raw=atob(key.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-key.length%4)%4));return Uint8Array.from(raw,c=>c.charCodeAt(0));}
  async function device(){if(!subscription)return '';const bytes=new TextEncoder().encode(subscription.endpoint);return [...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(v=>v.toString(16).padStart(2,'0')).join('');}
  async function api(path,data){
    const response=await fetch(path,{method:data?'POST':'GET',cache:'no-store',headers:{Accept:'application/json',...(data?{'Content-Type':'application/json','X-Push-CSRF':csrf}:{})},...(data?{body:JSON.stringify(data)}:{})});
    if(!response.headers.get('content-type')?.includes('application/json'))throw new Error('Please sign in again to manage notifications.');
    if(!response.ok)throw new Error((await response.json().catch(()=>({}))).error||'Could not update notifications. Please sign in again or retry.');
    return response.json();
  }
  async function badge(count){
    try{if(count>0&&navigator.setAppBadge)await navigator.setAppBadge(count);else if(navigator.clearAppBadge)await navigator.clearAppBadge();}catch(_error){}
  }
  async function refresh(){
    if(document.hidden||busy)return;
    const generation=++statusGeneration;
    try{
      const data=await api(config.status+'?device='+await device());if(generation!==statusGeneration)return;state=data;csrf=state.csrf;
      await badge(state.enabled?state.badge:0);
      if(status){if(ios&&!standalone)text('On iPhone/iPad, use Share → Add to Home Screen, then open BFH from its icon to enable notifications.');else if(!supported)text('This browser does not support Web Push notifications.');else if(Notification.permission==='denied')text('Notifications are blocked. Allow them in your browser or device notification settings, then return here.');else text(state.enabled?'Notifications are enabled on this device.':'Notifications are off on this device.');}
      controls();
    }catch(error){text(error.message);}
  }
  async function start(){
    try{if(supported){registration=await navigator.serviceWorker.register('/push-sw.js',{scope:'/'});await navigator.serviceWorker.ready;subscription=await registration.pushManager.getSubscription();}await refresh();}catch(error){text('Could not prepare notifications. Please reload and try again.');controls();}
  }
  enable?.addEventListener('click',async()=>{
    if(busy||!supported||(ios&&!standalone))return;
    // Ask directly inside the click handler, without awaiting a fetch first (required on iOS).
    const permissionPromise=Notification.requestPermission();busy=true;++statusGeneration;controls();
    try{
      if(await permissionPromise!=='granted')throw new Error('Notifications were not enabled. You can change permission in your device settings.');
      if(!state?.public_key)throw new Error('Please reload the page and try again.');
      subscription=await registration.pushManager.getSubscription()||await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:toBytes(state.public_key)});
      await api(config.subscribe,subscription.toJSON());
    }catch(error){text(error.message);busy=false;controls();return;}
    busy=false;await refresh();
  });
  disable?.addEventListener('click',async()=>{
    busy=true;++statusGeneration;controls();try{await api(config.unsubscribe,{device:await device()});if(subscription)await subscription.unsubscribe();subscription=null;state.enabled=false;await badge(0);}catch(error){text(error.message);busy=false;controls();return;}busy=false;await refresh();
  });
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});window.addEventListener('focus',refresh);
  window.addEventListener('pageshow',()=>refresh());setInterval(refresh,45000);
  // A shared device must not retain the signed-out account's message badge.
  document.querySelectorAll('a[href="'+config.logout+'"]').forEach(link=>link.addEventListener('click',()=>{badge(0);if(subscription)subscription.unsubscribe().catch(()=>{});}));
  start();controls();
})();
