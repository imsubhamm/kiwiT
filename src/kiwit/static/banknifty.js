/* Text-only rendering: model summaries and provider data are untrusted. */
(() => {
  const el = id => document.getElementById(id);
  let busy = false;
  let syncing = false;
  const lines = (id, values) => {
    el(id).replaceChildren(...values.map(value => {
      const p = document.createElement('p'); p.textContent = value; return p;
    }));
  };
  async function sync() {
    if (syncing || location.protocol === 'file:') return;
    syncing = true;
    try {
      const data = await call('/api/v1/banknifty/status');
      const s = data.session;
      if (data.available) {
        const legacy = el('run-form');
        if (legacy) legacy.hidden = true;
      }
      const today = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Kolkata', year:'numeric', month:'2-digit', day:'2-digit'}).format(new Date());
      const usedToday = Boolean(s && s.day === today);
      el('bn-run').disabled = busy || !data.available || usedToday || Boolean(s && s.state !== 'completed');
      el('bn-run').title = usedToday ? 'Today’s Run already exists; limits cannot be reset.' : '';
      el('bn-stop').disabled = busy || !s || s.state === 'completed';
      el('bn-state').textContent = `${data.model || 'AI'} · ${s ? s.state : 'Not running'} · PAPER ONLY`;
      el('bn-detail').textContent = s ? s.detail : data.available ? 'Ready. Set your limits and click Run.' : 'AI desk is not enabled on this server yet.';
      el('bn-totals').textContent = s ? `Paper cash ₹${s.cash} · P&L ₹${s.pnl}${s.valuation_fresh === false ? ' (STALE valuation)' : ''} · Entries ${s.entries} · ${s.position ? s.position.contract.symbol + ' × ' + s.position.quantity : 'No open position'} · Worker ${s.last_tick || 'not seen yet'}` : 'No paper session';
      el('bn-budget').textContent = data.budget ? `API budget used/reserved $${data.budget.used_or_reserved_usd} / $${data.budget.trial_limit_usd} trial · $${data.budget.daily_limit_usd}/day · conservative estimate, not invoice` : 'Budget unavailable';
      lines('bn-decisions', (data.decisions || []).map(d => `${d.at} · ${d.state} · ${d.result?.decision ? d.result.decision.action + ': ' + d.result.decision.summary : 'No usable AI decision'}`));
      lines('bn-events', (data.events || []).slice(0, 15).map(e => `${e.at} · ${e.kind} · ${JSON.stringify(e.detail)}`));
    } catch (error) {
      el('bn-detail').textContent = 'AI desk unavailable: ' + error.message;
      el('bn-run').disabled = true;
      // Stop stays available after a polling failure when a session was loaded.
    } finally { syncing = false; }
  }
  async function action(event, name) {
    event.preventDefault(); if (busy) return; busy = true;
    el('bn-run').disabled = el('bn-stop').disabled = true;
    try {
      const body = name === 'run' ? JSON.stringify({amount: el('bn-amount').value, loss_pct: el('bn-loss').value, profit_pct: el('bn-profit').value}) : undefined;
      await call('/api/v1/banknifty/' + name, {method: 'POST', body});
    } catch (error) { el('bn-detail').textContent = error.message; busy = false; return; }
    finally { busy = false; }
    await sync();
  }
  el('bn-form').addEventListener('submit', event => action(event, 'run'));
  el('bn-stop').addEventListener('click', event => action(event, 'stop'));
  el('refresh').addEventListener('click', sync);
  setInterval(() => {if (!document.hidden && !busy) sync();}, 15000);
  sync();
})();
