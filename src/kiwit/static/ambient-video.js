(() => {
 const video=document.getElementById('ambient-video');
 const reduced=window.matchMedia('(prefers-reduced-motion: reduce)');
 function update(){
  if(reduced.matches || document.hidden || navigator.connection?.saveData){video.pause();return;}
  if(!video.getAttribute('src'))video.src='/static/media/network-4k.mp4';
  video.play().catch(()=>{});
 }
 reduced.addEventListener('change',update);
 document.addEventListener('visibilitychange',update);
 update();
})();
