const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function setup() {
  const nodes = {};
  const element = () => ({value:'', disabled:true, textContent:'', listeners:{},
    addEventListener(name, fn){this.listeners[name] = fn}, replaceChildren(...children){this.children = children},
    setAttribute(k,v){(this.attributes ||= {})[k]=v}, appendChild(n){(this.children ||= []).push(n)}});
  const doc = {hidden:false, getElementById:id=>nodes[id] ||= element(), createElement:element, createElementNS:element};
  ['bn-amount','bn-loss','bn-profit'].forEach(id=>doc.getElementById(id));
  const calls = [], timers = [];
  const data = {available:true, model:'test', session:null, events:[], decisions:[]};
  const context = vm.createContext({document:doc,location:{protocol:'https:'},
    setInterval:fn=>timers.push(fn),
    call:async(path, options)=>{calls.push({path,options});return data;}});
  vm.runInContext(fs.readFileSync('src/kiwit/static/banknifty.js','utf8'),context);
  return {nodes,calls,timers,data,context};
}
const settle = () => new Promise(resolve=>setImmediate(resolve));
test('playbook plans, rejection reasons and evidence remain text-only and clearly experimental',async()=>{
  const {nodes,data,timers}=setup();await settle();
  data.playbooks=[{id:'test_v1',name:'<img onerror=bad()>'}];
  data.paper_review=[{playbook_id:'test_v1',closed_trades:1,winning_trades:0,closed_net_pnl:'-40',partially_exited_trades:1,realized_pnl_including_partial:'-60'}];
  data.session={state:'running',strategy_selection:{version:'selector-v1',at:new Date().toISOString(),evaluations:[{playbook_id:'test_v1',eligible:false,reasons:['5m/15m conflict']}],plans:[{id:'abc',playbook_id:'test_v1',symbol:'BANKNIFTY',quantity:30,expires_at:'2026-01-01T00:00:00Z',underlying_trigger:55000,underlying_invalidation:54900,underlying_max_chase:55050,max_fill:'101',planned_stop:'95',planned_target:'110'}]}};
  timers[0]();await settle();
  assert.match(nodes['bn-playbooks'].children[0].textContent,/5m\/15m conflict/);
  assert.match(nodes['bn-entry-plans'].children[0].textContent,/EXPIRED/);
  assert.match(nodes['bn-playbook-review'].children[0].textContent,/UNVALIDATED.*Closed trades 1/);
  assert.match(nodes['bn-playbook-review'].children[0].textContent,/Partial exits still open 1/);
  assert.equal(nodes['bn-playbook-review'].children[0].innerHTML,undefined);
  assert.match(nodes['bn-playbook-review'].children[0].textContent,/<img/);
});
test('Run posts capital and limits once, with no extra approval dialog',async()=>{
  const {nodes,calls}=setup(); await settle();
  assert.equal(nodes['bn-run'].disabled,false);
  nodes['bn-amount'].value='100000';nodes['bn-loss'].value='5';nodes['bn-profit'].value='10';
  await nodes['bn-form'].listeners.submit({preventDefault(){}});
  const posts=calls.filter(c=>c.path.endsWith('/run'));
  assert.equal(posts.length,1);
  assert.deepEqual(JSON.parse(posts[0].options.body),{amount:'100000',loss_pct:'5',profit_pct:'10'});
});
test('active session enables Stop and summaries are rendered as text',async()=>{
  const {nodes,data,timers}=setup();await settle();
  data.session={state:'running',detail:'<script>bad()</script>',cash:'100',pnl:'0',entries:0,position:null};
  data.decisions=[{state:'applied',result:{decision:{action:'HOLD',summary:'<img onerror=bad()>'}}}];
  timers[0]();await settle();
  assert.equal(nodes['bn-run'].disabled,true);assert.equal(nodes['bn-stop'].disabled,false);
  assert.match(nodes['bn-decisions'].children[0].textContent,/<img/);
  assert.equal(nodes['bn-decisions'].children[0].innerHTML,undefined);
});
test('unavailable server keeps new runs disabled with a visible explanation',async()=>{
  const {nodes,context,timers}=setup();await settle();
  context.call=async()=>{throw Error('Offline')};timers[0]();await settle();
  assert.equal(nodes['bn-run'].disabled,true);assert.match(nodes['bn-detail'].textContent,/Offline/);
});
test('chart and evidence render safely, support timeframes and label stale data',async()=>{
  const {nodes,data,timers}=setup();await settle();
  const bar={at:'2026-01-01T09:30:00+05:30',open:100,high:105,low:99,close:103};
  data.session={state:'running',chart_analysis:{at:bar.at,ready:true,summary:'<script>bad()</script>',
    timeframes:{'5m':{regime:'uptrend'}},issues:[],patterns:[{name:'<img onerror=bad()>',direction:'bullish',timeframe:'5m'}],
    chart_bars:{'5m':[bar],'1m':[bar]}}};
  timers[0]();await settle();
  assert.match(nodes['bn-chart-summary'].textContent,/STALE/);
  assert.match(nodes['bn-patterns'].children[0].textContent,/<img/);
  assert.equal(nodes['bn-patterns'].children[0].innerHTML,undefined);
  assert.equal(nodes['bn-chart'].children[0].attributes.role,'img');
  nodes['bn-timeframe'].value='1m';nodes['bn-timeframe'].listeners.change();
  assert.match(nodes['bn-chart'].children[0].attributes['aria-label'],/1m/);
});
test('previous calendar week and missing coverage are visible separately',async()=>{
  const {nodes,data,timers}=setup();await settle();
  const week={period_start:'2026-08-17',period_end:'2026-08-21',trend:'upward_bias',
    coverage:{status:'complete',partial_sessions:[],absent_weekdays_unverified:[]},
    ohlc:{open:100,high:110,low:99,close:108},return_pct:8,range_pct:11,
    structure:{higher_closes:4,lower_closes:0,higher_highs:4,higher_lows:4,lower_highs:0,lower_lows:0}};
  data.session={state:'running',chart_analysis:{at:new Date().toISOString(),ready:true,summary:'Weekly context',
    timeframes:{},patterns:[],previous_calendar_week:week,
    weekly_alignment:{alignment:'aligned',price_location:'inside_previous_week_range'}}};
  timers[0]();await settle();
  let text=nodes['bn-context'].children.map(n=>n.textContent).join('\n');
  assert.match(text,/PREVIOUS CALENDAR WEEK · 2026-08-17 to 2026-08-21/);
  assert.match(text,/Open-to-close return 8%/);
  assert.match(text,/15m vs previous week: aligned/);
  week.coverage.status='incomplete';week.coverage.absent_weekdays_unverified=['2026-08-17'];
  week.coverage.partial_sessions=['2026-08-18'];week.ohlc=null;week.structure=null;
  week.trend='insufficient_data';data.session.chart_analysis.ready=false;
  timers[0]();await settle();
  text=nodes['bn-context'].children.map(n=>n.textContent).join('\n');
  assert.match(text,/Not assumed to be holidays/);
  assert.match(text,/Partial weekly sessions: 2026-08-18/);
  assert.match(nodes['bn-chart-summary'].textContent,/new entries blocked/);
});
