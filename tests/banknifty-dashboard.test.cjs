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
