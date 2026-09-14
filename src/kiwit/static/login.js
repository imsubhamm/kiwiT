const form=document.getElementById('login-form'),error=document.getElementById('login-error');
form.addEventListener('submit',async event=>{
 event.preventDefault();const button=form.querySelector('button');if(button.disabled)return;
 error.textContent='';button.disabled=true;
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);
 try{
  const response=await fetch('/api/v1/auth/login',{method:'POST',signal:controller.signal,headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.getElementById('email').value,password:document.getElementById('password').value})});
  if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(typeof body.detail==='string'?body.detail:'Sign-in failed');}
  location.assign('/dashboard');
 }catch(reason){error.textContent=reason.name==='AbortError'?'Sign-in timed out. Please try again.':reason.message;}
 finally{clearTimeout(timer);button.disabled=false;}
});
