'use strict';
/* News view (rail icon): every announcement the globe's ticker shows - exchange events from the
   calendars and the headlines the fetcher stores - newest first, with source, time, exchange and
   tickers. Filter by kind or to watched symbols; a headline opens its article, a ticker opens
   its chart, an exchange shows it on the globe. */
(() => {
  const box = document.getElementById('viewNews');
  const esc = window.MassiveIcons.esc;
  let items = [], fetchedAt = 0, filter = 'all', query = '';

  async function load(force = false) {
    if (!force && Date.now() - fetchedAt < 60000) return render();
    try {
      const res = await api('/api/announcements');
      items = res.items;
      fetchedAt = Date.now();
    } catch (err) { showError(err); }
    render();
  }

  const fmt = (iso) => new Date(iso).toLocaleString('en-US', { timeZone: 'America/New_York', weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
  function ago(iso) {
    const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
    return m < 1 ? 'now' : m < 60 ? `${m}m ago` : m < 1440 ? `${Math.round(m / 60)}h ago` : `${Math.round(m / 1440)}d ago`;
  }

  function render() {
    if (document.body.dataset.view !== 'news') return;
    const watched = new Set(state.symbols.map((s) => s.ticker));
    const q = query.trim().toLowerCase();
    const rows = items.filter((it) => (filter === 'all' || (filter === 'watched' ? it.watched?.length : it.kind === filter))
      && (!q || [it.text, it.source, it.exchange, ...(it.tickers || [])].some((v) => v && v.toLowerCase().includes(q))));
    const counts = { all: items.length, event: items.filter((i) => i.kind === 'event').length,
                     news: items.filter((i) => i.kind === 'news').length, watched: items.filter((i) => i.watched?.length).length };
    box.innerHTML = `
      <div class="view-head"><div><h2>News</h2>
        <div class="p-muted small">Everything the globe's ticker flashes: exchange events from each exchange's calendar,
          and headlines (the fetcher checks at most every 30 minutes, 1 request).</div></div>
        <div class="view-actions"><input id="newsSearch" type="search" placeholder="Filter" value="${esc(query)}"></div></div>
      <div class="filters">${[['all', 'All'], ['event', 'Exchange events'], ['news', 'Headlines'], ['watched', 'Your symbols']]
        .map(([k, l]) => `<button data-nf="${k}" class="${k === filter ? 'active' : ''}">${l} <span class="chip">${counts[k]}</span></button>`).join('')}</div>
      <div class="news-list">${rows.length ? rows.map((it) => `
        <div class="news-row ${esc(it.kind)}">
          <span class="src">${esc(it.source)}</span>
          <div class="news-main">
            ${it.url ? `<a href="${esc(it.url)}" target="_blank" rel="noopener noreferrer">${esc(it.text)}</a>` : `<span>${esc(it.text)}</span>`}
            <div class="p-muted small">${it.kind === 'news' ? `${fmt(it.time)} ET · ${ago(it.time)}` : 'now'}
              · <a href="#" data-exchange="${esc(it.exchange)}" title="Show on the globe">${esc(it.exchange)}</a>
              ${(it.tickers || []).map((t) => watched.has(t)
                ? ` <a href="#" class="tk" data-ticker="${esc(t)}" title="Open the chart">${esc(t)}</a>` : ` <span class="tk">${esc(t)}</span>`).join('')}</div>
          </div>
        </div>`).join('') : '<div class="empty">Nothing yet. Headlines arrive with the fetcher\'s next run.</div>'}</div>`;
    const input = document.getElementById('newsSearch');
    input.addEventListener('input', (e) => { query = e.target.value; render(); document.getElementById('newsSearch').focus(); });
  }

  box.addEventListener('click', (e) => {
    const f = e.target.closest('[data-nf]');
    if (f) { filter = f.dataset.nf; render(); return; }
    const t = e.target.closest('[data-ticker]');
    if (t) { e.preventDefault(); window.dispatchEvent(new CustomEvent('massive:open-symbol', { detail: t.dataset.ticker })); return; }
    const x = e.target.closest('[data-exchange]');
    if (x) { e.preventDefault(); showPane('markets', { toggle: false }); window.dispatchEvent(new CustomEvent('massive:feature-exchange', { detail: x.dataset.exchange })); }
  });
  window.addEventListener('massive:view', (e) => { if (e.detail.view === 'news') load(); });
  setInterval(() => { if (document.body.dataset.view === 'news') load(); }, 60000);
})();
