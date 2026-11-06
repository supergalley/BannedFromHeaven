/* SPDX-License-Identifier: MIT — original canvas artwork for the BFH arcade. */
(function(root){
  'use strict';
  const W=480,H=640;
  const embryoSprite=new Image();
  embryoSprite.src='/static/Embryo.png';
  const shipSprite=new Image();
  shipSprite.src='/static/Spaceship.png';
  const serpentMotion=new WeakMap();
  const veinLayer=document.createElement('canvas');veinLayer.width=W;veinLayer.height=H;
  const veinCtx=veinLayer.getContext('2d');
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
    if(!s.started){ctx.textAlign='center';ctx.fillStyle='#d9c3cd';ctx.font='600 14px system-ui';ctx.fillText('SPACE / TAP TO FAP',W/2,415);}
  }
  // Cache the detailed cell surface once rather than rebuilding it every frame.
  let eggSprite;
  function cellSprite(){
    if(eggSprite)return eggSprite;
    eggSprite=document.createElement('canvas');eggSprite.width=128;eggSprite.height=128;
    const c=eggSprite.getContext('2d');c.translate(64,64);
    c.save();c.beginPath();c.arc(0,0,57,0,Math.PI*2);c.clip();
    const g=c.createRadialGradient(-20,-23,2,0,0,60);
    g.addColorStop(0,'#368db0');g.addColorStop(.65,'#145675');g.addColorStop(.9,'#479ebc');g.addColorStop(1,'#b0e9f1');
    c.fillStyle=g;c.fillRect(-64,-64,128,128);
    // Deterministic winding membrane ridges, inspired by microscopy of an ovum.
    let seed=781;const random=()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;};
    c.lineCap='round';
    for(let i=0;i<460;i++){
      let x=random()*116-58,y=random()*116-58;
      c.strokeStyle=i%3===0?'rgba(161,226,242,.65)':'rgba(74,162,194,.6)';c.lineWidth=.6+random()*.8;
      c.beginPath();c.moveTo(x,y);
      for(let j=0;j<5;j++){const angle=random()*Math.PI*2;x+=Math.cos(angle)*3.4;y+=Math.sin(angle)*3.4;c.lineTo(x,y);}
      c.stroke();
    }
    c.restore();c.strokeStyle='#9fdbea';c.lineWidth=1.8;c.beginPath();
    for(let i=0;i<=160;i++){const angle=i/160*Math.PI*2,r=57+Math.sin(angle*29)*.7+Math.sin(angle*47)*.45;const x=Math.cos(angle)*r,y=Math.sin(angle)*r;if(i===0)c.moveTo(x,y);else c.lineTo(x,y);}
    c.closePath();c.stroke();return eggSprite;
  }
  function egg(ctx,x,y,frame,index){
    ctx.save();ctx.translate(x,y);ctx.rotate(Math.sin(frame/13+index)*.08);
    ctx.shadowColor='#64c4e3';ctx.shadowBlur=5;
    ctx.drawImage(cellSprite(),-23,-23,46,46);ctx.restore();
  }
  function embryo(ctx,e,frame){
    const age=frame-e.at,progress=Math.min(1,age/22);
    ctx.save();ctx.translate(e.x/100,e.y/100-age*.65);ctx.rotate(progress*.35);ctx.globalAlpha=1-progress;
    if(embryoSprite.complete&&embryoSprite.naturalWidth){
      const scale=52/Math.max(embryoSprite.naturalWidth,embryoSprite.naturalHeight);
      const w=embryoSprite.naturalWidth*scale,h=embryoSprite.naturalHeight*scale;
      ctx.globalCompositeOperation='screen';
      ctx.drawImage(embryoSprite,-w/2,-h/2,w,h);
      ctx.restore();return;
    }
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
      if(shipSprite.complete&&shipSprite.naturalWidth){
        const scale=48/Math.max(shipSprite.naturalWidth,shipSprite.naturalHeight);
        const width=shipSprite.naturalWidth*scale,height=shipSprite.naturalHeight*scale;
        ctx.drawImage(shipSprite,x-width/2,y-height/2,width,height);
      }else{
        ctx.fillStyle='#a18ce9';ctx.beginPath();ctx.moveTo(x,y-17);ctx.lineTo(x+24,y+13);ctx.lineTo(x+9,y+8);ctx.lineTo(x-9,y+8);ctx.lineTo(x-24,y+13);ctx.closePath();ctx.fill();
        ctx.fillStyle='#e3d8ff';round(ctx,x-7,y-13,14,22,4);ctx.fill();ctx.fillStyle='#99f6d3';ctx.beginPath();ctx.ellipse(x,y-18,5,3,0,0,Math.PI*2);ctx.fill();
        ctx.fillStyle='#a2ffe7';ctx.globalAlpha=.5+.3*Math.sin(s.frame);ctx.beginPath();ctx.moveTo(x-5,y+9);ctx.lineTo(x,y+24+Math.sin(s.frame)*4);ctx.lineTo(x+5,y+9);ctx.fill();ctx.globalAlpha=1;
      }
    }
    ctx.strokeStyle='#3b365d';ctx.beginPath();ctx.moveTo(0,610);ctx.lineTo(W,610);ctx.stroke();
  }
  function smoothPath(ctx,points){
    ctx.beginPath();ctx.moveTo(points[0].x,points[0].y);
    for(let i=1;i<points.length-1;i++){
      ctx.quadraticCurveTo(points[i].x,points[i].y,(points[i].x+points[i+1].x)/2,(points[i].y+points[i+1].y)/2);
    }
    ctx.lineTo(points[points.length-1].x,points[points.length-1].y);
  }
  function serpent(ctx,points,direction){
    if(points.length<2)return;
    ctx.save();ctx.lineCap='round';ctx.lineJoin='round';
    const first=points[0],head=points[points.length-1];
    const skin=ctx.createLinearGradient(first.x,first.y,head.x+1,head.y+1);
    skin.addColorStop(0,'#92654c');skin.addColorStop(.55,'#c99a77');skin.addColorStop(1,'#e5bb99');
    smoothPath(ctx,points);ctx.strokeStyle='#694937';ctx.lineWidth=20;ctx.stroke();
    smoothPath(ctx,points);ctx.strokeStyle=skin;ctx.lineWidth=17;ctx.stroke();
    smoothPath(ctx,points);ctx.strokeStyle='rgba(255,239,217,.28)';ctx.lineWidth=5;ctx.stroke();
    const vessels=points.map((p,i)=>{
      const before=points[Math.max(0,i-1)],after=points[Math.min(points.length-1,i+1)];
      const dx=after.x-before.x,dy=after.y-before.y,length=Math.hypot(dx,dy)||1;
      const nx=-dy/length,ny=dx/length,offset=Math.sin(i*1.63)*2.6;
      return {x:p.x+nx*offset,y:p.y+ny*offset,nx,ny,tx:dx/length,ty:dy/length};
    });
    veinCtx.clearRect(0,0,W,H);veinCtx.globalCompositeOperation='source-over';veinCtx.lineCap='round';
    smoothPath(veinCtx,vessels);veinCtx.strokeStyle='rgba(49,103,159,.65)';veinCtx.lineWidth=1.15;veinCtx.stroke();
    for(let i=1;i<vessels.length-1;i+=2){
      const p=vessels[i],side=i%4===1?1:-1;
      veinCtx.beginPath();veinCtx.moveTo(p.x,p.y);
      veinCtx.bezierCurveTo(p.x+p.nx*3*side+p.tx*2,p.y+p.ny*3*side+p.ty*2,p.x+p.nx*6*side-p.tx*2,p.y+p.ny*6*side-p.ty*2,p.x+p.nx*7*side+p.tx*2,p.y+p.ny*7*side+p.ty*2);
      veinCtx.strokeStyle='rgba(70,126,182,.65)';veinCtx.lineWidth=.9;veinCtx.stroke();
      veinCtx.beginPath();veinCtx.moveTo(p.x+p.nx*3*side,p.y+p.ny*3*side);veinCtx.quadraticCurveTo(p.x+p.nx*4*side-p.tx*2,p.y+p.ny*4*side-p.ty*2,p.x+p.nx*5*side-p.tx*4,p.y+p.ny*5*side-p.ty*4);veinCtx.lineWidth=.6;veinCtx.stroke();
    }
    veinCtx.globalCompositeOperation='destination-in';smoothPath(veinCtx,points);
    veinCtx.strokeStyle='#fff';veinCtx.lineWidth=16.5;veinCtx.stroke();veinCtx.globalCompositeOperation='source-over';
    ctx.drawImage(veinLayer,0,0);
    ctx.translate(head.x,head.y);ctx.rotate((direction-1)*Math.PI/2);
    if(shipSprite.complete&&shipSprite.naturalWidth){
      const scale=26/Math.max(shipSprite.naturalWidth,shipSprite.naturalHeight);
      const width=shipSprite.naturalWidth*scale,height=shipSprite.naturalHeight*scale;
      ctx.rotate(Math.PI/2);
      ctx.drawImage(shipSprite,-width/2,-height/2,width,height);
      ctx.restore();return;
    }
    const face=ctx.createLinearGradient(-8,-8,10,9);face.addColorStop(0,'#efd2b3');face.addColorStop(.6,'#d9ae89');face.addColorStop(1,'#ad7c5e');
    ctx.fillStyle=face;ctx.strokeStyle='#694937';ctx.lineWidth=1.5;
    ctx.beginPath();ctx.ellipse(1,0,12,9,0,0,Math.PI*2);ctx.fill();ctx.stroke();
    ctx.fillStyle='#eaf4c8';ctx.beginPath();ctx.ellipse(4,-4,3,2.4,0,0,Math.PI*2);ctx.ellipse(4,4,3,2.4,0,0,Math.PI*2);ctx.fill();
    ctx.fillStyle='#203d34';ctx.beginPath();ctx.ellipse(4.5,-4,1,1.9,0,0,Math.PI*2);ctx.ellipse(4.5,4,1,1.9,0,0,Math.PI*2);ctx.fill();
    ctx.fillStyle='#4b725d';ctx.beginPath();ctx.arc(10,-2.5,.7,0,Math.PI*2);ctx.arc(10,2.5,.7,0,Math.PI*2);ctx.fill();
    ctx.strokeStyle='#f58e8e';ctx.lineWidth=1.4;ctx.beginPath();ctx.moveTo(13,0);ctx.lineTo(18,0);ctx.lineTo(21,-2);ctx.moveTo(18,0);ctx.lineTo(21,2);ctx.stroke();ctx.restore();
  }
  function smoothSerpentPoints(s,alpha){
    const positions=s.body.map(cell=>({x:cell[0]*20+10,y:cell[1]*20+50}));
    let motion=serpentMotion.get(s.body);
    if(!motion){motion={head:s.body[0],previous:positions,current:positions,at:s.frame};serpentMotion.set(s.body,motion);}
    else if(motion.head!==s.body[0]){
      motion.previous=motion.current;motion.current=positions;motion.head=s.body[0];motion.at=s.frame;
    }
    const period=Math.max(2,5-Math.floor(s.apples/5));
    const progress=s.ended?1:Math.max(0,Math.min(1,(s.frame-motion.at+alpha)/period));
    return motion.current.map((p,i)=>{
      const previous=motion.previous[Math.min(i,motion.previous.length-1)];
      return {x:previous.x+(p.x-previous.x)*progress,y:previous.y+(p.y-previous.y)*progress};
    }).reverse();
  }
  function snake(ctx,s,alpha){
    backdrop(ctx,s.frame,s.game);
    for(let y=0;y<28;y++)for(let x=0;x<24;x++){
      ctx.fillStyle=(x+y)%2?'#101a1d':'#111d20';ctx.fillRect(x*20,40+y*20,20,20);
    }
    if(s.food){const x=s.food[0]*20+10,y=s.food[1]*20+50;ctx.save();ctx.translate(x,y);ctx.shadowColor='#b7ff8e';ctx.shadowBlur=16;ctx.fillStyle='#c9ffb0';ctx.beginPath();ctx.arc(0,-2,6,Math.PI,0);ctx.lineTo(6,6);ctx.lineTo(2,3);ctx.lineTo(-1,7);ctx.lineTo(-5,3);ctx.lineTo(-6,6);ctx.closePath();ctx.fill();ctx.shadowBlur=0;ctx.fillStyle='#294a34';ctx.fillRect(-3,-3,2,3);ctx.fillRect(1,-3,2,3);ctx.restore();}
    serpent(ctx,smoothSerpentPoints(s,alpha),s.direction);
    ctx.strokeStyle='#405f4b';ctx.strokeRect(.5,40.5,479,559);
  }
  function draw(ctx,s,dpr=1,alpha=1){ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);ctx.save();if(s.game==='flappy-devil')flappy(ctx,s);else if(s.game==='spunk-invaders')invaders(ctx,s);else snake(ctx,s,alpha);ctx.restore();}
  root.BFHArcadeArt={draw,devil,egg,tadpole,serpent};
})(window);
