/* Read-only evidence graph. Links represent grouping, not proven causation. */
(() => {
 const root=document.getElementById('decision-graph');
 const svg=root.querySelector('svg'), detail=document.getElementById('graph-detail');
 const ns='http://www.w3.org/2000/svg';
 let data=null, refreshing=false;
 const node=(tag,attrs,text)=>{const el=document.createElementNS(ns,tag);Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,v));if(text!==undefined)el.textContent=text;return el;};
 function render(){
  svg.replaceChildren();
  const s=data?.session;
  const items=[['Session',s ? {day:s.day,state:s.state,detail:s.detail} : {status:'No session loaded'},450,250,-1],
   ['Market context',s?.chart_analysis || {status:'No market analysis recorded'},220,130,0],
   ['Strategy scan',s?.strategy_selection || {status:'No strategy scan recorded'},680,130,0],
   ['Paper position',s?.position || {status:'No open position'},680,390,0],
   ['AI decisions',data?.decisions || [],220,390,0]];
  (s?.chart_analysis?.patterns || []).slice(0,4).forEach((x,i)=>items.push([x.name||'Pattern',x,70+i*100,45,1]));
  (s?.strategy_selection?.evaluations || []).slice(0,4).forEach((x,i)=>items.push([x.playbook_id||'Playbook',x,540+i*100,45,2]));
  (data?.decisions || []).slice(0,4).forEach((x,i)=>items.push([x.result?.decision?.action||x.state||'Decision',x,70+i*100,510,4]));
  items.forEach((x,i)=>{if(x[4]>=0){const p=items[x[4]];svg.appendChild(node('line',{x1:p[2],y1:p[3],x2:x[2],y2:x[3],class:'graph-edge'}));}});
  items.forEach((x,i)=>{
   const g=node('g',{tabindex:0,role:'button','aria-label':x[0],class:'graph-node'});
   g.appendChild(node('circle',{cx:x[2],cy:x[3],r:i===0?32:i<5?23:12,class:i===0?'graph-center':'graph-dot'}));
   g.appendChild(node('text',{x:x[2],y:x[3]+(i<5?49:28),'text-anchor':'middle'},x[0].length>23?x[0].slice(0,21)+'…':x[0]));
   const select=()=>{detail.textContent=x[0]+'\n\n'+JSON.stringify(x[1],null,2);svg.querySelectorAll('g').forEach(n=>n.classList.remove('selected'));g.classList.add('selected');};
   g.addEventListener('click',select);g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();select();}});svg.appendChild(g);
  });
 }
 async function refresh(){
  if(refreshing)return;refreshing=true;
  const button=document.getElementById('graph-refresh');button.disabled=true;
  const status=document.getElementById('graph-status');
  try{data=await call('/api/v1/banknifty/status');status.textContent='Session evidence loaded · '+new Date().toLocaleTimeString('en-IN')+' · Connections show recorded groupings, not causal proof.';render();detail.textContent='Select a node to inspect its recorded evidence.';}
  catch(e){data=null;render();status.textContent='Evidence unavailable: '+e.message;detail.textContent='No live evidence loaded. The graph shows the available categories only.';}
  finally{refreshing=false;button.disabled=false;}
 }
 document.getElementById('graph-refresh').addEventListener('click',refresh);
 window.addEventListener('hashchange',()=>{if(location.hash==='#decision-graph')refresh();});
 render();if(location.hash==='#decision-graph')refresh();
})();
