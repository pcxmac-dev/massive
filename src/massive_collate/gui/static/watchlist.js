// Watchlist (right rail, beside the chart): quick access to symbols, as TradingView's. "Collected"
// lists every symbol with data here, grouped by class (what is streaming live first) with its
// exchange, last price and change; your own lists (saved in the database, /api/watchlists) keep
// the symbols you add in your order. A click opens the symbol on the chart.
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const { symbolIcon, esc } = window.MassiveIcons;
  const COLLECTED = '__collected__';
  const GROUPS = [['live', 'Live now'], ['stock', 'Stocks'], ['index', 'Indices'], ['future', 'Futures'],
                  ['fx', 'Currencies'], ['crypto', 'Crypto']];
  let lists = [];  // [{ name, tickers: [] }]
  let current = prefs.get('watchlist', COLLECTED);
  let query = '';
  let sort = prefs.get('watchSort', { key: 'symbol', dir: 1 });
  let collapsed = new Set(prefs.get('watchCollapsed', []));
  // A group shows its first GROUP_ROWS rows until "Show all": thousands of stocks took half a second
  // to draw, again with every activity poll. A filter still searches every symbol.
  const GROUP_ROWS = 200;
  const expanded = new Set();

  const shown = () => document.body.dataset.pane === 'watchlist';

  async function load() {
    try {
      lists = (await api('/api/watchlists')).lists || [];
    } catch { lists = []; }
    if (current !== COLLECTED && !lists.some((l) => l.name === current)) current = COLLECTED;
    render();
  }

  async function save() {
    try {
      lists = (await apiWrite('PUT', '/api/watchlists', { lists })).lists;
    } catch (err) { showError(err); }
    render();
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

  function render() {
    if (!shown()) return;
    const box = $('pWatch');
    const symbols = state.symbols || [];
    const bySym = new Map(symbols.map((s) => [s.ticker, s]));
    const q = query.trim().toUpperCase();
    const match = (s) => !q || s.ticker.includes(q) || (s.name || '').toUpperCase().includes(q) || (s.exchange || '').includes(q);
    const mine = lists.find((l) => l.name === current);
    const head = (key, label, cls = '') => `<th class="${cls}" data-sort="${key}">${label}${sort.key === key ? (sort.dir > 0 ? ' ▲' : ' ▼') : ''}</th>`;
    const options = [`<option value="${COLLECTED}">Collected (${symbols.length})</option>`,
      ...lists.map((l) => `<option value="${esc(l.name)}">${esc(l.name)} (${l.tickers.length})</option>`),
      '<option value="__new__">New list…</option>'].join('');
    let body;
    if (!mine) {
      const live = new Set(window.Activity?.data?.liveFeeds?.liveTickers || []);
      body = GROUPS.map(([key, label]) => {
        const entries = symbols.filter((s) => (key === 'live' ? live.has(s.ticker) : s.asset === key && !live.has(s.ticker)) && match(s))
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
    box.innerHTML = `
      <h3 class="w-head">Watchlist
        <select id="watchList" aria-label="Watchlist">${options}</select>
        ${mine ? `<button type="button" id="watchAdd" title="Add ${esc(state.ticker || 'the open symbol')} to this list"${state.ticker && !mine.tickers.includes(state.ticker) ? '' : ' disabled'}>+</button>
          <button type="button" id="watchDelete" title="Delete this list">🗑</button>` : ''}</h3>
      <input id="watchFilter" type="search" placeholder="Filter symbols" value="${esc(query)}" autocomplete="off">
      <div class="table-wrap w-wrap"><table class="w-table">
        <thead><tr>${head('symbol', 'Symbol')}${head('last', 'Last', 'num')}${head('pct', 'Chg%', 'num')}<th></th></tr></thead>
        ${body}</table></div>`;
    $('watchList').value = mine ? mine.name : COLLECTED;
  }

  // A light refresh of prices without losing the filter's focus.
  function refresh() {
    if (!shown()) return;
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
    } else {
      current = e.target.value;
      prefs.set('watchlist', current);
    }
    render();
  });
  $('pWatch').addEventListener('click', async (e) => {
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
  window.addEventListener('massive:symbol', refresh);
  window.addEventListener('massive:activity', refresh);  // live prices with every activity poll
  window.Watchlist = { render, load };
  load();
})();
