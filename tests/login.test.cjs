const {test}=require('node:test');const assert=require('node:assert/strict');const vm=require('node:vm');const fs=require('node:fs');
test('login timeout recovers the button and duplicate submits are ignored',async()=>{
 let submit,timer,requests=0;const button={disabled:false},error={textContent:''};
 const form={addEventListener:(_,fn)=>submit=fn,querySelector:()=>button};
 vm.runInNewContext(fs.readFileSync('src/kiwit/static/login.js','utf8'),{
 document:{getElementById:id=>id==='login-form'?form:id==='login-error'?error:{value:'test'}},
 AbortController,setTimeout:fn=>{timer=fn;return 1},clearTimeout(){},location:{assign(){throw Error('Unexpected redirect')}},
 fetch:(_,options)=>{requests++;return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{const e=new Error();e.name='AbortError';reject(e)}))}
 });
 const first=submit({preventDefault(){}});await submit({preventDefault(){}});assert.equal(requests,1);timer();await first;assert.equal(button.disabled,false);assert.match(error.textContent,/timed out/);
});
