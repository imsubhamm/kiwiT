// Interaction layer. Live orders are deliberately not exposed here.
let syncInFlight = null;
let nextRefreshAt = 0;
let authenticated = false;
let lastFeed = null;
let reviewInFlight = false;
let runInFlight = false;
const refreshEveryMs = 30000;
const formatTime = value => value ? new Date(value).toLocaleString('en-IN', {timeZone:'Asia/Kolkata'}) + ' IST' : 'Not recorded';

function setActiveSection(id) {
  document.querySelectorAll('.nav-item').forEach(link => {
    const active = link.getAttribute('href') === `#${id}`;
    link.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'location');
    else link.removeAttribute('aria-current');
  });
}

function renderSignalContext(data) {
  renderSession(data.session, data.available);
  lastFeed = data.freshness;
  const signals = data.signals || [];
  $('signal-help').textContent = !data.available
    ? 'Signal service unavailable. Approvals cannot be submitted.'
    : data.session && ['armed','running','stopping'].includes(data.session.state)
      ? 'Run owns strategy selection and paper entries/exits. No per-trade approval is needed. Use Stop & exit to end the run.'
    : signals.some(signal => signal.status === 'pending')
      ? 'Review a pending card below. Approval simulates a purchase; it does not place a Groww order.'
      : 'No pending signal to approve or reject. The observer needs at least 20 stored quotes and a matching setup; no trade is guaranteed. This experimental observer is not an approved router strategy.';
  const delivery = data.notifications || {};
  $('delivery-summary').textContent = `Signal emails: ${delivery.sent || 0} sent · ${delivery.failed || 0} failed · ${delivery.not_configured || 0} not configured. Counts reflect attempts, not inbox receipt.`;
  const record = data.reconciliations?.[0];
  $('reconciliation-summary').textContent = record
    ? `Latest reconciliation: ${record.trading_date} · ${record.state} · ${record.open_positions} open · ${record.pending_signals} pending · P&L ₹${money(record.realized_pnl)}`
    : 'No daily reconciliation recorded yet. Scheduled after 15:30 IST.';
  updateFeedAge();
}

function renderSession(session, available = true) {
  const active = session && ['armed','running','stopping'].includes(session.state);
  const today = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Kolkata'}).format(new Date());
  const usedToday = session?.trading_date === today;
  $('run-session').disabled = !available || runInFlight || active || usedToday;
  $('stop-session').disabled = !available || runInFlight || !active || session.state === 'stopping';
  for (const [id, key] of [['run-amount','amount'],['run-loss','loss_pct'],['run-profit','profit_pct']]) {
    $(id).disabled = Boolean(active || usedToday || runInFlight);
    if (session && (active || usedToday)) $(id).value = session[key];
  }
  $('run-state').textContent = !available ? 'Session service unavailable' : session ? `PAPER · ${session.state.toUpperCase()} · ${session.trading_date}` : 'Ready for your Run approval';
  $('run-detail').textContent = session?.detail || 'Enter an amount and limits, then click Run once. The server handles matching setups and exits.';
  $('run-pnl').textContent = session ? `Net session P&L ₹${money(session.pnl)} · allocated ₹${money(session.amount)} · deployed ₹${money(session.exposure)} · ${session.entries} entries · ${session.open_positions} open${session.valuation_fresh ? '' : ' · STALE VALUATION — not current'}` : 'Session P&L —';
}

async function sessionAction(event, action) {
  event?.preventDefault();
  if (runInFlight) return;
  runInFlight = true;
  $('run-session').disabled = $('stop-session').disabled = true;
  $('run-detail').textContent = action === 'run' ? 'Saving your paper-session approval…' : 'Requesting stop; exits require fresh market quotes…';
  try {
    const body = action === 'run' ? JSON.stringify({amount:$('run-amount').value,loss_pct:$('run-loss').value,profit_pct:$('run-profit').value}) : undefined;
    const data = await call(`/api/v1/intraday/session/${action}`, {method:'POST',body});
    renderSession(data);
    $('action-status').textContent = action === 'run' ? 'Run approved. Automatic paper decisions begin on the next worker tick; no per-trade clicks.' : 'Stop requested. No further session entries; fresh-quote exits are monitored by the server.';
  } catch(error) {showError(error); $('run-detail').textContent = error.message}
  finally {runInFlight = false}
  await refresh();
}

function updateFeedAge() {
  if (!lastFeed) return;
  const quote = lastFeed.instruments?.[0];
  const age = quote?.observed_at ? Math.floor((Date.now() - new Date(quote.observed_at).getTime()) / 1000) : null;
  $('feed-age').textContent = age === null ? 'No quote' : `${age}s`;
  $('feed-times').textContent = `Last quote: ${formatTime(quote?.observed_at)} · Worker: ${formatTime(lastFeed.worker?.completed_at || lastFeed.worker?.started_at)}`;
  if (lastFeed.market_window === 'closed') $('feed-status').textContent = 'MARKET CLOSED';
  else if (age === null || age < 0 || age > lastFeed.freshness_limit_seconds) {
    $('feed-status').textContent = 'STALE';
    $('feed-status').parentElement.classList.remove('fresh');
    $('feed-status').parentElement.classList.add('stale');
  }
}

async function refresh() {
  if (syncInFlight) return syncInFlight;
  if (reviewInFlight || runInFlight) return;
  syncInFlight = (async () => {
    const button = $('refresh');
    button.disabled = true;
    button.querySelector('span').textContent = 'Syncing…';
    $('error').textContent = '';
    syncState('Contacting workspace…');
    const account = encodeURIComponent($('account').value);
    const jobs = [
      ['Portfolio', `/api/v1/paper/accounts/${account}`, renderAccount],
      ['Operations', `/api/v1/paper/accounts/${account}/operations`, renderOperations],
      ['Signals', '/api/v1/intraday/status', data => {renderIntraday(data); renderSignalContext(data)}],
      ['Evidence', '/api/v1/research/regime-router', data => {
        $('evidence-status').textContent = `Server snapshot: ${data.strategy_id} v${data.version} · ${data.decision} · data through ${data.dataset.end}. Checked ${formatTime(new Date())}.`;
      }],
    ];
    const results = await Promise.allSettled(jobs.map(async ([, path, render]) => render(await call(path))));
    const failures = results.flatMap((result, index) => result.status === 'rejected' ? [`${jobs[index][0]}: ${result.reason?.message || 'Unavailable'}`] : []);
    if (results[2].status === 'rejected') {
      lastFeed = null;
      $('feed-status').textContent = 'UNAVAILABLE';
      $('pending-signals').innerHTML = '<p class="empty">Signal service unavailable. Previous controls removed; retry sync.</p>';
      $('feed-detail').textContent = 'Could not refresh. Previously displayed values may be old.';
      renderSession(null, false);
    }
    const message = failures.length ? `Partial sync — ${failures.join(' · ')}` : `Synchronized · ${formatTime(new Date())}`;
    $('system-state').textContent = failures.length ? 'Connection needs attention' : 'Workspace connected';
    syncState(message, failures.length ? 'error' : 'success');
    if (failures.length) showError(new Error(message));
  })().catch(showError).finally(() => {
    $('refresh').disabled = false;
    $('refresh').querySelector('span').textContent = 'Sync workspace';
    nextRefreshAt = Date.now() + refreshEveryMs;
    syncInFlight = null;
  });
  return syncInFlight;
}

async function safetyAction(action) {
  const button = $(action);
  button.disabled = true;
  try {
    const body = action === 'halt' ? {reason:$('halt-reason').value} : {operator:$('signed-in-user').value};
    const data = await call(`/api/v1/paper/accounts/${encodeURIComponent($('account').value)}/${action}`, {method:'POST',body:JSON.stringify(body)});
    renderAccount(data);
    $('action-status').textContent = data.execution_halted ? 'New paper entries halted. Existing exits remain monitored.' : 'Account halt released. Strategy and freshness gates still apply.';
  } catch (error) {showError(error)} finally {button.disabled = false}
}

async function reviewSignal(event) {
  const button = event.target.closest('.signal-review');
  if (!button || reviewInFlight) return;
  // Freeze poll-driven DOM replacement while the operator confirms/submits.
  reviewInFlight = true;
  try {
    const action = button.dataset.action;
    if (action === 'approve' && !confirm('Create this PAPER trade? No Groww order will be placed.')) return;
    document.querySelectorAll('.signal-review').forEach(control => {control.disabled = true});
    await call(`/api/v1/intraday/signals/${encodeURIComponent(button.dataset.id)}/${action}`, {
      method:'POST', body:JSON.stringify({reason:action === 'approve' ? 'Operator approved paper simulation' : 'Operator rejected signal'}),
    });
    $('action-status').textContent = action === 'approve' ? 'Paper entry recorded. No Groww order was placed.' : 'Signal rejected.';
  } catch (error) {showError(error)} finally {
    reviewInFlight = false;
    document.querySelectorAll('.signal-review').forEach(control => {control.disabled = false});
    nextRefreshAt = Date.now() + refreshEveryMs;
  }
  await refresh();
}

async function retrieveInsight() {
  const query = $('query').value.trim();
  if (!query) {showError(new Error('Enter a research question first.')); return}
  $('search').disabled = true;
  try {
    const data = await call('/api/v1/research/search', {method:'POST',body:JSON.stringify({query,limit:6})});
    $('results').innerHTML = data.hits.length ? data.hits.map(hit => `<article class="result"><b>${escapeText(hit.citation)}</b><p>${escapeText(hit.content)}</p></article>`).join('') : '<p class="empty">No matching evidence.</p>';
  } catch (error) {showError(error)} finally {$('search').disabled = false}
}

document.querySelectorAll('a[href^="#"]').forEach(link => {
  const section = document.getElementById(link.getAttribute('href').slice(1));
  if (!section) return;
  link.addEventListener('click', event => {
    event.preventDefault();
    history.replaceState(null, '', link.getAttribute('href'));
    section.setAttribute('tabindex', '-1');
    section.scrollIntoView({behavior:'instant', block:'start'});
    section.focus({preventScroll:true});
    setActiveSection(section.id);
  });
});
$('refresh').addEventListener('click', refresh);
$('run-form').addEventListener('submit', event => sessionAction(event, 'run'));
$('stop-session').addEventListener('click', event => sessionAction(event, 'stop'));
$('halt').addEventListener('click', () => safetyAction('halt'));
$('resume').addEventListener('click', () => safetyAction('resume'));
$('pending-signals').addEventListener('click', reviewSignal);
$('search').addEventListener('click', retrieveInsight);
$('query').addEventListener('keydown', event => {if (event.key === 'Enter') retrieveInsight()});
$('logout').addEventListener('click', async () => {
  try {await call('/api/v1/auth/logout', {method:'POST'}); location.assign('/login')} catch (error) {showError(error)}
});
document.addEventListener('visibilitychange', () => {if (!document.hidden && authenticated) refresh()});
setInterval(() => {
  updateClock();
  updateFeedAge();
  $('refresh-status').textContent = !authenticated ? 'Waiting for sign-in' : document.hidden ? 'Display polling paused while tab is hidden' : syncInFlight ? 'Sync in progress…' : `Auto-refresh in ${Math.max(0, Math.ceil((nextRefreshAt - Date.now()) / 1000))}s · server observes every minute`;
  if (authenticated && !document.hidden && !reviewInFlight && Date.now() >= nextRefreshAt) refresh();
}, 1000);
if (location.protocol !== 'file:') {
  $('preview-notice').hidden = true;
  // The signal desk currently serves one configured paper account, not arbitrary accounts.
  $('account').readOnly = true;
  $('account').title = 'Single-account paper workspace. Account switching is not supported yet.';
  call('/api/v1/auth/me').then(user => {authenticated = true; $('signed-in-user').value = user.email; refresh()}).catch(showError);
}
