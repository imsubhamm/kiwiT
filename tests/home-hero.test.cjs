const {test}=require('node:test');const assert=require('node:assert/strict');
const {homeHeroState}=require('../src/kiwit/static/home-hero.js');
const now=new Date('2026-09-14T05:00:00Z');
test('hero never invents monitoring when disconnected or stale',()=>{
 assert.equal(homeHeroState(null,now).state,'neutral');
 assert.equal(homeHeroState({available:true,session:{state:'running',day:'2026-09-01'}},now).state,'neutral');
});
test('position attention takes priority and changes action',()=>{
 assert.equal(homeHeroState({available:false,session:{state:'stopping',position:{}}},now).review,true);
});
test('current worker evidence permits monitoring and empty setup messaging',()=>{
 const session={state:'running',day:'2026-09-14',last_tick:now.toISOString()};
 assert.equal(homeHeroState({available:true,session},now).state,'monitoring');
 session.strategy_selection={at:now.toISOString(),plans:[]};
 assert.match(homeHeroState({available:true,session},now).message,/No qualified/);
});
