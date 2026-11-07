/* SPDX-License-Identifier: MIT — Hammer-Time renderer. */
(function(root){
  'use strict';
  const atlas=new Image();atlas.src='/static/arcade/hammer-characters.png';
  function sprite(ctx,kind,x,y,frame,facing=1,walking=false){
    ctx.save();ctx.translate(x,y);ctx.scale(kind===0?facing:1,1);
    const bounce=walking?Math.sin(frame*.45)*1.5:0;
    if(atlas.complete&&atlas.naturalWidth){
      const cell=atlas.naturalWidth/2,cellH=atlas.naturalHeight/2;
      // Tight source rectangles retain all four characters while removing atlas margins.
      const bounds=[[.19,.01,.49,.96],[.26,.01,.44,.96],[.29,.01,.38,.96],[.25,.01,.45,.96]][kind];
      const [bx,by,bw,bh]=bounds,h=kind===0?96:90,w=h*(bw*cell)/(bh*cellH);
      ctx.drawImage(atlas,(kind%2+bx)*cell,(Math.floor(kind/2)+by)*cellH,bw*cell,bh*cellH,-w/2,-h+bounce,w,h);
    }else{
      ctx.fillStyle=['#d5d0c6','#ea6db5','#ecab60','#e54b52'][kind];ctx.fillRect(-13,-64,26,64);ctx.beginPath();ctx.arc(0,-76,13,0,Math.PI*2);ctx.fill();
    }
    ctx.restore();
  }
  function mallet(ctx,x,y,swing,facing,frame){
    ctx.save();ctx.translate(x+facing*12,y-55);ctx.scale(facing,1);
    const angle=swing?-.9+(12-swing)/12*2.1:-.65;
    ctx.rotate(angle);ctx.lineCap='round';
    ctx.strokeStyle='#2b160c';ctx.lineWidth=10;ctx.beginPath();ctx.moveTo(0,5);ctx.lineTo(28,-42);ctx.stroke();
    ctx.strokeStyle='#bf8655';ctx.lineWidth=6;ctx.stroke();
    const wood=ctx.createLinearGradient(12,-70,12,-35);wood.addColorStop(0,'#f0bd79');wood.addColorStop(.5,'#b86e36');wood.addColorStop(1,'#724022');ctx.fillStyle=wood;
    ctx.strokeStyle='#382316';ctx.lineWidth=2;ctx.beginPath();ctx.roundRect(6,-65,48,29,5);ctx.fill();ctx.stroke();
    ctx.strokeStyle='#f0cc90';ctx.lineWidth=2;for(const px of [14,45]){ctx.beginPath();ctx.moveTo(px,-62);ctx.lineTo(px,-39);ctx.stroke();}
    ctx.restore();
    if(swing>=3&&swing<=10){ctx.save();ctx.strokeStyle='#ffdf8b99';ctx.lineWidth=3;ctx.beginPath();ctx.ellipse(x+facing*42,y-51,37,27,0,-1.8,1.1);ctx.stroke();ctx.restore();}
  }
  function star(ctx,x,y,size){ctx.beginPath();for(let i=0;i<10;i++){const angle=i*Math.PI/5-Math.PI/2,r=i%2?size*.4:size;const px=x+Math.cos(angle)*r,py=y+Math.sin(angle)*r;if(i)ctx.lineTo(px,py);else ctx.moveTo(px,py);}ctx.closePath();ctx.fill();}
  function draw(ctx,s){
    const cam=s.camera/100;
    const sky=ctx.createLinearGradient(0,0,0,640);sky.addColorStop(0,'#191827');sky.addColorStop(.65,'#71505c');sky.addColorStop(1,'#302431');ctx.fillStyle=sky;ctx.fillRect(0,0,480,640);
    ctx.fillStyle='#ffdeb0';ctx.beginPath();ctx.arc(395-cam*.015%160,92,27,0,Math.PI*2);ctx.fill();
    for(let i=-1;i<9;i++){
      const x=i*92-(cam*.25%92),h=155+((i+20)*37%90);
      ctx.fillStyle=i%2?'#282431':'#342b3a';ctx.fillRect(x,360-h,86,h+190);
      for(let row=0;row<5;row++)for(let col=0;col<3;col++){ctx.fillStyle=(row+col+i)%3?'#d7a36a55':'#171c2b';ctx.fillRect(x+12+col*24,380-h+row*32,11,17);}
    }
    for(let i=-1;i<5;i++){
      const x=i*190-(cam*.6%190);ctx.fillStyle='#111724';ctx.fillRect(x,495,180,55);ctx.fillStyle='#e4b56a';ctx.fillRect(x+8,495,164,5);
      ctx.fillStyle='#172334';ctx.fillRect(x+16,509,148,40);
    }
    ctx.fillStyle='#322c36';ctx.fillRect(0,550,480,90);ctx.fillStyle='#bd9e86';ctx.fillRect(0,550,480,8);
    ctx.strokeStyle='#665160';ctx.lineWidth=1;for(let x=-(cam%48);x<480;x+=48){ctx.beginPath();ctx.moveTo(x,560);ctx.lineTo(x-30,640);ctx.stroke();}for(let y=580;y<640;y+=25){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(480,y);ctx.stroke();}
    for(const p of s.platforms){const x=p.x/100-cam,y=p.y/100;ctx.fillStyle='#3a303c';ctx.fillRect(x,y,p.w/100,16);ctx.fillStyle='#e3b775';ctx.fillRect(x,y,p.w/100,5);ctx.fillStyle='#6a4c45';ctx.fillRect(x+10,y+16,9,550-y-16);ctx.fillRect(x+p.w/100-19,y+16,9,550-y-16);}
    for(const e of s.opponents)sprite(ctx,e.kind+1,e.x/100-cam,e.y/100,s.frame,1,true);
    for(const e of s.effects){
      const age=s.frame-e.at,progress=age/24,x=e.x/100-cam+age*e.direction*3,y=e.y/100-Math.sin(progress*Math.PI)*45;
      ctx.save();ctx.globalAlpha=1-progress;sprite(ctx,e.kind+1,x,y,s.frame);ctx.fillStyle='#ffe19a';
      for(let j=0;j<3;j++)star(ctx,x+Math.cos(age*.3+j*2.1)*23,y-90+Math.sin(age*.3+j*2.1)*8,5);
      ctx.font='bold 17px system-ui';ctx.textAlign='center';ctx.fillText('+10',x,y-110);ctx.restore();
    }
    const x=s.x/100-cam,y=s.y/100;
    if(s.frame>=s.invulnerable_until||Math.floor(s.frame/3)%2===0){sprite(ctx,0,x,y,s.frame,s.facing,s.last_input%4!==0);mallet(ctx,x,y,s.swing,s.facing,s.frame);}
    ctx.fillStyle='#151624bb';ctx.beginPath();ctx.roundRect(12,14,190,39,9);ctx.fill();ctx.fillStyle='#ffe2aa';ctx.font='bold 12px system-ui';ctx.fillText('HAMMER-TIME',24,31);ctx.fillStyle='#e7c8b3';ctx.font='11px system-ui';ctx.fillText('JUMP THE LEDGES · TIME YOUR SWING',24,45);
  }
  function preview(ctx){ctx.save();ctx.translate(18,19);ctx.scale(.7,.7);sprite(ctx,0,43,77,0,1,false);mallet(ctx,43,77,0,1,0);ctx.restore();}
  root.BFHHammerArt={draw,preview};
})(window);
