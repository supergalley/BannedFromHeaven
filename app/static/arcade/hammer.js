/* SPDX-License-Identifier: MIT — mirrored by app/arcade/hammer.py. */
(function(root){
  'use strict';
  function create(s){Object.assign(s,{x:10000,y:55000,vy:0,facing:1,grounded:true,lives:3,camera:0,chunk:0,platforms:[],opponents:[],effects:[],swing:0,next_swing:0,invulnerable_until:0,last_input:0,hits:0});}
  function step(s,input,random){
    const frame=s.frame,move=Number(Boolean(input&2))-Number(Boolean(input&1));
    if(move)s.facing=move;
    s.x=Math.max(s.camera+1800,s.x+move*340);
    if((input&4)&&!(s.last_input&4)&&s.grounded){s.vy=-1450;s.grounded=false;}
    const previousY=s.y;s.vy+=80;s.y+=s.vy;s.grounded=false;
    const floors=[55000,...s.platforms.filter(p=>p.x-1200<=s.x&&s.x<=p.x+p.w+1200).map(p=>p.y)];
    if(s.vy>=0){const landings=floors.filter(y=>previousY<=y&&y<=s.y);if(landings.length){s.y=Math.min(...landings);s.vy=0;s.grounded=true;}}
    s.camera=Math.max(s.camera,s.x-15000);
    while(s.chunk*72000<s.camera+100000){
      const start=s.chunk*72000,height=45000-(random(s)%2)*1000;
      s.platforms.push({x:start+28000,y:height,w:16000});
      for(const [offset,y] of [[36000,height],[59000,55000]])s.opponents.push({x:start+offset,y,kind:random(s)%3,left:start+offset-6500,right:start+offset+6500});
      s.chunk++;
    }
    s.swing=Math.max(0,s.swing-1);
    if((input&8)&&frame>=s.next_swing){s.swing=12;s.next_swing=frame+23;}
    const activeSwing=s.swing>=3&&s.swing<=10,remaining=[];
    for(const opponent of s.opponents){
      const direction=opponent.x>s.x?-1:1;
      opponent.x=Math.max(opponent.left,Math.min(opponent.right,opponent.x+direction*65));
      const distance=(opponent.x-s.x)*s.facing;
      if(activeSwing&&distance>=-1200&&distance<=8500&&Math.abs(opponent.y-s.y)<=6500){
        s.hits++;s.score+=10;s.effects.push({x:opponent.x,y:opponent.y,kind:opponent.kind,at:frame,direction:s.facing});continue;
      }
      if(Math.abs(opponent.x-s.x)<2700&&Math.abs(opponent.y-s.y)<6000&&frame>=s.invulnerable_until){s.lives--;s.invulnerable_until=frame+60;}
      if(opponent.x>s.camera-6000)remaining.push(opponent);
    }
    s.opponents=remaining;s.platforms=s.platforms.filter(p=>p.x+p.w>s.camera-6000);s.effects=s.effects.filter(e=>frame-e.at<24);s.last_input=input;
    if(s.lives<=0){s.ended=true;s.reason='hammer-hit';}else if(frame+1>=90*30){s.ended=true;s.reason='hammer-time';}
  }
  const api={create,step};if(typeof module!=='undefined'&&module.exports)module.exports=api;root.BFHHammer=api;
})(typeof window!=='undefined'?window:globalThis);
