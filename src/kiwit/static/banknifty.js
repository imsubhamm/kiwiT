/* Text-only rendering: model summaries and provider data are untrusted. */
(() => {
  const el = id => document.getElementById(id);
  let busy = false;
  let syncing = false;
  let chartAnalysis = null;
  const lines = (id, values) => {
    el(id).replaceChildren(...values.map(value => {
      const p = document.createElement('p'); p.textContent = value; return p;
    }));
  };
  function renderChart() {
    const frame = el('bn-timeframe').value || '5m';
    const bars = chartAnalysis?.chart_bars?.[frame] || [];
    const container = el('bn-chart'); container.replaceChildren();
    if (!bars.length) { container.textContent = 'No completed candles available yet.'; return; }
    const svgNode = (name, attributes, content) => {
      const node = document.createElementNS('http://www.w3.org/2000/svg', name);
      Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
      if (content !== undefined) node.textContent = content;
      return node;
    };
    const svg = svgNode('svg', {viewBox:'0 0 780 260', role:'img', 'aria-label':`Bank Nifty ${frame} completed candles`});
    const levels = [
      ['PDH', chartAnalysis.previous_day?.high], ['PDL', chartAnalysis.previous_day?.low],
      ['ORH', chartAnalysis.opening_range?.high], ['ORL', chartAnalysis.opening_range?.low],
    ].filter(([,v]) => Number.isFinite(v));
    const values = bars.flatMap(b => [b.high,b.low]).concat(levels.map(([,v]) => v));
    const lo = Math.min(...values), hi = Math.max(...values), span = Math.max(hi-lo,1);
    const y = price => 215 - (price-lo)/span*190;
    const width = 655/bars.length;
    svg.appendChild(svgNode('title', {}, `Bank Nifty ${frame}. Source: completed Groww candles. Not a live tick chart.`));
    for(let i=0;i<4;i++) {
      const price = lo+span*i/3, pos=y(price);
      svg.appendChild(svgNode('line',{x1:10,x2:670,y1:pos,y2:pos,class:'bn-grid'}));
      svg.appendChild(svgNode('text',{x:675,y:pos+4,class:'bn-axis'},price.toFixed(2)));
    }
    levels.forEach(([label,price]) => {
      svg.appendChild(svgNode('line',{x1:10,x2:665,y1:y(price),y2:y(price),class:'bn-level'}));
      svg.appendChild(svgNode('text',{x:12,y:y(price)-3,class:'bn-axis'},`${label} ${price.toFixed(2)}`));
    });
    bars.forEach((b,i) => {
      const x=15+(i+0.5)*width, cls=b.close>=b.open?'bn-up':'bn-down';
      svg.appendChild(svgNode('line',{x1:x,x2:x,y1:y(b.high),y2:y(b.low),class:cls}));
      const rect=svgNode('rect',{x:x-width*0.3,y:Math.min(y(b.open),y(b.close)),width:Math.max(width*0.6,1),height:Math.max(Math.abs(y(b.open)-y(b.close)),1),class:cls});
      rect.appendChild(svgNode('title',{},`${b.at} O ${b.open} H ${b.high} L ${b.low} C ${b.close}`));
      svg.appendChild(rect);
    });
    const stamp = b => new Date(b.at).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'});
    svg.appendChild(svgNode('text',{x:10,y:248,class:'bn-axis'},stamp(bars[0])));
    svg.appendChild(svgNode('text',{x:665,y:248,'text-anchor':'end',class:'bn-axis'},stamp(bars[bars.length-1])+' IST'));
    container.appendChild(svg);
  }
  function renderAnalysis(analysis) {
    chartAnalysis = analysis || null;
    if (!analysis) {
      el('bn-chart-summary').textContent='No analysis yet. Run starts scanning during market hours.';
      lines('bn-context', []); lines('bn-patterns', []); renderChart(); return;
    }
    const age = (Date.now()-Date.parse(analysis.at))/1000;
    const quality = age>120 || age<0 ? 'STALE — historical display only' : analysis.ready ? 'Context ready' : 'Incomplete — new entries blocked';
    el('bn-chart-summary').textContent=`${quality} · ${analysis.at} · ${analysis.summary}`;
    const context = Object.entries(analysis.timeframes).map(([frame,m])=>`${frame}: ${m.regime} · EMA9 ${m.ema9 ?? 'warming up'} · EMA21 ${m.ema21 ?? 'warming up'} · ATR14 ${m.atr14 ?? 'warming up'} · RSI14 ${m.rsi14 ?? 'warming up'}`);
    if(analysis.week) context.push(`Prior sessions: ${analysis.week.sessions.join(', ')} · Return ${analysis.week.return_pct}% · High ${analysis.week.high} · Low ${analysis.week.low}`);
    if(analysis.previous_day) context.push(`Previous day: high ${analysis.previous_day.high} · low ${analysis.previous_day.low} · close ${analysis.previous_day.close} · Opening gap ${analysis.gap_pct}%`);
    lines('bn-context', context.concat(analysis.issues || []));
    lines('bn-patterns', analysis.patterns.length ? analysis.patterns.map(p=>`${p.name} · ${p.direction} · ${p.timeframe} · ${p.strategy} · Level ${p.level} · Invalidation ${p.invalidation} · Detected ${p.at}`) : ['No active confirmed setup. Waiting is a valid decision.']);
    renderChart();
  }
  async function sync() {
    if (syncing || location.protocol === 'file:') return;
    syncing = true;
    try {
      const data = await call('/api/v1/banknifty/status');
      const s = data.session;
      renderAnalysis(s?.chart_analysis);
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
      el('bn-chart-summary').textContent = 'Connection unavailable — displayed chart may be stale. No fresh analysis confirmed.';
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
  el('bn-timeframe').addEventListener('change', renderChart);
  setInterval(() => {if (!document.hidden && !busy) sync();}, 15000);
  sync();
})();
