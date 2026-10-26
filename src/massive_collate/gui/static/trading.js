'use strict';
/* Trading (the rail; docs/ARCHITECTURE.md, "Trading"): a chart view of its own (interval, chart
   type, indicators and drawings, like Stock and Analysis), with the broker's account beside it in
   the side panel: the brokers, each a plugin; the account, an order ticket, positions and orders.
   On the chart: the paper position's average cost and each working order's price, for the symbol
   shown. Paper is simulated here: orders fill from the stored bars by the backtests' rules
   (trading/paper.py), matched each time this view asks. Interactive Brokers is planned; live
   trading is off, so nothing here reaches a real account. */
(() => {
  const esc = window.MassiveIcons.esc;
  const POLL_MS = 5000;
  const T = window.Trading = { data: null, timer: null };
  const box = $('pTrading');
  const shown = () => document.body.dataset.pane === 'trading' && document.body.dataset.view === 'chart';
  const money = (v) => (v == null ? '–' : v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  const signed = (v) => (v == null ? '–' : `${v >= 0 ? '+' : ''}${money(v)}`);
  const px = (v) => (v == null ? '–' : String(Number(Number(v).toPrecision(8))));
  const when = (t) => (t == null ? '' : esc(fmtTime(t)));
  let typedSymbol = false;  // the ticket follows the chart's symbol until another is typed

  box.innerHTML = `
    <h3>Trading <button id="trReset" title="Start a new paper account (the old one's record is kept)">New account…</button></h3>
    <div id="trBrokers" class="tr-brokers" role="tablist" aria-label="Broker"></div>
    <div class="p-muted small">Paper: simulated, filled from the stored bars. Live trading is off: nothing here reaches a real account.</div>
    <div id="trTiles" class="tr-tiles"></div>
    <div class="p-sub">Order <span class="p-muted">paper</span></div>
    <form id="trTicket" class="tr-ticket" autocomplete="off">
      <label>Symbol <input id="trSym" required maxlength="24"></label>
      <label>Quantity <input id="trQty" type="number" min="0" step="any" value="100" required></label>
      <label>Type <select id="trType"><option value="market">Market</option><option value="limit">Limit</option><option value="stop">Stop</option></select></label>
      <label>Time in force <select id="trTif"><option value="day">Day</option><option value="gtc">Until cancelled</option></select></label>
      <label class="tr-limit" hidden>Limit <input id="trLimit" type="number" min="0" step="any"></label>
      <label class="tr-stop" hidden>Stop <input id="trStop" type="number" min="0" step="any"></label>
      <div class="tr-sides"><button type="submit" class="tr-buy" data-side="buy">Buy</button><button type="submit" class="tr-sell" data-side="sell">Sell</button></div>
      <div id="trMsg" class="small"></div>
    </form>
    <div class="p-muted small">Fills: market at the next bar's open; a limit or stop at its price, or the open when a bar opens beyond it; from 1-second bars where held, else minutes; US stocks in regular hours. Day orders expire at the close.</div>
    <div class="p-sub">Positions</div>
    <div class="table-wrap"><table class="act-table tr-table"><thead><tr><th>Symbol</th><th class="num">Qty</th><th class="num">Avg</th><th class="num">Last</th><th class="num">Unrealized</th></tr></thead><tbody id="trPositions"></tbody></table></div>
    <div class="p-sub">Orders</div>
    <div class="table-wrap tr-orders"><table class="act-table tr-table"><thead><tr><th>Placed</th><th>Order</th><th>Status</th><th></th></tr></thead><tbody id="trOrders"></tbody></table></div>`;

  function render() {
    const d = T.data;
    if (!d) return;
    $('trBrokers').innerHTML = d.brokers.map((b) => `<button type="button" role="tab" data-broker="${esc(b.key)}"
      class="${b.key === d.broker ? 'active' : ''}" ${b.status !== 'ready' ? 'disabled' : ''} title="${esc(b.status)}">${esc(b.label)}${b.status !== 'ready' ? ' · planned' : ''}</button>`).join('');
    const a = d.account;
    const tile = (label, value, c = '') => `<div class="tile"><div class="p-muted small">${label}</div><div class="tr-big ${c}">${value}</div></div>`;
    $('trTiles').innerHTML = tile('Equity', money(a.equity)) + tile('Cash', money(a.cash))
      + tile('Unrealized', signed(a.unrealized), cls(a.unrealized)) + tile('Realized, after fees', signed(a.realized), cls(a.realized));
    $('trPositions').innerHTML = d.positions.length ? d.positions.map((p) => `<tr data-ticker="${esc(p.ticker)}" title="Realized ${signed(p.realized)}">
      <td>${esc(p.ticker)}</td><td class="num ${p.qty < 0 ? 'down' : ''}">${p.qty.toLocaleString('en-US')}</td><td class="num">${px(p.avg)}</td>
      <td class="num">${px(p.last)}</td><td class="num ${cls(p.unrealized)}">${signed(p.unrealized)}</td></tr>`).join('')
      : '<tr><td colspan="5" class="p-muted">No positions.</td></tr>';
    $('trOrders').innerHTML = d.orders.length ? d.orders.map((o) => {
      const price = o.type === 'limit' ? ` @ ${px(o.limit_px)}` : o.type === 'stop' ? ` stop ${px(o.stop_px)}` : '';
      return `<tr title="${o.tif === 'gtc' ? 'until cancelled' : 'day'}${o.done_at ? ` · ${esc(o.status)} ${esc(fmtTime(o.done_at))}` : ''}">
        <td>${when(o.placed_at)}</td>
        <td><span class="${o.side === 'buy' ? 'up' : 'down'}">${esc(o.side)}</span> ${o.qty.toLocaleString('en-US')} ${esc(o.ticker)} ${esc(o.type)}${price}</td>
        <td class="tr-${esc(o.status)}">${esc(o.status)}${o.fill_px != null ? ` ${px(o.fill_px)}` : ''}</td>
        <td>${o.status === 'working' ? `<button data-cancel="${o.id}" title="Cancel this paper order">✕</button>` : ''}</td></tr>`;
    }).join('') : '<tr><td colspan="4" class="p-muted">No orders yet.</td></tr>';
  }

  async function load() {
    if (!shown()) return;
    try {
      T.data = await api('/api/trading');
      render();
      const sig = JSON.stringify([T.data.positions.map((x) => [x.ticker, x.qty, x.avg]),
                                  T.data.orders.filter((o) => o.status === 'working').map((o) => o.id)]);
      if (sig !== T.drawn) {  // the chart repaints on its own events only: once more for the new levels
        T.drawn = sig;
        chart.overrideIndicator({ name: 'TRADING_MARKS', extendData: sig });
      }
    } catch (err) { showError(err); }
  }

  function setType() {
    const t = $('trType').value;
    box.querySelector('.tr-limit').hidden = t !== 'limit';
    box.querySelector('.tr-stop').hidden = t !== 'stop';
  }
  $('trType').addEventListener('change', setType);
  $('trSym').addEventListener('input', () => { typedSymbol = true; });

  $('trTicket').addEventListener('submit', async (e) => {
    e.preventDefault();
    const msg = $('trMsg');
    const sym = $('trSym').value.trim().toUpperCase();
    const body = { ticker: sym, source: sym === state.ticker ? state.source : '*', side: e.submitter?.dataset.side || 'buy',
                   qty: Number($('trQty').value), type: $('trType').value, limit: $('trLimit').value || null,
                   stop: $('trStop').value || null, tif: $('trTif').value };
    try {
      const o = await apiWrite('POST', '/api/trading/orders', body);
      msg.className = 'small up';
      msg.textContent = `Paper order ${o.id}: ${o.side} ${o.qty} ${o.ticker}, ${o.status}${o.fill_px != null ? ` at ${px(o.fill_px)}` : ''}`;
      load();
    } catch (err) {
      msg.className = 'small down';
      msg.textContent = err.message || String(err);
    }
  });

  box.addEventListener('click', async (e) => {
    const c = e.target.closest('button[data-cancel]');
    if (c) {
      try { await apiWrite('DELETE', `/api/trading/orders/${c.dataset.cancel}`); load(); } catch (err) { showError(err); }
      return;
    }
    const row = e.target.closest('tr[data-ticker]');
    if (row && row.dataset.ticker !== state.ticker) selectSymbol(row.dataset.ticker).catch(showError);
    if (e.target.id === 'trReset') {
      const cash = prompt('Starting cash for a new paper account (the current one is closed, its record kept):', '100000');
      if (cash == null) return;
      const fee = prompt('Fee per trade, in basis points of what is traded (0 for none):', String(T.data?.account?.feeBps ?? 0));
      if (fee == null) return;
      try { await apiWrite('POST', '/api/trading/reset', { cash: Number(cash), feeBps: Number(fee) }); load(); } catch (err) { showError(err); }
    }
  });

  // -- on the chart: the position's average cost and the working orders, for the symbol shown ----
  kc.registerIndicator({
    name: 'TRADING_MARKS',
    shortName: '',
    series: 'price',
    figures: [],
    calc: (list) => list.map(() => ({})),
    createTooltipDataSource: () => ({ name: '', calcParamsText: '', features: [], legends: [] }),
    draw: ({ ctx, bounding, yAxis }) => {
      const d = T.data;
      if (!d || !shown()) return true;
      const pos = d.positions.find((p) => p.ticker === state.ticker);
      const levels = d.orders.filter((o) => o.status === 'working' && o.ticker === state.ticker && o.type !== 'market')
        .map((o) => ({ price: o.type === 'limit' ? o.limit_px : o.stop_px, color: o.side === 'buy' ? '#26a69a' : '#ef5350',
                       text: `${o.side} ${o.qty} ${o.type}`, dash: [4, 3] }));
      if (pos) levels.push({ price: pos.avg, color: '#f5a623', text: `${pos.qty > 0 ? 'long' : 'short'} ${Math.abs(pos.qty)} @ ${px(pos.avg)}`, dash: [] });
      ctx.save();
      ctx.font = '11px sans-serif';
      for (const lv of levels) {
        const y = yAxis.convertToPixel(lv.price);
        if (!Number.isFinite(y) || y < 0 || y > bounding.height) continue;
        ctx.strokeStyle = lv.color; ctx.lineWidth = 1; ctx.setLineDash(lv.dash);
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(bounding.width, y); ctx.stroke();
        ctx.setLineDash([]);
        const w = ctx.measureText(lv.text).width + 8;
        ctx.fillStyle = lv.color; ctx.fillRect(4, y - 8, w, 16);
        ctx.fillStyle = '#fff'; ctx.fillText(lv.text, 8, y + 4);
      }
      ctx.restore();
      return true;
    },
  });

  function activate(pane, view) {
    clearInterval(T.timer);
    chart.removeIndicator({ name: 'TRADING_MARKS' });
    if (pane !== 'trading' || view !== 'chart') return;
    chart.createIndicator({ name: 'TRADING_MARKS', paneId: 'candle_pane' }, true);
    T.drawn = null;
    if (!typedSymbol && state.ticker) $('trSym').value = state.ticker;
    setType();
    load();
    T.timer = setInterval(load, POLL_MS);
  }
  window.addEventListener('massive:view', (e) => activate(e.detail.pane, e.detail.view));
  window.addEventListener('massive:symbol', () => { if (!typedSymbol && state.ticker) $('trSym').value = state.ticker; });
  T.load = load;
  activate(document.body.dataset.pane, document.body.dataset.view);  // opened on this view: its event came first
})();
