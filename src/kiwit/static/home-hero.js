function homeHeroState(data, now = new Date()) {
 const s=data?.session;
 if(!data) return {state:'neutral',message:'Sync your workspace to check the latest paper session.'};
 if(s?.position && (s.state==='stopping' || s.valuation_fresh!==true)) return {state:'attention',message:'One paper position needs your attention.',review:true};
 if(!data.available) return {state:'blocked',message:'The paper desk is unavailable. Check its status before starting.'};
 if(s?.state==='stopping') return {state:'attention',message:'The previous paper session is awaiting reconciliation.'};
 const today=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit'}).format(now);
 const age=(now-new Date(s?.last_tick))/1000;
 const fresh=s?.day===today && age>=0 && age<=120;
 if(s?.state==='running' && !fresh) return {state:'neutral',message:'Session status needs refreshing. Open the desk to review the last recorded state.'};
 if(s?.state==='running' && s?.strategy_selection?.plans?.length===0 && (now-new Date(s.strategy_selection.at))/1000<=120 && now>=new Date(s.strategy_selection.at)) return {state:'monitoring',message:'No qualified trade setup is available right now.'};
 if(s?.state==='running') return {state:'monitoring',message:'NitiQuant is monitoring Bank Nifty for qualified setups.'};
 return {state:'neutral',message:'Your paper workspace is ready to review. Open the desk for session details.'};
}
if(typeof module!=='undefined') module.exports={homeHeroState};
if(typeof document!=='undefined') (()=>{
 const hero=document.querySelector('.workspace-intro');
 const heading=hero.querySelector('h2'),message=hero.querySelector('p');
 const content=document.createElement('div');content.className='hero-extras';
 content.innerHTML='<div class="hero-chips"><span>BANK NIFTY</span><span>PAPER MODE</span><span title="Risk controls apply to simulated execution; losses are not guaranteed to be capped.">RISK PROTECTED</span></div><div class="hero-actions"><a class="button primary" id="hero-desk-link" href="#banknifty">Open trading desk</a><a class="button ghost" href="#decision-graph">View decision graph</a></div>';
 hero.appendChild(content);
 const core=document.createElement('div');core.className='intelligence-core';core.setAttribute('aria-hidden','true');
 core.innerHTML='<svg viewBox="0 0 260 220" focusable="false"><path class="core-wave" d="M0 115 Q25 115 40 102 T70 120 T100 90 T135 125 T170 100 T210 115 T260 105"/><g class="core-ring ring-outer"><circle cx="130" cy="110" r="86"/><circle class="core-particle" cx="130" cy="24" r="3"/><circle class="core-particle" cx="130" cy="196" r="2"/></g><g class="core-ring ring-inner"><ellipse cx="130" cy="110" rx="65" ry="65"/><circle class="core-particle" cx="195" cy="110" r="3"/></g><circle class="core-halo" cx="130" cy="110" r="45"/><rect class="core-mark" x="103" y="83" width="54" height="54" rx="17"/><path class="core-letter" d="M118 125V95h6l14 20V95h5v30h-6l-14-20v20z"/><circle class="core-signal" cx="192" cy="51" r="4"/></svg>';
 hero.appendChild(core);
 let stateData=globalThis.nitiSessionSnapshot || null;
 function update(){
  const home=!location.hash || location.hash==='#overview';hero.classList.toggle('home-hero',home);content.hidden=!home;core.hidden=!home;
  if(!home)return;
  const hour=Number(new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Kolkata',hour:'2-digit',hour12:false}).format(new Date()));
  heading.textContent=`Good ${hour<12?'morning':hour<17?'afternoon':'evening'}, Subham.`;
  const state=homeHeroState(stateData);hero.dataset.state=state.state;message.textContent=state.message;
  document.getElementById('hero-desk-link').textContent=state.review?'Review position':'Open trading desk';
 }
 document.addEventListener('niti-session',()=>{stateData=globalThis.nitiSessionSnapshot;update();});
 window.addEventListener('hashchange',update);
 let visible=true;const pause=()=>hero.classList.toggle('core-paused',!visible||document.hidden);
 const observer=new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;pause();});observer.observe(hero);
 document.addEventListener('visibilitychange',()=>{pause();update();});
 setInterval(()=>{if(!document.hidden)update();},60000);
 update();
})();
