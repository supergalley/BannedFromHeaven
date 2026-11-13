// No real push provider is contacted: exercise the service-worker handlers directly.
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
 const events={},notifications=[],badges=[],navigations=[];
 const client={url:'https://bannedfromheaven.com/',navigate:async url=>navigations.push(url),focus:async()=>{}};
 const self={location:{origin:'https://bannedfromheaven.com'},navigator:{setAppBadge:async n=>badges.push(n),clearAppBadge:async()=>badges.push(0)},addEventListener:(name,fn)=>events[name]=fn,registration:{showNotification:async(title,options)=>notifications.push({title,options})},clients:{matchAll:async()=>[client],openWindow:async url=>navigations.push(url)}};
 vm.runInNewContext(fs.readFileSync('app/static/push-sw.js','utf8'),{self,URL,Number,Promise});
 for(const url of ['/messages/12','/jokes/6537','/user/sinner#account-status']){
  let promise;events.push({data:{json:()=>({title:'Test alert',body:'Relevant update',url,badge:3,tag:'unique-event'})},waitUntil:p=>promise=p});await promise;
  const {options}=notifications.at(-1);assert.equal(options.data.url,url);assert.equal(badges.at(-1),3);
  events.notificationclick({notification:{data:options.data,close:()=>{}},waitUntil:p=>promise=p});await promise;assert.equal(navigations.at(-1),'https://bannedfromheaven.com'+url);
 }
 let promise;events.notificationclick({notification:{data:{url:'https://evil.test/'},close:()=>{}},waitUntil:p=>promise=p});await promise;assert.equal(navigations.at(-1),'https://bannedfromheaven.com/notifications');
 events.push({data:{json:()=>({title:'New update',badge:0})},waitUntil:p=>promise=p});await promise;assert.equal(badges.at(-1),0);
 assert.equal(events.fetch,undefined,'Push worker must not intercept site browsing');
 console.log('Service worker: visible alerts, badge updates/clearing, all deep links, external-link rejection and no page caching passed.');
})().catch(e=>{console.error(e);process.exit(1)});
