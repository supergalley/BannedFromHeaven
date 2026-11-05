/* SPDX-License-Identifier: MIT
 * Original integer-only arcade rules. Mirrored by app/arcade/rules.py. */
(function (root) {
  'use strict';
  const FPS = 30;
  const MAX_FRAMES = FPS * 60 * 5;
  const RULES_VERSION = 1;
  const directions = [[0,-1],[1,0],[0,1],[-1,0]];
  function randomNumber(s) { s.rng = (Math.imul(1664525, s.rng) + 1013904223) >>> 0; return s.rng; }
  function eggs() {
    const result = [];
    for (let r=0;r<3;r++) for (let c=0;c<5;c++) result.push({x:6400+c*8500,y:7400+r*6200,alive:true});
    return result;
  }
  function placeFood(s) {
    const free=24*28-s.body.length;
    if (!free) { s.ended=true;s.reason='cleared';s.food=null;return; }
    let index=randomNumber(s)%free;
    const occupied=new Set(s.body.map(cell=>cell[0]+','+cell[1]));
    for(let y=0;y<28;y++)for(let x=0;x<24;x++)if(!occupied.has(x+','+y)) {
      if(index===0){s.food=[x,y];return;}index--;
    }
  }
  function create(game,seed) {
    const s={game,frame:0,score:0,ended:false,reason:'',rng:seed>>>0};
    if(game==='flappy-devil')Object.assign(s,{y:30000,vy:0,started:false,flight:0,pipes:[],last_input:0});
    else if(game==='spunk-invaders')Object.assign(s,{x:24000,lives:3,wave:1,direction:1,eggs:eggs(),shots:[],bombs:[],effects:[],next_shot:0,next_bomb:30,invulnerable_until:0});
    else if(game==='hell-serpent') {Object.assign(s,{body:[[12,14],[11,14],[10,14],[9,14]],direction:1,wanted:1,countdown:5,apples:0,food:null});placeFood(s);}
    else throw new Error('Unknown game');
    return s;
  }
  function flappy(s,input) {
    if((input&1)&&!(s.last_input&1)){s.started=true;s.vy=-750;}
    s.last_input=input;
    if(!s.started)return;
    s.vy+=75;s.y+=s.vy;
    if(s.flight%88===0)s.pipes.push({x:52000,center:16000+randomNumber(s)%27001,passed:false});
    s.flight++;
    for(const p of s.pipes){
      p.x-=260;
      if(p.x<12600&&p.x+6800>9800&&(s.y-1400<p.center-9000||s.y+1400>p.center+9000)){s.ended=true;s.reason='crash';}
    }
    if(s.y<=1400||s.y>=58600){s.ended=true;s.reason='crash';}
    if(!s.ended)for(const p of s.pipes)if(!p.passed&&p.x+6800<11200){p.passed=true;s.score++;}
    s.pipes=s.pipes.filter(p=>p.x>-7000);
  }
  function invaders(s,input){
    s.x=Math.max(2200,Math.min(45800,s.x+((input&2?1:0)-(input&1?1:0))*650));
    if((input&4)&&s.frame>=s.next_shot){s.shots.push({x:s.x,y:55100});s.next_shot=s.frame+8;}
    let alive=s.eggs.filter(e=>e.alive);
    const speed=90+Math.min(s.wave,8)*20,dx=speed*s.direction;
    const turn=Math.min(...alive.map(e=>e.x))+dx<2800||Math.max(...alive.map(e=>e.x))+dx>45200;
    if(turn)s.direction*=-1;
    for(const e of alive){if(turn)e.y+=2200;else e.x+=dx;}
    s.effects=s.effects.filter(e=>s.frame-e.at<22);
    s.shots=s.shots.filter(shot=>{
      shot.y-=1100;
      const hit=s.eggs.find(e=>e.alive&&Math.abs(shot.x-e.x)<=2100&&Math.abs(shot.y-e.y)<=2700);
      if(hit){hit.alive=false;s.score+=10;s.effects.push({x:hit.x,y:hit.y,at:s.frame});return false;}
      return shot.y>-2000;
    });
    alive=s.eggs.filter(e=>e.alive);
    if(alive.length&&s.frame>=s.next_bomb){
      const source=alive[randomNumber(s)%alive.length];
      s.bombs.push({x:source.x,y:source.y+2600});
      s.next_bomb=s.frame+Math.max(14,42-s.wave*3);
    }
    s.bombs=s.bombs.filter(bomb=>{
      bomb.y+=430+s.wave*20;
      const hit=Math.abs(bomb.x-s.x)<=2400&&Math.abs(bomb.y-57400)<=1800;
      if(hit&&s.frame>=s.invulnerable_until){s.lives--;s.invulnerable_until=s.frame+45;return false;}
      return bomb.y<64000;
    });
    if(s.lives<=0){s.ended=true;s.reason='hit';}
    else if(alive.length&&Math.max(...alive.map(e=>e.y))>=54600){s.ended=true;s.reason='invasion';}
    else if(!alive.length){s.score+=25;s.wave++;s.eggs=eggs();s.direction=1;s.bombs=[];s.next_bomb=s.frame+35;}
  }
  function snake(s,input){
    const wanted=({1:0,2:1,4:2,8:3})[input];
    if(wanted!==undefined&&(wanted+2)%4!==s.direction)s.wanted=wanted;
    s.countdown--;if(s.countdown>0)return;
    s.direction=s.wanted;
    const [dx,dy]=directions[s.direction],head=[s.body[0][0]+dx,s.body[0][1]+dy];
    const growing=head[0]===s.food?.[0]&&head[1]===s.food?.[1];
    if(head[0]<0||head[0]>=24||head[1]<0||head[1]>=28){s.ended=true;s.reason='wall';return;}
    const check=growing?s.body:s.body.slice(0,-1);
    if(check.some(c=>c[0]===head[0]&&c[1]===head[1])){s.ended=true;s.reason='tail';return;}
    s.body.unshift(head);
    if(growing){s.apples++;s.score+=10;placeFood(s);}else s.body.pop();
    s.countdown=Math.max(2,5-Math.floor(s.apples/5));
  }
  function step(s,input){
    if(s.ended)return;
    if(s.game==='flappy-devil')flappy(s,input);else if(s.game==='spunk-invaders')invaders(s,input);else snake(s,input);
    s.frame++;
    if(s.frame>=MAX_FRAMES&&!s.ended){s.ended=true;s.reason='time';}
  }
  const api={FPS,MAX_FRAMES,RULES_VERSION,create,step};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  root.BFHArcade=api;
})(typeof window!=='undefined'?window:globalThis);
