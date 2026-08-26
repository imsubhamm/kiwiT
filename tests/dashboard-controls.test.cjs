const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const root = path.join(__dirname, '../src/kiwit/static');
const html = fs.readFileSync(path.join(root, 'dashboard.html'), 'utf8');

class Element {
  constructor(id = '') {
    this.id = id; this.value = ''; this.textContent = ''; this.innerHTML = '';
    this.style = {}; this.dataset = {}; this.attrs = {}; this.listeners = {};
    this.children = {}; this.disabled = false;
    const classes = new Set();
    this.classList = {add:(...items)=>items.forEach(x=>classes.add(x)), remove:(...items)=>items.forEach(x=>classes.delete(x)), toggle:(x,on)=>on?classes.add(x):classes.delete(x), contains:x=>classes.has(x)};
  }
  addEventListener(type, fn) {this.listeners[type] = fn}
  querySelector(selector) {return this.children[selector] ||= new Element()}
  getAttribute(key) {return this.attrs[key] ?? null}
  setAttribute(key, value) {this.attrs[key] = value}
  removeAttribute(key) {delete this.attrs[key]}
  scrollIntoView() {this.scrolled = true}
  focus() {this.focused = true}
}
function setup({failOperations=false, signals=[]} = {}) {
  const nodes = Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],new Element(m[1])]));
  nodes.account.value = 'kiwit-paper-main';
  nodes['feed-status'].parentElement = new Element();
  const links = [...html.matchAll(/class="nav-item[^"]*" href="#([^"]+)"/g)].map(m=>{const e=new Element();e.attrs.href='#'+m[1];return e});
  const calls = [], timers = [];
  const document = {hidden:false,getElementById:id=>nodes[id],querySelectorAll:selector=>selector==='.signal-review'?[]:links,
    addEventListener(){},createElement:()=>{const e=new Element();Object.defineProperty(e,'innerHTML',{get(){return e.textContent.replaceAll('&','&amp;').replaceAll('<','&lt;')}});return e}};
  const account = {account_id:'kiwit-paper-main',cash_balance:'1000',realized_pnl:'0',positions:[],execution_halted:false};
  const feed = {available:true,signals,counts:{pending:signals.length},freshness:{market_window:'entry_window',freshness_limit_seconds:120,instruments:[{last_price:'100',observed_at:new Date().toISOString(),fresh:true}],worker:{state:'completed',started_at:new Date().toISOString()}},audit:[]};
  const context = vm.createContext({document,location:{protocol:'https:',assign(url){this.redirect=url}},history:{replaceState(){}},confirm:()=>true,
    Intl, Date, AbortController, console, setTimeout,clearTimeout,setInterval:fn=>timers.push(fn),
    fetch:async (url,opts)=>{
      calls.push({url,opts});
      if (url.endsWith('/operations') && failOperations) return {ok:false,status:503,json:async()=>({detail:'Database unavailable'})};
      const data = url.endsWith('/me')?{email:'operator@example.test'}:url.includes('/intraday/status')?feed:
        url.includes('regime-router')?{strategy_id:'regime_router',version:'1.0.0',decision:'rejected',dataset:{end:'2026-08-20'}}:
        url.endsWith('/operations')?{}:account;
      return {ok:true,status:200,json:async()=>data};
    }});
  for (const file of ['dashboard.js','dashboard-controls.js']) vm.runInContext(fs.readFileSync(path.join(root,file),'utf8'),context);
  return {context,nodes,links,calls,timers,feed};
}
const settle = () => new Promise(resolve=>setImmediate(resolve));

test('all seven navigation items focus real sections and update active state',async()=>{
  const {links,nodes}=setup(); await settle();
  assert.equal(links.length,7);
  for(const link of links){link.listeners.click({preventDefault(){}});const target=nodes[link.attrs.href.slice(1)];assert.ok(target.scrolled);assert.ok(target.focused);assert.equal(link.attrs['aria-current'],'location');assert.equal(links.filter(l=>l.attrs['aria-current']).length,1)}
});
test('sync survives one failed panel and releases the button',async()=>{
  const {nodes}=setup({failOperations:true}); await settle();
  assert.equal(nodes['feed-price'].textContent,'₹100');
  assert.equal(nodes.refresh.disabled,false);
  assert.match(nodes.connection.querySelector('span').textContent,/Partial sync/);
  assert.match(nodes['action-status'].textContent,/Operations/);
});
test('polling refreshes automatically and does not overlap requests',async()=>{
  const {context,calls,timers}=setup(); await settle();
  const before=calls.length;
  vm.runInContext('nextRefreshAt=0',context);timers[0]();timers[0]();await settle();
  assert.equal(calls.length-before,4);
  context.document.hidden=true;vm.runInContext('nextRefreshAt=0',context);timers[0]();await settle();assert.equal(calls.length-before,4);
});
test('only pending cards contain approval controls; empty state explains why',async()=>{
  const empty=setup();await settle();assert.match(empty.nodes['signal-help'].textContent,/No pending signal/);
  assert.ok(!empty.nodes['pending-signals'].innerHTML.includes('Approve paper buy'));
  const signal={signal_id:'example',symbol:'NIFTYBEES',pattern:'test',regime:'range',side:'buy',signal_at:new Date().toISOString(),entry_price:100,stop_price:95,target_price:110,quantity:1,status:'pending'};
  const pending=setup({signals:[signal]});await settle();assert.match(pending.nodes['pending-signals'].innerHTML,/Approve paper buy/);
  signal.status='rejected';await vm.runInContext('refresh()',pending.context);assert.ok(!pending.nodes['pending-signals'].innerHTML.includes('Approve paper buy'));
});
test('network timeout is bounded, informative, and never automatically resubmits a mutation',async()=>{
  const {context}=setup();await settle();let attempts=0;
  context.setTimeout=(fn)=>setTimeout(fn,1);
  context.fetch=async (url,options)=>{attempts++;return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{const e=new Error();e.name='AbortError';reject(e)}))};
  await assert.rejects(vm.runInContext("call('/test',{method:'POST'})",context),/timed out/);assert.equal(attempts,1);
});
test('stale age is recomputed even without a successful refresh',async()=>{
  const {context,nodes,feed,timers}=setup();await settle();
  feed.freshness.instruments[0].observed_at=new Date(Date.now()-180000).toISOString();
  timers[0]();assert.equal(nodes['feed-status'].textContent,'STALE');
  assert.ok(nodes['feed-status'].parentElement.classList.contains('stale'));
});
