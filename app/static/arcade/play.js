/* SPDX-License-Identifier: MIT — arcade UI, controls, and replay recording. */
(() => {
  'use strict';
  const configNode=document.getElementById('arcade-config');
  if(!configNode)return;
  const config=JSON.parse(configNode.textContent),core=window.BFHArcade,art=window.BFHArcadeArt;
  const canvas=document.getElementById('arcade-canvas'),ctx=canvas.getContext('2d');
  const player=document.getElementById('arcade-player'),overlay=document.getElementById('arcade-overlay');
  const startButton=document.getElementById('arcade-start'),pauseButton=document.getElementById('arcade-pause');
  const restartButton=document.getElementById('arcade-restart'),retryButton=document.getElementById('arcade-retry-save');
  const status=document.getElementById('arcade-save-status'),boardStatus=document.getElementById('arcade-board-status');
  const refreshBoard=document.getElementById('arcade-refresh-board'),touchControls=document.getElementById('arcade-touch-controls');
  const cards=[...document.querySelectorAll('[data-game]')],keys=new Set(),touches=new Map(),aims=new Map();
  const root=config.root.replace(/\/$/,'');
  let game=config.selected,state,phase='idle',run=null,events=[],recordedInput=0,snakeDirection=0;
  let flapQueued=false;
  let lastTime=0,accumulator=0,saving=false,pending=null,boardGeneration=0,dpr=1,dragStart=null;
  const $=id=>document.getElementById(id);
  const setText=(id,value)=>{const text=String(value);if($(id).textContent!==text)$(id).textContent=text;};
  const format=value=>Number(value||0).toLocaleString();
  const isLive=()=>phase==='playing'||phase==='paused';

  async function api(path,body){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);
    try{
      const response=await fetch(root+path,{method:body?'POST':'GET',cache:'no-store',signal:controller.signal,
        headers:{Accept:'application/json',...(body?{'Content-Type':'application/json','X-Arcade-CSRF':config.csrf}:{})},
        ...(body?{body:JSON.stringify(body)}:{})});
      if(!response.headers.get('content-type')?.includes('application/json'))throw new Error('Please refresh this page and sign in again.');
      const data=await response.json();
      if(!response.ok)throw new Error(data.error||'Could not complete the request. Please try again.');
      return data;
    }catch(error){if(error.name==='AbortError')throw new Error('The connection timed out. Please try again.');throw error;}
    finally{clearTimeout(timer);}
  }
  function showOverlay(label,title,copy,button){
    setText('arcade-overlay-label',label);setText('arcade-overlay-title',title);setText('arcade-overlay-copy',copy);
    startButton.innerHTML='';startButton.append(document.createTextNode(button+' '));
    const arrow=document.createElement('span');arrow.setAttribute('aria-hidden','true');arrow.textContent='↗';startButton.appendChild(arrow);
    overlay.hidden=false;
  }
  function resetControls(){
    keys.clear();touches.clear();aims.clear();snakeDirection=0;dragStart=null;flapQueued=false;
    touchControls.querySelectorAll('.is-held').forEach(node=>node.classList.remove('is-held'));
  }
  function lockPicker(locked){cards.forEach(card=>{card.disabled=locked;});startButton.disabled=locked;restartButton.disabled=locked||!isLive();}
  function preview(slug){
    const s=core.create(slug,1337);
    if(slug==='flappy-devil'){s.y=31500;s.pipes=[{x:31000,center:30000,passed:false}];s.started=true;}
    return s;
  }
  function applyBoard(data,slug){
    config.bests[slug]=data.personal_best;
    document.querySelector('[data-best="'+slug+'"]').textContent=format(data.personal_best);
    if(slug!==game)return;
    $('arcade-leaderboard').innerHTML=data.leaderboard_html;
    setText('arcade-personal-best',format(data.personal_best));
    setText('arcade-personal-rank',data.rank?'#'+data.rank+' overall':'Your throne awaits');
  }
  async function loadBoard(){
    const slug=game,generation=++boardGeneration;
    refreshBoard.disabled=true;boardStatus.textContent='Refreshing…';
    try{const data=await api('/'+slug+'/leaderboard');if(game===slug&&generation===boardGeneration){applyBoard(data,slug);boardStatus.textContent='';}}
    catch(error){if(game===slug&&generation===boardGeneration)boardStatus.textContent=error.message;}
    finally{refreshBoard.disabled=false;}
  }
  function configureControls(){
    touchControls.replaceChildren();
    touchControls.classList.toggle('arcade-dpad',game==='hell-serpent');
    player.classList.toggle('has-dpad',game==='hell-serpent');
    const definitions=game==='flappy-devil'?[[1,'FAP','control-flap']]:game==='spunk-invaders'?[[1,'←',''],[4,'FIRE','control-fire'],[2,'→','']]:[[1,'↑','control-up'],[8,'←','control-left'],[2,'→','control-right'],[4,'↓','control-down']];
    definitions.forEach(([mask,label,className])=>{
      const button=document.createElement('button');button.type='button';button.className='arcade-control '+className;button.textContent=label;
      button.setAttribute('aria-label',label==='←'?'Move left':label==='→'?'Move right':label==='↑'?'Move up':label==='↓'?'Move down':label);
      button.addEventListener('pointerdown',event=>{
        event.preventDefault();if(phase!=='playing')return;
        button.setPointerCapture(event.pointerId);canvas.focus({preventScroll:true});button.classList.add('is-held');
        if(game==='hell-serpent')snakeDirection=mask;else{touches.set(event.pointerId,mask);if(game==='flappy-devil')flapQueued=true;}
      });
      button.addEventListener('keydown',event=>{
        if(![' ','Enter'].includes(event.key)||phase!=='playing')return;
        event.preventDefault();event.stopPropagation();button.classList.add('is-held');
        if(game==='hell-serpent')snakeDirection=mask;else{touches.set('key-'+mask,mask);if(game==='flappy-devil'&&!event.repeat)flapQueued=true;}
      });
      button.addEventListener('keyup',event=>{if([' ','Enter'].includes(event.key)){event.preventDefault();event.stopPropagation();touches.delete('key-'+mask);button.classList.remove('is-held');}});
      button.addEventListener('blur',()=>{touches.delete('key-'+mask);button.classList.remove('is-held');});
      const release=event=>{touches.delete(event.pointerId);button.classList.remove('is-held');};
      button.addEventListener('pointerup',release);button.addEventListener('pointercancel',release);button.addEventListener('lostpointercapture',release);
      touchControls.appendChild(button);
    });
  }
  function selectGame(slug,initial=false){
    if(saving||phase==='starting')return;
    if(!initial&&slug===game)return;
    if(!initial&&isLive()&&!window.confirm('Switch games? This unfinished round will not be saved.'))return;
    if(!initial&&pending&&!pending.saved&&pending.data.score>0&&!window.confirm('Switch without saving this score? You can retry saving before switching.'))return;
    resetControls();game=slug;phase='idle';run=null;pending=null;events=[];accumulator=0;state=preview(slug);
    player.style.setProperty('--game-accent',config.games[game].accent);
    setText('arcade-game-title',config.games[game].name);setText('arcade-controls-help',config.games[game].controls+(game==='spunk-invaders'?' You can also drag on the game to aim and fire.':game==='hell-serpent'?' Swipe the game to turn.':''));
    canvas.setAttribute('aria-label',config.games[game].name+' game area');
    cards.forEach(card=>{const selected=card.dataset.game===game;card.classList.toggle('is-selected',selected);card.setAttribute('aria-pressed',String(selected));});
    pauseButton.disabled=true;pauseButton.textContent='Pause';restartButton.disabled=true;retryButton.hidden=true;
    status.textContent='Your best score saves at game over.';status.classList.remove('text-warning');
    showOverlay('YOUR NEXT BAD HABIT',config.games[game].name,config.games[game].description,'Play now');
    configureControls();updateHud();
    if(!initial){
      const url=new URL(location.href);url.searchParams.set('game',game);history.replaceState(null,'',url);
      $('arcade-leaderboard').textContent='Loading leaderboard…';setText('arcade-personal-best',format(config.bests[game]||0));setText('arcade-personal-rank','Loading…');loadBoard();
    }
  }
  async function startRound(){
    if(phase==='paused'){resume();return;}
    if(saving||phase==='starting')return;
    if(pending&&!pending.saved&&pending.data.score>0&&!window.confirm('Start a new round without saving this score?'))return;
    const oldPhase=phase;phase='starting';lockPicker(true);retryButton.hidden=true;resetControls();
    setText('arcade-overlay-copy','Opening your round…');status.textContent='Starting…';status.classList.remove('text-warning');
    try{
      const data=await api('/'+game+'/start',{});
      if(data.rules_version!==core.RULES_VERSION)throw new Error('The game has been updated. Refresh this page before playing.');
      run=data.run_id;state=core.create(game,data.seed);events=[];recordedInput=0;pending=null;accumulator=0;lastTime=performance.now();
      phase='playing';overlay.hidden=true;pauseButton.disabled=false;pauseButton.textContent='Pause';status.textContent='Personal best saves automatically at game over.';
      canvas.focus({preventScroll:true});updateHud();
    }catch(error){
      phase=oldPhase==='over'?'over':'idle';showOverlay('COULD NOT START',config.games[game].name,error.message,'Try again');status.textContent=error.message;status.classList.add('text-warning');
    }finally{lockPicker(false);}
  }
  function pause(){
    if(phase!=='playing')return;
    phase='paused';resetControls();pauseButton.textContent='Resume';showOverlay('TIME OUT','Round paused','Take a breath. Your round is waiting for you.','Resume');
  }
  function resume(){
    if(phase!=='paused')return;
    resetControls();phase='playing';overlay.hidden=true;pauseButton.textContent='Pause';lastTime=performance.now();accumulator=0;canvas.focus({preventScroll:true});
  }
  function endingCopy(){
    return ({crash:'The gates of hell claim another one.',hit:'The eggs got the better of you.',invasion:'The invasion reached your launcher.',wall:'The walls remain undefeated.',tail:'You got a little too close to yourself.',cleared:'Every soul collected. Absolute menace.',time:'Five minutes of chaos. Nicely done.'})[state.reason]||'Another round in the books.';
  }
  function finish(){
    phase='over';resetControls();pauseButton.disabled=true;restartButton.disabled=true;
    pending={game,data:{run_id:run,frames:state.frame,events:events.map(event=>[...event]),score:state.score},saved:false};
    showOverlay('ROUND FINISHED',format(state.score)+' points',endingCopy(),'Play again');saveRound();
  }
  async function saveRound(){
    if(!pending||pending.saved||saving)return;
    const round=pending;saving=true;lockPicker(true);retryButton.hidden=true;status.classList.remove('text-warning');status.textContent='Saving your score…';
    try{
      const data=await api('/'+round.game+'/score',round.data);
      round.saved=true;++boardGeneration;applyBoard(data,round.game);boardStatus.textContent='';
      status.textContent=data.new_best?'New personal best! Score saved.':'Score saved. Personal best: '+format(data.personal_best)+'.';
      if(round===pending){setText('arcade-overlay-label',data.new_best?'NEW PERSONAL BEST':'ROUND FINISHED');setText('arcade-overlay-copy',endingCopy()+' Your score is saved.');}
    }catch(error){
      status.textContent=error.message;status.classList.add('text-warning');retryButton.hidden=false;
      if(round===pending)setText('arcade-overlay-copy','Your score hasn’t saved yet. Use Retry saving score below.');
    }finally{saving=false;lockPicker(false);}
  }
  function readInput(){
    let touch=0;for(const mask of touches.values())touch|=mask;
    if(game==='spunk-invaders')for(const x of aims.values())touch|=4|(x<state.x/100-12?1:x>state.x/100+12?2:0);
    if(game==='hell-serpent')return snakeDirection;
    if(game==='flappy-devil'){if(flapQueued){if(recordedInput===1)return 0;flapQueued=false;return 1;}return (keys.has(' ')||keys.has('arrowup')||keys.has('w')||(touch&1))?1:0;}
    return ((keys.has('arrowleft')||keys.has('a')||(touch&1))?1:0)|((keys.has('arrowright')||keys.has('d')||(touch&2))?2:0)|((keys.has(' ')||(touch&4))?4:0);
  }
  function updateHud(){
    setText('arcade-score',format(state.score));
    const left=Math.max(0,Math.ceil((core.MAX_FRAMES-(phase==='idle'?0:state.frame))/core.FPS));
    setText('arcade-time',Math.floor(left/60)+':'+String(left%60).padStart(2,'0'));
    setText('arcade-extra',phase==='idle'?'Ready when you are':game==='flappy-devil'?(!state.started?'Tap or press Space to fap':'Gates cleared'):game==='spunk-invaders'?'Wave '+state.wave+' · '+('♥ '.repeat(Math.max(0,state.lives))).trim():state.body.length+' segments · '+state.apples+' souls');
  }
  function resize(){dpr=Math.min(2,window.devicePixelRatio||1);canvas.width=480*dpr;canvas.height=640*dpr;}
  function animate(time){
    const elapsed=lastTime?Math.min(200,time-lastTime):0;lastTime=time;
    if(phase==='playing'){
      accumulator+=elapsed;
      while(accumulator>=1000/core.FPS&&phase==='playing'){
        const controls=readInput();
        if(controls!==recordedInput){events.push([state.frame,controls]);recordedInput=controls;}
        core.step(state,controls);accumulator-=1000/core.FPS;
        if(state.ended)finish();
      }
      updateHud();
    }else accumulator=0;
    const display=phase==='idle'?{...state,frame:Math.floor(time/1000*30),...(game==='flappy-devil'?{y:31500+Math.round(Math.sin(time/700)*1400)}:{})}:state;
    art.draw(ctx,display,dpr,phase==='playing'?accumulator/(1000/core.FPS):0);requestAnimationFrame(animate);
  }
  document.addEventListener('keydown',event=>{
    if(!event.target.closest?.('#arcade-player'))return;
    if(event.target.closest('button')&&!event.target.classList.contains('arcade-control'))return;
    const key=event.key.toLowerCase();
    if((key==='p'||key==='escape')&&!event.repeat&&isLive()){event.preventDefault();phase==='paused'?resume():pause();return;}
    if(![' ','arrowup','arrowdown','arrowleft','arrowright','w','a','s','d'].includes(key))return;
    if(key===' '&&!event.repeat&&(phase==='idle'||phase==='over')){event.preventDefault();startRound();return;}
    if(phase!=='playing')return;
    event.preventDefault();keys.add(key);
    if(game==='flappy-devil'&&!event.repeat&&[' ','arrowup','w'].includes(key))flapQueued=true;
    if(game==='hell-serpent'){const input=({'arrowup':1,w:1,'arrowright':2,d:2,'arrowdown':4,s:4,'arrowleft':8,a:8})[key];if(input)snakeDirection=input;}
  });
  document.addEventListener('keyup',event=>{keys.delete(event.key.toLowerCase());});
  function canvasInput(event){
    const rect=canvas.getBoundingClientRect(),x=(event.clientX-rect.left)/rect.width*480;
    touches.set(event.pointerId,4);aims.set(event.pointerId,x);
  }
  canvas.addEventListener('pointerdown',event=>{
    if(phase!=='playing')return;
    event.preventDefault();canvas.focus({preventScroll:true});canvas.setPointerCapture(event.pointerId);
    if(game==='flappy-devil'){touches.set(event.pointerId,1);flapQueued=true;}
    else if(game==='spunk-invaders')canvasInput(event);
    else dragStart={id:event.pointerId,x:event.clientX,y:event.clientY};
  });
  canvas.addEventListener('pointermove',event=>{
    if(phase!=='playing')return;
    if(game==='spunk-invaders'&&touches.has(event.pointerId))canvasInput(event);
    else if(game==='hell-serpent'&&dragStart?.id===event.pointerId){
      const dx=event.clientX-dragStart.x,dy=event.clientY-dragStart.y;
      if(Math.max(Math.abs(dx),Math.abs(dy))>15){snakeDirection=Math.abs(dx)>Math.abs(dy)?(dx>0?2:8):(dy>0?4:1);dragStart={id:event.pointerId,x:event.clientX,y:event.clientY};}
    }
  });
  const releaseCanvas=event=>{touches.delete(event.pointerId);aims.delete(event.pointerId);if(dragStart?.id===event.pointerId)dragStart=null;};
  canvas.addEventListener('pointerup',releaseCanvas);canvas.addEventListener('pointercancel',releaseCanvas);canvas.addEventListener('lostpointercapture',releaseCanvas);
  cards.forEach(card=>card.addEventListener('click',()=>selectGame(card.dataset.game)));
  startButton.addEventListener('click',startRound);retryButton.addEventListener('click',saveRound);
  pauseButton.addEventListener('click',()=>phase==='paused'?resume():pause());
  restartButton.addEventListener('click',()=>{if(isLive()&&window.confirm('Restart? This unfinished score will not be saved.')){phase='over';pending=null;startRound();}});
  refreshBoard.addEventListener('click',loadBoard);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)pause();});window.addEventListener('blur',()=>{resetControls();pause();});window.addEventListener('resize',resize);
  document.querySelectorAll('[data-preview]').forEach(node=>{
    const c=node.getContext('2d'),slug=node.dataset.preview;
    if(slug==='flappy-devil')art.devil(c,53,41,0,0,1.55);
    else if(slug==='spunk-invaders'){art.egg(c,69,28,0,0);art.tadpole(c,29,51,0,0);}
    else art.serpent(c,[{x:17,y:57},{x:33,y:57},{x:49,y:51},{x:49,y:31},{x:63,y:29},{x:78,y:29}],1);
  });
  selectGame(game,true);resize();requestAnimationFrame(animate);
})();
