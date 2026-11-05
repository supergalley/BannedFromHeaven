/* SPDX-License-Identifier: MIT — original canvas artwork for the BFH arcade. */
(function(root){
  'use strict';
  const W=480,H=640;
  function round(ctx,x,y,w,h,r=8){ctx.beginPath();ctx.roundRect(x,y,w,h,r);}
  function backdrop(ctx,frame,game){
    const g=ctx.createLinearGradient(0,0,0,H);
    g.addColorStop(0,game==='flappy-devil'?'#211120':'#0c1023');g.addColorStop(1,'#080b12');
    ctx.fillStyle=g;ctx.fillRect(0,0,W,H);
    for(let i=0;i<48;i++){
      const x=(i*137+23)%W,y=(i*79+29)%590;
      ctx.globalAlpha=.2+(.5+.5*Math.sin(frame/25+i))*.4;
      ctx.fillStyle=i%3===0?'#edb6bb':'#b4b8e7';ctx.fillRect(x,y,i%5===0?2:1,i%5===0?2:1);
    }
    ctx.globalAlpha=1;
  }
  function devil(ctx,x,y,frame,tilt=0,scale=1){
    ctx.save();ctx.translate(x,y);ctx.rotate(tilt);ctx.scale(scale,scale);
    const wing=Math.sin(frame*.5)*4;
    ctx.fillStyle='#a92b50';
    ctx.beginPath();ctx.moveTo(-9,3);ctx.lineTo(-29,-9+wing);ctx.lineTo(-25,8);ctx.lineTo(-17,5);ctx.lineTo(-13,12);ctx.closePath();ctx.fill();
    ctx.strokeStyle='#ed5360';ctx.lineWidth=3;ctx.beginPath();ctx.moveTo(-12,10);ctx.bezierCurveTo(-34,22,-29,32,-40,23);ctx.stroke();
    ctx.fillStyle='#ed5360';ctx.beginPath();ctx.moveTo(-40,18);ctx.lineTo(-46,28);ctx.lineTo(-35,26);ctx.closePath();ctx.fill();
    ctx.fillStyle='#ffdbab';
    ctx.beginPath();ctx.moveTo(-13,-7);ctx.lineTo(-12,-27);ctx.lineTo(-3,-13);ctx.closePath();ctx.fill();
    ctx.beginPath();ctx.moveTo(6,-13);ctx.lineTo(15,-25);ctx.lineTo(15,-4);ctx.closePath();ctx.fill();
    const face=ctx.createRadialGradient(-5,-6,1,0,0,20);face.addColorStop(0,'#ff8b76');face.addColorStop(1,'#cf284d');
    ctx.shadowColor='#f74f66';ctx.shadowBlur=12;ctx.fillStyle=face;ctx.beginPath();ctx.arc(0,0,16,0,Math.PI*2);ctx.fill();ctx.shadowBlur=0;
    ctx.fillStyle='#fff5df';ctx.beginPath();ctx.ellipse(-5,-2,4,5,-.25,0,Math.PI*2);ctx.fill();ctx.beginPath();ctx.ellipse(7,-2,4,5,.25,0,Math.PI*2);ctx.fill();
    ctx.fillStyle='#1f1024';ctx.fillRect(-4,-3,2,5);ctx.fillRect(7,-3,2,5);
    ctx.strokeStyle='#591d33';ctx.lineWidth=2;ctx.beginPath();ctx.arc(2,5,6,.1,Math.PI-.1);ctx.stroke();
    ctx.fillStyle='#fff5df';ctx.beginPath();ctx.moveTo(-1,8);ctx.lineTo(2,12);ctx.lineTo(4,8);ctx.fill();ctx.restore();
  }
  function pillar(ctx,x,top,bottom){
    if(bottom<=top)return;
    const g=ctx.createLinearGradient(x,0,x+68,0);g.addColorStop(0,'#662b3a');g.addColorStop(.5,'#352037');g.addColorStop(1,'#21172c');
    ctx.fillStyle=g;round(ctx,x,top,68,bottom-top,5);ctx.fill();ctx.strokeStyle='#a24d4b';ctx.lineWidth=2;ctx.stroke();
    ctx.strokeStyle='#744146';ctx.lineWidth=1;
    for(let y=top+30;y<bottom;y+=34){ctx.beginPath();ctx.moveTo(x+3,y);ctx.lineTo(x+65,y);ctx.moveTo(x+34,y-30);ctx.lineTo(x+34,y);ctx.stroke();}
    ctx.fillStyle='#e77b65';ctx.fillRect(x-4,top,76,8);ctx.fillRect(x-4,bottom-8,76,8);
  }
  function flappy(ctx,s){
    backdrop(ctx,s.frame,s.game);
    ctx.fillStyle='#20172c';
    for(let i=0;i<8;i++){const x=i*74-(s.flight*.6)%74;ctx.fillRect(x,500-(i%3)*24,45,100);ctx.beginPath();ctx.moveTo(x-5,500-(i%3)*24);ctx.lineTo(x+22,465-(i%3)*24);ctx.lineTo(x+50,500-(i%3)*24);ctx.fill();}
    for(const p of s.pipes){pillar(ctx,p.x/100,-20,p.center/100-90);pillar(ctx,p.x/100,p.center/100+90,605);}
    const lava=ctx.createLinearGradient(0,600,0,H);lava.addColorStop(0,'#fa926b');lava.addColorStop(.18,'#df4151');lava.addColorStop(1,'#4e1c36');
    ctx.fillStyle=lava;ctx.fillRect(0,600,W,40);ctx.strokeStyle='#ffc691';ctx.lineWidth=2;
    ctx.beginPath();for(let x=0;x<=W;x+=8){const y=602+Math.sin(x/18+s.frame/8)*3;if(x===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);}ctx.stroke();
    devil(ctx,112,s.y/100,s.frame,Math.max(-.35,Math.min(.8,s.vy/1400)));
    if(!s.started){ctx.textAlign='center';ctx.fillStyle='#d9c3cd';ctx.font='600 14px system-ui';ctx.fillText('SPACE / TAP TO TAKE OFF',W/2,415);}
  }
  function egg(ctx,x,y,frame,index){
    ctx.save();ctx.translate(x,y);ctx.rotate(Math.sin(frame/13+index)*.08);
    ctx.shadowColor='#e8b1f2';ctx.shadowBlur=8;
    const g=ctx.createRadialGradient(-6,-8,2,0,0,26);g.addColorStop(0,'#fff8f4');g.addColorStop(.7,'#edcbdc');g.addColorStop(1,'#a681b9');
    ctx.fillStyle=g;ctx.beginPath();ctx.ellipse(0,0,17,23,0,0,Math.PI*2);ctx.fill();ctx.shadowBlur=0;
    ctx.fillStyle='#efaabf';ctx.globalAlpha=.75;ctx.beginPath();ctx.ellipse(1,3,9,12,-.2,0,Math.PI*2);ctx.fill();ctx.globalAlpha=1;
    ctx.strokeStyle='#fff';ctx.lineWidth=2;ctx.beginPath();ctx.ellipse(-7,-8,3,6,.3,0,Math.PI);ctx.stroke();ctx.restore();
  }
  function embryo(ctx,e,frame){
    const age=frame-e.at,progress=Math.min(1,age/22);
    ctx.save();ctx.translate(e.x/100,e.y/100-age*.65);ctx.rotate(progress*.35);ctx.globalAlpha=1-progress;
    ctx.strokeStyle='#fbd6e7';ctx.lineWidth=2;
    if(progress<.5){ctx.beginPath();ctx.moveTo(-20-progress*20,8);ctx.lineTo(-14-progress*20,-3);ctx.lineTo(-19-progress*20,-9);ctx.moveTo(20+progress*20,-8);ctx.lineTo(14+progress*20,1);ctx.lineTo(18+progress*20,10);ctx.stroke();}
    ctx.strokeStyle='#fba6b8';ctx.lineWidth=9;ctx.lineCap='round';ctx.beginPath();ctx.moveTo(-1,-2);ctx.bezierCurveTo(16,-1,16,18,3,17);ctx.bezierCurveTo(-7,17,-7,9,0,10);ctx.stroke();
    ctx.fillStyle='#ffd1cf';ctx.beginPath();ctx.arc(-3,-8,8,0,Math.PI*2);ctx.fill();ctx.fillStyle='#553341';ctx.beginPath();ctx.arc(0,-9,1.2,0,Math.PI*2);ctx.fill();ctx.restore();
  }
  function tadpole(ctx,x,y,frame,index){
    ctx.save();ctx.translate(x,y);ctx.strokeStyle='#a5f7df';ctx.lineWidth=2.2;ctx.lineCap='round';ctx.beginPath();ctx.moveTo(0,4);ctx.bezierCurveTo(Math.sin(frame*.7+index)*7,12,-Math.sin(frame*.7+index)*6,19,2,25);ctx.stroke();
    ctx.shadowColor='#9bffdb';ctx.shadowBlur=8;ctx.fillStyle='#edfff2';ctx.beginPath();ctx.ellipse(0,-1,5,7,0,0,Math.PI*2);ctx.fill();ctx.restore();
  }
  function invaders(ctx,s){
    backdrop(ctx,s.frame,s.game);
    ctx.strokeStyle='#18223b';ctx.lineWidth=1;for(let y=40;y<640;y+=40){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(W,y);ctx.stroke();}
    s.eggs.forEach((e,i)=>{if(e.alive)egg(ctx,e.x/100,e.y/100,s.frame,i);});
    s.effects.forEach(e=>embryo(ctx,e,s.frame));
    s.shots.forEach((shot,i)=>tadpole(ctx,shot.x/100,shot.y/100,s.frame,i));
    s.bombs.forEach(b=>{ctx.fillStyle='#ff6e99';ctx.shadowColor='#ff5588';ctx.shadowBlur=10;ctx.beginPath();ctx.ellipse(b.x/100,b.y/100,5,8,0,0,Math.PI*2);ctx.fill();ctx.shadowBlur=0;});
    const x=s.x/100,y=574;
    if(s.frame>=s.invulnerable_until||Math.floor(s.frame/4)%2===0){
      ctx.fillStyle='#a18ce9';ctx.beginPath();ctx.moveTo(x,y-17);ctx.lineTo(x+24,y+13);ctx.lineTo(x+9,y+8);ctx.lineTo(x-9,y+8);ctx.lineTo(x-24,y+13);ctx.closePath();ctx.fill();
      ctx.fillStyle='#e3d8ff';round(ctx,x-7,y-13,14,22,4);ctx.fill();ctx.fillStyle='#99f6d3';ctx.beginPath();ctx.ellipse(x,y-18,5,3,0,0,Math.PI*2);ctx.fill();
      ctx.fillStyle='#a2ffe7';ctx.globalAlpha=.5+.3*Math.sin(s.frame);ctx.beginPath();ctx.moveTo(x-5,y+9);ctx.lineTo(x,y+24+Math.sin(s.frame)*4);ctx.lineTo(x+5,y+9);ctx.fill();ctx.globalAlpha=1;
    }
    ctx.strokeStyle='#3b365d';ctx.beginPath();ctx.moveTo(0,610);ctx.lineTo(W,610);ctx.stroke();
  }
  function snake(ctx,s){
    backdrop(ctx,s.frame,s.game);
    for(let y=0;y<28;y++)for(let x=0;x<24;x++){
      ctx.fillStyle=(x+y)%2?'#101a1d':'#111d20';ctx.fillRect(x*20,40+y*20,20,20);
    }
    if(s.food){const x=s.food[0]*20+10,y=s.food[1]*20+50;ctx.save();ctx.translate(x,y);ctx.shadowColor='#b7ff8e';ctx.shadowBlur=16;ctx.fillStyle='#c9ffb0';ctx.beginPath();ctx.arc(0,-2,6,Math.PI,0);ctx.lineTo(6,6);ctx.lineTo(2,3);ctx.lineTo(-1,7);ctx.lineTo(-5,3);ctx.lineTo(-6,6);ctx.closePath();ctx.fill();ctx.shadowBlur=0;ctx.fillStyle='#294a34';ctx.fillRect(-3,-3,2,3);ctx.fillRect(1,-3,2,3);ctx.restore();}
    [...s.body].reverse().forEach((cell,index)=>{
      const x=cell[0]*20+2,y=cell[1]*20+42;
      ctx.fillStyle=index===s.body.length-1?'#c3ef8c':'hsl('+(95+index%8)+', 37%, '+(31+Math.min(20,index))+ '%)';round(ctx,x,y,16,16,5);ctx.fill();
    });
    const head=s.body[0];ctx.save();ctx.translate(head[0]*20+10,head[1]*20+50);ctx.rotate((s.direction-1)*Math.PI/2);
    ctx.fillStyle='#e9ffd5';ctx.beginPath();ctx.moveTo(-5,-6);ctx.lineTo(-4,-12);ctx.lineTo(1,-7);ctx.moveTo(-5,6);ctx.lineTo(-4,12);ctx.lineTo(1,7);ctx.fill();
    ctx.fillStyle='#203a31';ctx.fillRect(2,-5,3,3);ctx.fillRect(2,2,3,3);ctx.strokeStyle='#f58e8e';ctx.lineWidth=1.5;ctx.beginPath();ctx.moveTo(7,0);ctx.lineTo(13,0);ctx.lineTo(16,-2);ctx.moveTo(13,0);ctx.lineTo(16,2);ctx.stroke();ctx.restore();
    ctx.strokeStyle='#405f4b';ctx.strokeRect(.5,40.5,479,559);
  }
  function draw(ctx,s,dpr=1){ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);ctx.save();if(s.game==='flappy-devil')flappy(ctx,s);else if(s.game==='spunk-invaders')invaders(ctx,s);else snake(ctx,s);ctx.restore();}
  root.BFHArcadeArt={draw,devil,egg,tadpole};
})(window);
