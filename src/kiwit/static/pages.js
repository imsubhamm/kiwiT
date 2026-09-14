/* Route existing panels into distinct views while preserving their data bindings. */
(() => {
  const main = document.getElementById('overview');
  const routes = {
    'decision-graph': ['Decision Graph', 'Explore connections in recorded session evidence.'],
    overview: ['Home', 'Your paper workspace at a glance.'],
    banknifty: ['Bank Nifty', 'Manage your simulated options session and review market evidence.'],
    portfolio: ['Cash portfolio', 'Legacy cash positions, account totals and observer history.'],
    approval: ['Archived strategies', 'Historical research results and approval evidence.'],
    operations: ['Operations', 'Review service evidence, reports and incidents.'],
    research: ['Research', 'Explore trading research with source citations.'],
    safety: ['Risk controls', 'Manage execution halts for your paper workspace.'],
  };
  const pages = {};
  for (const key of Object.keys(routes)) {
    const page = document.createElement('div');
    page.className = 'route-page'; page.dataset.route = key;
    page.hidden = true;
    main.insertBefore(page, document.getElementById('error'));
    pages[key] = page;
    if (key !== 'overview') page.appendChild(document.getElementById(key));
  }
  pages.portfolio.appendChild(document.getElementById('signals'));
  // Remove the now-empty containers, including old disclosure wrappers.
  main.querySelectorAll(':scope > .workspace-section, :scope > .content-grid').forEach(node => node.remove());
  const home = document.createElement('section');
  home.className = 'home-summary';
  home.innerHTML = '<span class="eyebrow">BANK NIFTY · PAPER ONLY</span><h3>Session overview</h3><div id="home-status"></div><div id="home-metrics" class="desk-metrics"></div><a class="button primary" href="#banknifty">Open trading desk →</a>';
  pages.overview.appendChild(home);
  const quick = document.createElement('div');quick.className = 'home-links';
  quick.innerHTML = '<a href="#portfolio"><strong>Cash portfolio →</strong><span>Legacy positions and history</span></a><a href="#research"><strong>Research →</strong><span>Search evidence and sources</span></a><a href="#operations"><strong>Operations →</strong><span>Reports and service evidence</span></a>';
  pages.overview.appendChild(quick);
  const syncHome = () => {
    document.getElementById('home-status').textContent = document.getElementById('bn-readiness').textContent;
    const target = document.getElementById('home-metrics'); target.replaceChildren();
    for (const source of document.querySelectorAll('#banknifty .desk-metrics article')) {
      const copy = source.cloneNode(true);copy.querySelectorAll('[id]').forEach(el => el.removeAttribute('id'));target.appendChild(copy);
    }
  };
  new MutationObserver(syncHome).observe(document.getElementById('banknifty'), {childList:true,subtree:true,characterData:true});
  syncHome();
  const show = () => {
    const hash = location.hash.slice(1) || 'overview';
    const key = hash === 'signals' ? 'portfolio' : routes[hash] ? hash : 'overview';
    Object.entries(pages).forEach(([id,page]) => {page.hidden = id !== key;});
    const title = document.querySelector('.workspace-intro h2');
    title.textContent = routes[key][0];
    document.querySelector('.workspace-intro p').textContent = routes[key][1];
    document.title = `${routes[key][0]} · kiwiT`;
    document.querySelectorAll('.nav-item').forEach(link => {
      const active = link.getAttribute('href') === '#'+key;
      link.classList.toggle('active', active);
      if(active) link.setAttribute('aria-current','page'); else link.removeAttribute('aria-current');
    });
    window.scrollTo(0,0);
    title.setAttribute('tabindex','-1');title.focus({preventScroll:true});
  };
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href^="#"]');
    if(!link) return;
    const key = link.getAttribute('href').slice(1);
    if(!routes[key] && key !== 'signals') return;
    event.preventDefault();event.stopImmediatePropagation();
    if(location.hash === '#'+key) show();else location.hash = key;
  }, true);
  window.addEventListener('hashchange',show);
  show();
})();
