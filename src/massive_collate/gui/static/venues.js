'use strict';
/* Exchanges view (rail icon: a pair of scales): every venue the app tracks symbols on (venues.py),
   grouped on the left: the stock exchanges (NYSE, Nasdaq, Blue Ocean's overnight), CME Group's four
   (CME, NYMEX, CBOT, COMEX), the indices, each crypto exchange, forex. In the middle the venue's
   products (a search, a sector filter); on the right the insight of the venue (open now or not, its
   products by sector, how much of them is collected and how freshly, the feeds and the delivery
   scheme behind it, the last session's movers) or of the product picked (the symbol panel's
   /api/info), with a button to open it in the chart. Asked again every minute while shown. */
(() => {
  const box = document.getElementById('viewExchanges');
  const { esc } = window.MassiveIcons;
  const SHOW = 300;  // product rows drawn at first ("show more" for the rest)
  let data = null, sel = null, list = null, picked = null, info = null, query = '', sector = '', shown = SHOW, timer = null;

  const STATE = { open: 'open', 'pre-market': 'pre-market', 'after-hours': 'after-hours', closed: 'closed' };
  const JOBS = { day: 'daily bars', minute: '1-minute', second: '1-second' };
  const num = (n) => (n == null ? '–' : Number(n).toLocaleString('en-US'));
  const pct = (a, b) => (b ? `${Math.round((a / b) * 100)}%` : '–');
  const price = (v) => (v == null ? '–' : Number(v).toLocaleString('en-US', { maximumFractionDigits: v < 1 ? 6 : 2 }));
  const when = (iso) => (iso ? new Date(iso).toLocaleString('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: false }) + ' ET' : '–');
  const ago = (iso) => {
    if (!iso) return 'never';
    const h = (Date.now() - Date.parse(iso)) / 36e5;
    return h < 1 ? `${Math.max(1, Math.round(h * 60))} min ago` : h < 48 ? `${Math.round(h)} h ago` : `${Math.round(h / 24)} days ago`;
  };
  const chip = (state) => `<span class="ex-state ${esc(state || 'closed')}">${esc(STATE[state] || state || 'closed')}</span>`;
  const change = (v) => (v == null ? '' : `<span class="${v >= 0 ? 'up' : 'down'}">${v >= 0 ? '+' : ''}${v.toFixed(2)}%</span>`);
  const tile = (title, big, small) => `<div class="tile"><h5>${title}</h5><div class="big">${big}</div><small>${small}</small></div>`;
  const venue = () => data?.venues.find((v) => v.id === sel) || null;

  async function load() {
    clearTimeout(timer);
    if (document.body.dataset.view !== 'exchanges' || document.hidden) return;
    try {
      data = await api('/api/venues');
      if (!sel || !data.venues.some((v) => v.id === sel)) { sel = data.venues[0]?.id || null; list = null; }
      if (!list && sel) await loadList();
    } catch (err) { showError(err); }
    render();
    timer = setTimeout(load, 60_000);
  }

  async function loadList() {
    const want = sel;
    try {
      const out = await api('/api/venue', { id: want });
      if (want === sel) list = out;
    } catch (err) { showError(err); }
  }

  async function pick(ticker) {
    picked = ticker; info = null;
    render();
    try {
      info = await api('/api/info', { source: '*', ticker });
    } catch (err) { info = { error: String(err.message || err) }; }
    if (picked === ticker) renderSide();
  }

  // -- left: the venues, by group ---------------------------------------------------------
  function venuesHtml() {
    return data.groups.concat(['Other']).map((g) => {
      const vs = data.venues.filter((v) => v.group === g);
      if (!vs.length) return '';
      return `<h4 class="ex-group">${esc(g)}</h4>` + vs.map((v) => `
        <button type="button" class="ex-venue${v.id === sel ? ' active' : ''}" data-venue="${esc(v.id)}" title="${esc(v.hours)}">
          <span class="ex-name">${esc(v.name)}</span>${chip(v.state)}
          <span class="ex-count">${num(v.products)} ${v.products === 1 ? 'product' : 'products'}</span></button>`).join('');
    }).join('');
  }

  // -- middle: the venue's products --------------------------------------------------------
  function rowsOf() {
    const q = query.trim().toUpperCase();
    return (list?.products || []).filter((p) => (!sector || p.sector === sector)
      && (!q || p.ticker.includes(q) || (p.name || '').toUpperCase().includes(q)));
  }

  function productsHtml() {
    const v = venue();
    if (!v) return '<div class="empty">No exchange tracked yet.</div>';
    if (!list || list.id !== sel) return '<div class="empty">Loading…</div>';
    const rows = rowsOf();
    const sectors = [...new Set(list.products.map((p) => p.sector))].sort();
    return `<div class="ex-tools"><input type="search" id="exSearch" placeholder="Search ${esc(v.name)}" value="${esc(query)}">
        <select id="exSector"><option value="">every sector</option>${sectors.map((s) => `<option${s === sector ? ' selected' : ''}>${esc(s)}</option>`).join('')}</select>
        <span class="p-muted small">${num(rows.length)} of ${num(list.products.length)}</span></div>
      <div class="table-wrap"><table class="act-table ex-table"><thead><tr><th>Symbol</th><th>Name</th><th>Sector</th>
        <th>Feeds</th><th>Collected</th><th class="num">Last day</th></tr></thead><tbody>
      ${rows.slice(0, shown).map((p) => `<tr class="has-data${p.ticker === picked ? ' active' : ''}" data-product="${esc(p.ticker)}">
        <td><b>${esc(p.ticker)}</b></td><td class="ex-pname">${esc(p.name || '')}</td><td>${esc(p.sector)}</td>
        <td class="p-muted">${esc(p.feeds.join(', '))}</td><td>${p.synced ? ago(p.synced) : '<span class="p-muted">not yet</span>'}</td>
        <td class="num">${change(p.change)}</td></tr>`).join('')}</tbody></table></div>
      ${rows.length > shown ? `<button type="button" class="ex-more" data-more="1">Show ${num(Math.min(SHOW, rows.length - shown))} more</button>` : ''}`;
  }

  // -- right: the insight -----------------------------------------------------------------
  function venueInsight(v) {
    const c = v.collected;
    const next = v.next ? `${esc(STATE[v.next.state] || v.next.state)} ${when(v.next.at)}` : '';
    const top = v.sectors[0]?.[1] || 1;
    const jobs = v.delivery.length ? `<h4>Who delivers it</h4><table class="act-table ex-mini"><tbody>${v.delivery.map((e) => `
        <tr><td>${esc(JOBS[e.job] || e.job)}</td><td>${esc(e.start)} → ${esc(e.end || 'now')}</td>
        <td><b>${esc(e.sources[0])}</b>${e.sources.length > 1 ? `<span class="p-muted"> then ${esc(e.sources.slice(1).join(', '))}</span>` : ''}</td>
        <td class="p-muted">${e.unique ? 'only source' : 'shared'}</td></tr>`).join('')}</tbody></table>` : '';
    const movers = (dir) => v.movers[dir].map((m) => `<button type="button" class="ex-mover" data-product="${esc(m.ticker)}">${esc(m.ticker)} ${change(m.pct)}</button>`).join('');
    return `<h3>${esc(v.name)} ${chip(v.state)}</h3>
      <div class="p-muted small">${esc(v.hours)}${v.until ? ` · until ${when(v.until)}` : ''}${next ? ` · next: ${next}` : ''}</div>
      <div class="tiles ex-tiles">
        ${tile('Products', num(v.products), `${v.sectors.length} sector${v.sectors.length === 1 ? '' : 's'}`)}
        ${tile('History collected', pct(c.filled, v.products), `${num(c.filled)} filled · ${num(c.never)} not yet asked`)}
        ${tile('Fresh', pct(c.fresh, v.products), `synced in the last 2 days · newest ${ago(c.newest)}`)}
        ${tile('Feeds', num(v.feeds.length), esc(v.feeds.map(([f, n]) => `${f} ${num(n)}`).join(' · ')))}
      </div>
      <h4>By sector</h4>
      <div class="ex-bars">${v.sectors.map(([s, n]) => `<div class="ex-bar" data-sector="${esc(s)}" title="Show only ${esc(s)}">
        <span>${esc(s)}</span><i style="width:${Math.max(2, (n / top) * 100).toFixed(1)}%"></i><b>${num(n)}</b></div>`).join('')}</div>
      ${v.movers.up.length || v.movers.down.length ? `<h4>Last session's movers</h4>
        <div class="ex-movers">${movers('up')}</div><div class="ex-movers">${movers('down')}</div>` : ''}
      ${jobs}`;
  }

  function productInsight() {
    const head = `<div class="ex-back"><button type="button" data-back="1">← ${esc(venue()?.name || 'Exchange')}</button></div>`;
    const row = list?.products.find((p) => p.ticker === picked);
    if (!info) return `${head}<h3>${esc(picked)}</h3><div class="empty">Loading…</div>`;
    const open = `<button type="button" class="primary ex-open" data-open="${esc(picked)}">Open chart</button>`;
    if (info.error) {
      return `${head}<h3>${esc(picked)}</h3><div class="p-muted">${esc(row?.name || '')}</div>
        <div class="empty">No bars stored yet${row?.synced ? '' : ': it has not been fetched'}. Opening it in the chart asks for it first.</div>${open}`;
    }
    const sym = info.symbol, st = info.stats || {}, d = info.data || {};
    const chg = st.changePct ?? (st.prevClose ? ((st.last / st.prevClose - 1) * 100) : null);
    return `${head}<h3>${esc(sym.ticker)} <span class="p-muted small">${esc(sym.asset)}</span></h3>
      <div class="p-muted">${esc(sym.name || row?.name || '')}</div>
      <div class="ex-price"><b>${price(st.last)}</b> ${chg == null ? '' : change(chg)} <span class="p-muted small">${st.asOf ? `last day ${when(new Date(st.asOf).toISOString())}` : ''}</span></div>
      <div class="tiles ex-tiles">
        ${st.range52w ? tile('52 weeks', `${price(st.range52w.low)} – ${price(st.range52w.high)}`, `${num(st.range52w.sessions)} sessions`) : ''}
        ${st.rangeAll ? tile('All stored', `${price(st.rangeAll.low)} – ${price(st.rangeAll.high)}`, `${num(st.sessions)} sessions`) : ''}
        ${tile('Data', esc(d.base || '–'), `${esc((d.feeds || []).map((f) => (typeof feedLabel === 'function' ? feedLabel(f) : f)).join(' > '))} · ${num(d.rows)} bars`)}
        ${tile('Stored', d.first ? esc(d.first.slice(0, 10)) : '–', d.last ? `to ${esc(d.last.slice(0, 10))}` : '')}
      </div>
      ${info.futures ? `<div class="p-muted small">Continuous: holding ${esc(info.futures.contract)}, ${num(info.futures.rolls)} rolls</div>` : ''}
      ${sym.exchangeName ? `<div class="p-muted small">Listed on ${esc(sym.exchangeName)}${sym.listed ? ` since ${esc(sym.listed)}` : ''}</div>` : ''}
      ${open}`;
  }

  function sideHtml() {
    const v = venue();
    if (picked) return productInsight();
    return v ? venueInsight(v) : '<div class="empty">Pick an exchange.</div>';
  }

  function renderSide() {
    const side = box.querySelector('.ex-side');
    if (side) side.innerHTML = sideHtml();
  }

  function render() {
    if (!data) { box.innerHTML = '<div class="empty">Loading the exchanges…</div>'; return; }
    const focus = document.activeElement?.id;
    const v = venue();
    const scrolled = box.querySelector('.ex-list')?.scrollTop || 0;
    box.innerHTML = `
      <div class="ex-venues"><div class="view-head"><div><h2>Exchanges</h2>
        <div class="p-muted small">${num(data.venues.reduce((a, x) => a + x.products, 0))} products on ${num(data.venues.length)} venues</div></div></div>
        ${venuesHtml()}</div>
      <div class="ex-list"><div class="view-head"><div><h2>${esc(v?.name || '')}</h2>
        <div class="p-muted small">${esc(v ? `${v.group} · ${v.hours}` : '')}</div></div></div>${productsHtml()}</div>
      <aside class="ex-side">${sideHtml()}</aside>`;
    box.querySelector('.ex-list').scrollTop = scrolled;
    if (focus === 'exSearch') {
      const input = document.getElementById('exSearch');
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
  }

  box.addEventListener('click', (e) => {
    const t = e.target.closest('[data-venue],[data-product],[data-more],[data-back],[data-open],[data-sector]');
    if (!t) return;
    if (t.dataset.venue && t.dataset.venue !== sel) {
      sel = t.dataset.venue; list = null; picked = null; info = null; query = ''; sector = ''; shown = SHOW;
      render();
      loadList().then(render);
    } else if (t.dataset.product) {
      pick(t.dataset.product);
    } else if (t.dataset.more) {
      shown += SHOW; render();
    } else if (t.dataset.back) {
      picked = null; info = null; render();
    } else if (t.dataset.open) {
      window.openSymbol?.(t.dataset.open);
      window.showPane?.('stock');
    } else if (t.dataset.sector) {
      sector = t.dataset.sector; shown = SHOW; render();
    }
  });
  box.addEventListener('input', (e) => {
    if (e.target.id === 'exSearch') { query = e.target.value; shown = SHOW; render(); }
  });
  box.addEventListener('change', (e) => {
    if (e.target.id === 'exSector') { sector = e.target.value; shown = SHOW; render(); }
  });
  window.addEventListener('massive:view', (e) => { if (e.detail.view === 'exchanges') load(); else clearTimeout(timer); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  if (document.body.dataset.view === 'exchanges') load();  // the page opened on this view (before this script listened)
})();
