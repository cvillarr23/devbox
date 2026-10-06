// One acknowledged link request is owned by one viewer at a time.
(() => {
  const viewer = crypto.randomUUID();
  const panel = document.getElementById('browser-panel');
  const frame = document.getElementById('browser-frame');
  const error = document.getElementById('browser-error');
  const pending = document.createElement('section');
  pending.className = 'widget';
  pending.innerHTML = '<h2>Links from agents</h2><div class="body"></div>';
  document.getElementById('widgets').prepend(pending);
  const rows = pending.querySelector('.body');
  let lastRemote = Date.now()/1000, busy = false;
  const seen = new Set();
  async function browserRequest(method) {
    const r = await fetch('/api/browser', {method, headers:{'Content-Type':'application/json'}, ...(method === 'POST' ? {body:'{}'} : {})});
    const data = await r.json();
    if(!r.ok) throw new Error(data.error || 'Browser unavailable');
    return data;
  }
  async function show(start = true) {
    panel.hidden = false;
    try {
      if(start) await browserRequest('POST');
      if(!frame.getAttribute('src')) frame.src='/browser/vnc.html?autoconnect=true&resize=remote&path=browser/websockify';
      error.textContent='';
    } catch(e) {error.textContent=e.message;}
  }
  document.getElementById('browser-toggle').onclick=()=>show();
  document.getElementById('browser-hide').onclick=()=>{panel.hidden=true;focusTerminal();};
  document.getElementById('browser-stop').onclick=async()=>{
    try {await browserRequest('DELETE');frame.removeAttribute('src');panel.hidden=true;}catch(e){error.textContent=e.message;}
  };
  async function ack(id,status) {await api('/links/ack',{method:'POST',body:{id,viewer,status}});}
  async function open(item, manual) {
    // Reserve a blank tab synchronously during a user click; a delayed open may be blocked.
    const tab = manual ? window.open('about:blank','_blank') : null;
    try {
      const claim = await api('/links/claim',{method:'POST',body:{id:item.id,viewer}});
      if(!claim.ok || !claim.data.claimed){if(tab)tab.close();return;}
      const destination = tab || window.open('about:blank','_blank');
      if(destination){destination.opener=null;destination.location=claim.data.url;await ack(item.id,'opened');}
      else await ack(item.id,'blocked');
    } catch(e) {if(tab)tab.close();}
  }
  async function poll() {
    if(busy)return;busy=true;
    try {
      const active=document.visibilityState==='visible' && document.hasFocus();
      const r=await api(`/links?viewer=${viewer}&active=${active?1:0}`);
      if(!r.ok)return;
      if(r.data.last_remote_open>lastRemote){lastRemote=r.data.last_remote_open;await show(false);}
      rows.replaceChildren();
      for(const item of r.data.links){
        const row=document.createElement('div');row.style.marginBottom='.7rem';
        const text=document.createElement('div');text.textContent=item.url;text.style.overflowWrap='anywhere';
        const button=document.createElement('button');button.className='btn';button.textContent='Open link';button.onclick=()=>open(item,true);
        row.append(text,button);rows.append(row);
        if(item.status==='pending' && item.can_claim && !seen.has(item.id)) {seen.add(item.id);await open(item,false);}
      }
      if(!r.data.links.length)rows.textContent='No pending links';
    } finally {busy=false;}
  }
  poll().catch(()=>{});setInterval(()=>poll().catch(()=>{}),1500);
})();
