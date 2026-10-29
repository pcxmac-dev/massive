// Watchlist (right rail, beside the chart): quick access to symbols, as TradingView's. "Collected"
// lists every symbol with data here (another profile than the first: only its own, from its lists and
// its register), grouped by class (what is streaming live first) with its
// exchange, last price and change; your own lists (saved in the database, /api/watchlists) keep
// the symbols you add in your order; "Visited" is the profile's register, every symbol it opened,
// the latest first (/api/register). A click opens the symbol on the chart.
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const { symbolIcon, esc } = window.MassiveIcons;
  const COLLECTED = '__collected__';
  const VISITED = '__visited__';  // the profile's register
  const GROUPS = [['live', 'Live now'], ['stock', 'Stocks'], ['index', 'Indices'], ['future', 'Futures'],
                  ['fx', 'Currencies'], ['crypto', 'Crypto']];
  let lists = [];  // [{ name, tickers: [] }]
  let visits = [];  // the register: [{ ticker, first_at, last_at, count }], the latest first
  let current = prefs.get('watchlist', COLLECTED);
  let query = '';
  let sort = prefs.get('watchSort', { key: 'symbol', dir: 1 });
  let collapsed = new Set(prefs.get('watchCollapsed', []));
  // The list as a table or as a heat map (the user, 2026-10-28: "heat maps for all the exchanges ... as
  // well as watchlist(s) so when i open the symbol picker or watchlist picker, its easier to figure").
  let view = prefs.get('watchView', 'list');
  const MAP_EQUAL = 400;  // a list up to this many: equal tiles; longer (everything collected): by dollar volume
  const MAP_REDRAW_MS = 15000;  // the map drawn again with the activity polls at most this often
  let mapDrawn = 0;
  // A group shows its first GROUP_ROWS rows until "Show all": thousands of stocks took half a second
  // to draw, again with every activity poll. A filter still searches every symbol.
  const GROUP_ROWS = 200;
  const expanded = new Set();

  const shown = () => document.body.dataset.pane === 'watchlist';

  async function load() {
    try {
      lists = (await api('/api/watchlists')).lists || [];
    } catch { lists = []; }
    await loadVisits();
    if (current !== COLLECTED && current !== VISITED && !lists.some((l) => l.name === current)) current = COLLECTED;
    render();
  }

  async function loadVisits() {
    try { visits = (await api('/api/register')).visits || []; } catch { /* kept as it was */ }
  }

  // One save at a time, each sending the lists as they are when it goes: two at once could reach the
  // server in either order, the older undoing the newer. The server's copy is taken only from the last
  // save waiting (an earlier answer lacks the changes still to go).
  let saving = Promise.resolve();
  let waiting = 0;
  function save() {
    waiting += 1;
    saving = saving.then(async () => {
      try {
        const res = await apiWrite('PUT', '/api/watchlists', { lists });
        if (waiting === 1) lists = res.lists;
      } catch (err) { showError(err); } finally { waiting -= 1; }
      render();
    });
    return saving;
  }

  // Live price where the symbol is streaming; otherwise the last session's close and change.
  function quote(sym) {
    const close = sym.market?.close ?? null, pct = sym.market?.changePct ?? sym.changePct ?? null;
    let live = null;
    for (const s of Object.values(window.Activity?.data?.liveFeeds?.streams || {})) {
      const p = s.pairs?.[sym.ticker];
      if (s.connected && p?.price != null && (live == null || p.last > live.last)) live = p;
    }
    const isLiveNow = (window.Activity?.data?.liveFeeds?.liveTickers || []).includes(sym.ticker);
    if (isLiveNow && live) {
      const prev = close != null && pct != null ? close / (1 + pct / 100) : null;
      return { last: live.price, pct: prev ? (live.price / prev - 1) * 100 : null, live: true };
    }
    return { last: close, pct, live: false };
  }

  const fmtPrice = (v) => (v == null ? '–' : Number(v).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: Math.abs(v) < 1 ? 6 : Math.abs(v) < 100 ? 4 : 2 }));
  const fmtPct = (v) => (v == null ? '' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`);

  function compare(a, b) {
    const k = sort.key;
    const va = k === 'symbol' ? a.sym.ticker : k === 'last' ? a.q.last : a.q.pct;
    const vb = k === 'symbol' ? b.sym.ticker : k === 'last' ? b.q.last : b.q.pct;
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    return (typeof va === 'string' ? va.localeCompare(vb) : va - vb) * sort.dir;
  }

  function row(entry, removable) {
    const { sym, q } = entry;
    const on = sym.ticker === state.ticker;
    return `<tr class="${on ? 'on' : ''}" data-ticker="${esc(sym.ticker)}" title="${esc(sym.name || sym.ticker)}${sym.exchangeName ? ` · ${esc(sym.exchangeName)}` : ''}">
      <td class="w-sym">${symbolIcon(sym, 18)}<span><b>${esc(sym.ticker)}</b>${q.live ? '<span class="w-live" title="Streaming live"></span>' : ''}
        <span class="w-ex">${esc(sym.exchange || sym.asset || '')}</span></span></td>
      <td class="num">${fmtPrice(q.last)}</td>
      <td class="num ${q.pct == null ? '' : q.pct >= 0 ? 'up' : 'down'}">${fmtPct(q.pct)}</td>
      ${removable ? `<td class="w-x"><button type="button" data-remove="${esc(sym.ticker)}" title="Remove from this list" aria-label="Remove ${esc(sym.ticker)}">×</button></td>` : '<td></td>'}
    </tr>`;
  }

  // The map of a list: stocks by sector and industry, the other classes in groups of their own (futures
  // by family and type), each tile coloured by its move (live where it streams, as the table's).
  function mapGroup(sym) {
    const M = window.MarketMap;
    if (sym.asset === 'stock') return [sym.sector || 'Not classified yet', sym.industry || ''];
    if (sym.asset === 'future') return [`Futures · ${M.futuresFamily(sym.futuresType)}`, M.futuresTypeName(sym.futuresType)];
    return [{ index: 'Indices', fx: 'Currencies', crypto: 'Crypto' }[sym.asset] || 'Other', ''];
  }

  function renderMap(box, head, syms) {
    const M = window.MarketMap;
    const items = syms.map((sym) => {
      const q = quote(sym), [group, kind] = mapGroup(sym);
      return { ticker: sym.ticker, name: sym.name, icon: sym.icon, asset: sym.asset, sector: group, industry: kind,
        marketCap: sym.marketCap, dollarVolume: sym.market?.dollarVolume ?? null, changePct: q.pct, close: q.last };
    });
    const volumes = items.map((s) => s.dollarVolume).filter((v) => v > 0).sort((a, b) => a - b);
    const median = volumes.length ? volumes[Math.floor(volumes.length / 2)] : 1;
    const size = items.length <= MAP_EQUAL ? () => 1 : (s) => (s.asset === 'stock' ? s.dollarVolume || 0 : median);
    box.innerHTML = `${head}<div id="watchMap" class="market-map watch-map" role="listbox" aria-label="Watchlist map"></div>
      <div class="map-foot"><span class="map-legend">${[-3, -1, 0, 1, 3].map((p) => `<span style="background:${M.changeColor(p)}">${p > 0 ? '+' : ''}${p}%</span>`).join('')}</span>
        <span class="p-muted small">${items.length.toLocaleString()} · ${items.length <= MAP_EQUAL ? 'equal tiles' : 'stocks by dollar volume'} · by sector</span></div>`;
    $('watchList').value = lists.some((l) => l.name === current) ? current : current === VISITED ? VISITED : COLLECTED;
    const el = $('watchMap');
    if (el.clientWidth) M.render(el, items, { current: state.ticker, levels: [(s) => s.sector, (s) => s.industry], size });
    mapDrawn = Date.now();
  }

  let shownHead = null;  // the panel's head (list, sort, filter) as last drawn
  function render() {
    if (!shown()) return;
    const box = $('pWatch');
    const symbols = state.symbols || [];
    const bySym = new Map(symbols.map((s) => [s.ticker, s]));
    // Collected: every symbol with data here for the first profile (it kept what was here before
    // profiles); another's are its own, in its lists or opened by it, so a new one starts with none.
    const own = PROFILE === 1 ? null : new Set([...lists.flatMap((l) => l.tickers), ...visits.map((v) => v.ticker)]);
    const collected = own ? symbols.filter((s) => own.has(s.ticker)) : symbols;
    const q = query.trim().toUpperCase();
    const match = (s) => !q || s.ticker.includes(q) || (s.name || '').toUpperCase().includes(q) || (s.exchange || '').includes(q);
    const mine = lists.find((l) => l.name === current);
    const th = (key, label, cls = '') => `<th class="${cls}" data-sort="${key}">${label}${sort.key === key ? (sort.dir > 0 ? ' ▲' : ' ▼') : ''}</th>`;
    const options = [`<option value="${COLLECTED}">Collected (${collected.length})</option>`,
      `<option value="${VISITED}">Visited (${visits.length})</option>`,
      ...lists.map((l) => `<option value="${esc(l.name)}">${esc(l.name)} (${l.tickers.length})</option>`),
      '<option value="__new__">New list…</option>'].join('');
    const toggle = `<button type="button" id="watchView" title="${view === 'map' ? 'Show the list as a table' : 'Show the list as a heat map, by sector'}"
      aria-pressed="${view === 'map'}">${view === 'map' ? 'List' : 'Map'}</button>`;
    if (view === 'map') {
      const syms = current === VISITED ? visits.map((v) => bySym.get(v.ticker)).filter(Boolean)
        : mine ? mine.tickers.map((t) => bySym.get(t)).filter(Boolean) : collected;
      const head = `<h3 class="w-head">Watchlist <select id="watchList" aria-label="Watchlist">${options}</select>${toggle}
          ${mine ? `<button type="button" id="watchAdd" title="Add ${esc(state.ticker || 'the open symbol')} to this list"${state.ticker && !mine.tickers.includes(state.ticker) ? '' : ' disabled'}>+</button>` : ''}</h3>
        <input id="watchFilter" type="search" placeholder="Filter symbols" value="${esc(query)}" autocomplete="off">`;
      shownHead = null;  // the table is drawn whole when it comes back
      renderMap(box, head, syms.filter(match));
      return;
    }
    let body;
    if (current === VISITED) {
      const entries = visits.map((v) => bySym.get(v.ticker)).filter((s) => s && match(s)).map((sym) => ({ sym, q: quote(sym) }));
      if (sort.key !== 'order') entries.sort(compare);
      body = entries.length ? `<tbody>${entries.map((e) => row(e, false)).join('')}</tbody>`
        : `<tbody><tr><td colspan="4" class="p-muted w-empty">${visits.length ? 'Nothing matches.' : 'Nothing visited yet: every symbol you open is noted here.'}</td></tr></tbody>`;
    } else if (!mine) {
      const live = new Set(window.Activity?.data?.liveFeeds?.liveTickers || []);
      body = !collected.length ? '<tbody><tr><td colspan="4" class="p-muted w-empty">Nothing collected yet: the symbols you open or add to a list show here.</td></tr></tbody>'
        : GROUPS.map(([key, label]) => {
        const entries = collected.filter((s) => (key === 'live' ? live.has(s.ticker) : s.asset === key && !live.has(s.ticker)) && match(s))
          .map((sym) => ({ sym, q: quote(sym) })).sort(compare);
        if (!entries.length) return '';
        const closed = collapsed.has(key) && !q;
        const shownRows = expanded.has(key) ? entries : entries.slice(0, GROUP_ROWS);
        const more = entries.length - shownRows.length;
        return `<tbody><tr class="w-group" data-group="${key}"><td colspan="4">${closed ? '▸' : '▾'} ${label} <span class="p-muted">${entries.length}</span></td></tr></tbody>
          <tbody${closed ? ' hidden' : ''}>${shownRows.map((e) => row(e, false)).join('')}${more
            ? `<tr class="w-more" data-more="${key}"><td colspan="4">Show all ${entries.length.toLocaleString()} (${more.toLocaleString()} more)</td></tr>` : ''}</tbody>`;
      }).join('');
    } else {
      const entries = mine.tickers.map((t) => bySym.get(t)).filter((s) => s && match(s)).map((sym) => ({ sym, q: quote(sym) }));
      if (sort.key !== 'order') entries.sort(compare);
      body = entries.length ? `<tbody>${entries.map((e) => row(e, true)).join('')}</tbody>`
        : `<tbody><tr><td colspan="4" class="p-muted w-empty">${mine.tickers.length ? 'Nothing matches.' : 'Empty: open a symbol and press + to add it.'}</td></tr></tbody>`;
    }
    const head = `<h3 class="w-head">Watchlist
        <select id="watchList" aria-label="Watchlist">${options}</select>${toggle}
        ${mine ? `<button type="button" id="watchAdd" title="Add ${esc(state.ticker || 'the open symbol')} to this list"${state.ticker && !mine.tickers.includes(state.ticker) ? '' : ' disabled'}>+</button>
          <button type="button" id="watchDelete" title="Delete this list">🗑</button>` : ''}</h3>
      <input id="watchFilter" type="search" placeholder="Filter symbols" value="${esc(query)}" autocomplete="off">
      <div class="table-wrap w-wrap"><table class="w-table">
        <thead><tr>${th('symbol', 'Symbol')}${th('last', 'Last', 'num')}${th('pct', 'Chg%', 'num')}<th></th></tr></thead>`;
    const table = box.querySelector('.w-table');
    if (table && shownHead === head) {  // the same list, sort and filter: new rows only
      table.querySelectorAll('tbody').forEach((t) => t.remove());
      table.insertAdjacentHTML('beforeend', body);
      return;
    }
    shownHead = head;
    box.innerHTML = `${head}${body}</table></div>`;
    $('watchList').value = mine ? mine.name : current === VISITED ? VISITED : COLLECTED;
  }

  // A light refresh of prices without losing the filter's focus.
  function refresh() {
    if (!shown()) return;
    if (view === 'map' && Date.now() - mapDrawn < MAP_REDRAW_MS && document.activeElement?.id !== 'watchFilter') return;
    const focused = document.activeElement?.id === 'watchFilter';
    const at = focused ? $('watchFilter').selectionStart : null;
    const scroll = document.querySelector('#pWatch .w-wrap')?.scrollTop;
    render();
    if (scroll != null) { const w = document.querySelector('#pWatch .w-wrap'); if (w) w.scrollTop = scroll; }
    if (focused) { const f = $('watchFilter'); f.focus(); f.setSelectionRange(at, at); }
  }

  $('pWatch').addEventListener('input', (e) => {
    if (e.target.id !== 'watchFilter') return;
    query = e.target.value;
    refresh();
  });
  $('pWatch').addEventListener('change', async (e) => {
    if (e.target.id !== 'watchList') return;
    if (e.target.value === '__new__') {
      const name = (window.prompt('Name of the new list') || '').trim().slice(0, 40);
      if (name && !lists.some((l) => l.name === name)) {
        lists.push({ name, tickers: state.ticker ? [state.ticker] : [] });
        current = name;
        prefs.set('watchlist', current);
        await save();
        return;
      }
      shownHead = null;  // cancelled, or a name in use: drawn whole, the picker back on the list shown
    } else {
      current = e.target.value;
      prefs.set('watchlist', current);
    }
    render();
  });
  $('pWatch').addEventListener('click', async (e) => {
    if (e.target.closest('#watchView')) {
      view = view === 'map' ? 'list' : 'map';
      prefs.set('watchView', view);
      return render();
    }
    const tile = e.target.closest('.map-tile[data-ticker]');
    if (tile) { window.dispatchEvent(new CustomEvent('massive:open-symbol', { detail: tile.dataset.ticker })); return; }
    const sortBy = e.target.closest('th[data-sort]');
    if (sortBy) {
      sort = { key: sortBy.dataset.sort, dir: sort.key === sortBy.dataset.sort ? -sort.dir : 1 };
      prefs.set('watchSort', sort);
      return render();
    }
    const group = e.target.closest('tr.w-group');
    if (group) {
      const k = group.dataset.group;
      if (collapsed.has(k)) collapsed.delete(k); else collapsed.add(k);
      prefs.set('watchCollapsed', [...collapsed]);
      return render();
    }
    const mine = lists.find((l) => l.name === current);
    if (e.target.closest('#watchAdd') && mine && state.ticker && !mine.tickers.includes(state.ticker)) {
      mine.tickers.push(state.ticker);
      return save();
    }
    if (e.target.closest('#watchDelete') && mine && window.confirm(`Delete the list "${mine.name}"?`)) {
      lists = lists.filter((l) => l !== mine);
      current = COLLECTED;
      prefs.set('watchlist', current);
      return save();
    }
    const remove = e.target.closest('button[data-remove]');
    if (remove && mine) {
      mine.tickers = mine.tickers.filter((t) => t !== remove.dataset.remove);
      return save();
    }
    const moreRow = e.target.closest('tr[data-more]');
    if (moreRow) { expanded.add(moreRow.dataset.more); render(); return; }
    const item = e.target.closest('tr[data-ticker]');
    if (item) window.dispatchEvent(new CustomEvent('massive:open-symbol', { detail: item.dataset.ticker }));
  });

  window.addEventListener('massive:view', (e) => { if (e.detail?.pane === 'watchlist') render(); });
  window.addEventListener('massive:symbol', () => {
    refresh();
    // the opened symbol goes in the register (the chart's priority request notes it): read it again
    setTimeout(() => loadVisits().then(() => { if (current === VISITED) { shownHead = ''; refresh(); } }), 1500);
  });
  window.addEventListener('massive:activity', refresh);  // live prices with every activity poll
  window.Watchlist = { render, load, lists: () => lists };  // the lists: the symbol picker maps them too
  load();
})();
