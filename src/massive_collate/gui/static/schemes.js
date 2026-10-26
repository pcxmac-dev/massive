'use strict';
/* Schemes on the Analysis view (docs/ARCHITECTURE.md, "Schemes and replay"): trading algorithms
   run by the server (calc/schemes.py, /api/scheme) over the highlight, or what is in view, at the
   chart's level. Drawn on the chart: each entry and exit, the scheme's own lines (the reversal
   level, the prior range) and swing points; below it the equity curve (and buy and hold over the
   same bars), and for CUSUM its two sums against the threshold. The side panel has the settings,
   the statistics and the trades. Backtests only: nothing here places an order.

   During a replay (replay.js) every decision is shown only once the bar that made it has closed:
   `Schemes.shownTo` is the replay's last closed bar, `Schemes.formingAt` the bar now forming. */
(() => {
  const esc = window.MassiveIcons.esc;
  const S = window.Schemes = { list: [], result: null, running: false, shownTo: null, formingAt: null, error: null };
  const saved = prefs.get('scheme', {});
  const cfg = S.cfg = { key: saved.key || 'atr_swing', params: saved.params || {}, fee: saved.fee ?? 1,
                        slippage: saved.slippage ?? 1, longOnly: !!saved.longOnly };
  const save = () => prefs.set('scheme', cfg);
  const UP = '#26a69a', DOWN = '#ef5350', LINE_COLOR = '#f5a623', EQUITY = '#2962ff', HOLD = '#9598a1';
  // Each scheme line's colour and dash: the Alligator in Williams' own colours, the FLD violet, the
  // levels a breakout must pass dashed (green above, red below), reversal levels orange.
  const LINE_STYLE = {
    jaw: ['#2962ff', []], teeth: ['#ef5350', []], lips: ['#26a69a', []], fld: ['#ab47bc', []],
    range_high: ['rgba(38,166,154,0.55)', [4, 3]], range_low: ['rgba(239,83,80,0.55)', [4, 3]],
    fractal_high: ['rgba(38,166,154,0.7)', [4, 3]], fractal_low: ['rgba(239,83,80,0.7)', [4, 3]],
    pivot_high: ['rgba(38,166,154,0.7)', [4, 3]], pivot_low: ['rgba(239,83,80,0.7)', [4, 3]],
  };
  const ZIGZAG = new Set(['high', 'low']);  // swing points joined; the others are marks

  const scheme = () => S.list.find((s) => s.key === cfg.key) || S.list[0];
  const seen = (t) => S.shownTo == null || t <= S.shownTo;  // a closed bar the replay has reached
  const reached = (t) => S.shownTo == null || t <= (S.formingAt ?? S.shownTo);  // a bar it has reached at all

  // -- running -----------------------------------------------------------------------------
  function stretch() {
    if (window.Replay?.active()) return window.Replay.range();  // what the replay has loaded, closed
    const span = highlightSpan();
    if (span) return { ...span, over: 'the highlight' };
    const data = chart.getDataList();
    const r = chart.getVisibleRange();
    if (!data.length) return null;
    const a = data[Math.max(0, r.from)], b = data[Math.min(data.length - 1, r.to - 1)];
    return a && b ? { from: a.timestamp, to: b.timestamp, over: 'the bars in view' } : null;
  }

  async function run(range = stretch(), { quiet = false } = {}) {
    const s = scheme();
    if (!s || !range || !state.ticker || !state.period) return;
    if (PRICE_BASED.has(state.chartType)) { S.error = 'Schemes run on time-based charts (candles, bars, lines ...).'; render(); return; }
    S.running = true;
    if (!quiet) render();
    const my = S.token = (S.token || 0) + 1;
    try {
      const res = await api('/api/scheme', {
        source: state.source, ticker: state.ticker, period: state.period.type, span: state.period.span,
        session: state.session, from: range.from, to: range.to, scheme: s.key, params: JSON.stringify(cfg.params),
        fee: cfg.fee, slippage: cfg.slippage, ...(cfg.longOnly ? { longOnly: 1 } : {}),
        ...(state.unfiltered ? { unfiltered: 1 } : {}),
      });
      if (my !== S.token) return;
      res.over = range.over;
      res.ticker = state.ticker;
      setResult(res);
      S.error = null;
    } catch (err) {
      if (my === S.token) S.error = err.message || String(err);
    } finally {
      if (my === S.token) { S.running = false; render(); }
    }
  }

  function setResult(res) {
    S.result = res;
    if (res) {
      S.eq = new Map(res.equity.map(([t, e, p]) => [t, { e, p }]));
      S.lines = Object.fromEntries(Object.entries(res.lines).map(([k, v]) => [k, new Map(v)]));
      const first = res.equity[0]?.[0];
      S.holdFrom = first != null ? chart.getDataList()[barIndexAt(chart.getDataList(), first)]?.open : null;
    }
    drawPanes();
    window.dispatchEvent(new CustomEvent('massive:scheme', { detail: res }));
  }

  function clear() {
    S.token = (S.token || 0) + 1;
    S.result = null; S.error = null; S.running = false;
    drawPanes();
    render();
  }

  // The result belongs to the chart as it is: the same symbol and level.
  const current = () => S.result && S.result.ticker === state.ticker && S.result.level.type === state.period?.type
    && S.result.level.span === state.period?.span;

  // -- the chart: marks on the price pane, equity and CUSUM panes ------------------------------
  function drawPanes() {
    chart.removeIndicator({ name: 'SCHEME_MARKS' });
    chart.removeIndicator({ name: 'SCHEME_EQUITY' });
    chart.removeIndicator({ name: 'SCHEME_CUSUM' });
    if (!S.result) return;
    chart.createIndicator({ name: 'SCHEME_MARKS', paneId: 'candle_pane' }, true);
    chart.createIndicator('SCHEME_EQUITY', false, { id: 'scheme_equity_pane', height: 90, minHeight: 50 });
    if (S.result.scheme === 'cusum') chart.createIndicator('SCHEME_CUSUM', false, { id: 'scheme_cusum_pane', height: 80, minHeight: 40 });
  }

  const noTooltip = () => ({ name: '', calcParamsText: '', features: [], legends: [] });

  kc.registerIndicator({
    name: 'SCHEME_MARKS',
    shortName: '',
    series: 'price',
    figures: [],
    calc: (list) => list.map(() => ({})),
    createTooltipDataSource: noTooltip,
    draw: ({ ctx, chart: c, bounding, xAxis, yAxis }) => {
      if (!current()) return true;
      const data = c.getDataList();
      const { from, to } = c.getVisibleRange();
      if (!data.length || to <= from) return true;
      const r = S.result;
      const x = (i) => xAxis.convertToPixel(i), y = (v) => yAxis.convertToPixel(v);
      const lo = Math.max(0, from - 1), hi = Math.min(data.length, to + 1);
      ctx.save();
      ctx.setLineDash([]);
      // the scheme's own lines: the reversal level, the prior range's high and low
      for (const [name, values] of Object.entries(S.lines || {})) {
        if (name.startsWith('s_')) continue;  // CUSUM: its own pane
        const [color, dash] = LINE_STYLE[name] || [LINE_COLOR, []];
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.2;
        ctx.setLineDash(dash);
        ctx.beginPath();
        let pen = false;
        for (let i = lo; i < hi; i++) {
          const t = data[i].timestamp, v = values.get(t);
          if (v == null || !seen(t)) { pen = false; continue; }
          if (pen) ctx.lineTo(x(i), y(v)); else ctx.moveTo(x(i), y(v));
          pen = true;
        }
        ctx.stroke();
      }
      ctx.setLineDash([]);
      // points, each known only at the bar that confirmed it: swings joined, the rest marked
      const pts = r.points.filter((p) => seen(p.known)).map((p) => ({ ...p, i: barIndexAt(data, p.time) }))
        .filter((p) => p.i < data.length && data[p.i].timestamp === p.time);
      const swings = pts.filter((p) => ZIGZAG.has(p.kind));
      if (swings.length) {
        ctx.strokeStyle = 'rgba(245,166,35,0.7)'; ctx.lineWidth = 1;
        ctx.beginPath();
        swings.forEach((p, k) => ctx[k ? 'lineTo' : 'moveTo'](x(p.i), y(p.price)));
        ctx.stroke();
      }
      for (const p of pts) {
        if (ZIGZAG.has(p.kind) || p.i < lo || p.i >= hi) continue;
        const high = p.kind.includes('_high');
        ctx.globalAlpha = p.kind.endsWith('_out') ? 0.3 : 0.9;  // left out by the Alligator
        ctx.fillStyle = p.kind === 'trough' ? '#ab47bc' : high ? UP : DOWN;
        const cx = x(p.i), cy = y(p.price) + (high ? -8 : 8);
        ctx.beginPath();
        if (p.kind === 'trough') ctx.arc(cx, cy, 3, 0, Math.PI * 2);
        else { ctx.moveTo(cx, cy + (high ? -4 : 4)); ctx.lineTo(cx - 4, cy + (high ? 3 : -3)); ctx.lineTo(cx + 4, cy + (high ? 3 : -3)); }
        ctx.fill();
        ctx.globalAlpha = 1;
      }
      // Hurst: when the next trough is due, from the last trough known
      const last = pts.filter((p) => p.kind === 'trough' && p.due != null).at(-1);
      if (last) {
        const di = barIndexAt(data, last.due);
        const dx = di < data.length ? x(di) : x(data.length - 1) + (last.due - data.at(-1).timestamp)
          / Math.max(1, data.at(-1).timestamp - data.at(-2)?.timestamp || 1) * c.getBarSpace().bar;
        ctx.strokeStyle = 'rgba(171,71,188,0.8)'; ctx.setLineDash([3, 4]); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(dx, 0); ctx.lineTo(dx, bounding.height); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = 'rgba(171,71,188,0.9)'; ctx.font = '11px sans-serif';
        ctx.fillText('next trough due', dx + 4, 12);
      }
      // entries and exits
      const bar = Math.max(4, Math.min(10, c.getBarSpace().bar));
      const tri = (i, price, up, color) => {
        const cx = x(i), cy = y(price) + (up ? 10 : -10);
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.moveTo(cx, cy + (up ? -bar / 2 - 2 : bar / 2 + 2));
        ctx.lineTo(cx - bar / 2 - 1, cy + (up ? bar / 2 : -bar / 2));
        ctx.lineTo(cx + bar / 2 + 1, cy + (up ? bar / 2 : -bar / 2));
        ctx.closePath(); ctx.fill();
      };
      for (const t of visibleTrades()) {
        const ei = barIndexAt(data, t.entryTime);
        if (ei >= lo && ei < hi) tri(ei, t.side === 'long' ? data[ei].low : data[ei].high, t.side === 'long', t.side === 'long' ? UP : DOWN);
        if (t.shownExit) {
          const xi = barIndexAt(data, t.exitTime);
          if (xi >= lo && xi < hi) {
            const cx = x(xi), cy = y(t.exit);
            ctx.strokeStyle = t.ret >= 0 ? UP : DOWN; ctx.lineWidth = 2;
            ctx.beginPath(); ctx.moveTo(cx - 4, cy - 4); ctx.lineTo(cx + 4, cy + 4);
            ctx.moveTo(cx + 4, cy - 4); ctx.lineTo(cx - 4, cy + 4); ctx.stroke();
          }
          if (ei < hi && xi >= lo) {  // entry to exit, faint
            ctx.strokeStyle = t.ret >= 0 ? 'rgba(38,166,154,0.45)' : 'rgba(239,83,80,0.45)'; ctx.lineWidth = 1;
            ctx.setLineDash([2, 3]);
            ctx.beginPath(); ctx.moveTo(x(ei), y(t.entry)); ctx.lineTo(x(xi), y(t.exit)); ctx.stroke();
            ctx.setLineDash([]);
          }
        } else {  // open: its entry price, stop and target to the newest bar
          const sig = r.signals.find((s) => s.time === t.signalTime);
          const right = x(data.length - 1) + 20;
          const level = (v, color, dash) => {
            if (v == null) return;
            ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.setLineDash(dash);
            ctx.beginPath(); ctx.moveTo(x(Math.max(ei, lo)), y(v)); ctx.lineTo(right, y(v)); ctx.stroke();
          };
          level(t.entry, t.side === 'long' ? UP : DOWN, [6, 3]);
          level(sig?.stop, DOWN, [2, 2]);
          level(sig?.target, UP, [2, 2]);
          ctx.setLineDash([]);
        }
      }
      ctx.restore();
      return true;
    },
  });

  // Trades as far as the replay has reached (all of them outside a replay): an entry once its bar
  // opened, an exit once its bar closed (or opened, for a fill at the open).
  function visibleTrades() {
    if (!S.result) return [];
    const out = [];
    for (const t of S.result.trades) {
      if (!reached(t.entryTime)) continue;
      const atOpen = t.why === 'reversed' || t.why === 'exit signal';
      const shownExit = t.exitTime != null && (seen(t.exitTime) || (atOpen && reached(t.exitTime)));
      out.push({ ...t, shownExit });
    }
    return out;
  }
  S.visibleTrades = visibleTrades;

  kc.registerIndicator({
    name: 'SCHEME_EQUITY',
    shortName: 'Equity %',
    precision: 2,
    figures: [
      { key: 'eq', title: 'Scheme: ', type: 'line' },
      { key: 'hold', title: 'Buy & hold: ', type: 'line' },
    ],
    styles: { lines: [{ color: EQUITY, size: 1.5 }, { color: HOLD, size: 1, style: 'dashed', dashedValue: [3, 3] }] },
    calc: (list) => list.map((d) => {
      if (!current() || !seen(d.timestamp)) return {};
      const v = S.eq?.get(d.timestamp);
      if (!v) return {};
      return { eq: (v.e - 1) * 100, hold: S.holdFrom ? (d.close / S.holdFrom - 1) * 100 : null };
    }),
  });

  kc.registerIndicator({
    name: 'SCHEME_CUSUM',
    shortName: 'CUSUM',
    precision: 2,
    figures: [
      { key: 'up', title: 'S+: ', type: 'line' },
      { key: 'down', title: 'S−: ', type: 'line' },
      { key: 'h', title: 'h: ', type: 'line' },
    ],
    styles: { lines: [{ color: UP, size: 1.2 }, { color: DOWN, size: 1.2 }, { color: HOLD, size: 1, style: 'dashed', dashedValue: [4, 3] }] },
    calc: (list) => list.map((d) => {
      if (!current() || !seen(d.timestamp)) return {};
      const up = S.lines?.s_up?.get(d.timestamp);
      if (up == null) return {};
      return { up, down: S.lines.s_down.get(d.timestamp), h: S.result.params.h };
    }),
  });

  // -- the side panel ---------------------------------------------------------------------------
  const fmtPct = (v) => (v == null ? '–' : pct(v * 100));
  const when = (t) => (t == null ? '–' : esc(fmtTime(t)));

  function statsRows(st, trades) {
    if (S.shownTo != null) {  // a replay: what it has reached
      const closed = trades.filter((t) => t.shownExit);
      const won = closed.filter((t) => t.ret > 0);
      const lastEq = [...S.result.equity].reverse().find(([t]) => seen(t));
      return [
        ['Net', `<span class="${cls(lastEq ? lastEq[1] - 1 : null)}">${fmtPct(lastEq ? lastEq[1] - 1 : null)}</span>`],
        ['Trades', `${trades.length}${closed.length < trades.length ? ' (1 open)' : ''}`],
        ['Won', closed.length ? fmtPct(won.length / closed.length).replace('+', '') : '–'],
      ];
    }
    return [
      ['Net', `<span class="${cls(st.net)}">${fmtPct(st.net)}</span>`],
      ['Buy & hold', `<span class="${cls(st.buy_hold)}">${fmtPct(st.buy_hold)}</span>`],
      ['Deepest drawdown', fmtPct(st.max_drawdown)],
      ['Trades', `${st.trades} <small>(${st.long} long, ${st.short} short)</small>`],
      ['Won', st.won == null ? '–' : fmtPct(st.won).replace('+', '')],
      ['Average win / loss', `${fmtPct(st.avg_win)} / ${fmtPct(st.avg_loss)}`],
      ['Profit factor', st.profit_factor == null ? '–' : st.profit_factor.toFixed(2)],
      ['In a position', st.exposure == null ? '–' : `${(st.exposure * 100).toFixed(0)}% of bars`],
    ];
  }

  function paramInput(p) {
    const v = cfg.params[p.key] ?? p.default;
    if (p.choices.length) {
      return `<label title="${esc(p.about)}">${esc(p.label)} <select data-param="${esc(p.key)}">${p.choices
        .map((c) => `<option${c === v ? ' selected' : ''}>${esc(c)}</option>`).join('')}</select></label>`;
    }
    return `<label title="${esc(p.about)}">${esc(p.label)} <input type="number" data-param="${esc(p.key)}" value="${esc(String(v))}"
      min="${p.lo}" max="${p.hi}" step="${p.step}"></label>`;
  }

  function render() {
    const box = $('pSchemes');
    if (!box) return;
    const s = scheme();
    if (!s) { box.innerHTML = '<h3>Schemes</h3><div class="p-muted">Loading…</div>'; return; }
    const r = current() ? S.result : null;
    const trades = r ? visibleTrades() : [];
    const range = stretch();
    box.innerHTML = `
      <h3>Schemes ${S.result ? '<button id="schClear" title="Remove the scheme from the chart">Clear</button>' : ''}
        <button id="schRun" class="primary" title="Run over ${esc(range?.over || 'the chart')}"${S.running ? ' disabled' : ''}>${S.running ? 'Running…' : 'Run'}</button></h3>
      <select id="schKey" aria-label="Scheme">${S.list.map((x) => `<option value="${esc(x.key)}"${x.key === s.key ? ' selected' : ''}>${esc(x.label)}</option>`).join('')}</select>
      <div class="p-muted small sch-about">${esc(s.about)}</div>
      <div class="sch-params">${s.params.map(paramInput).join('')}
        <label title="Fee charged at every entry and exit, basis points of what is traded">Fee bps <input type="number" id="schFee" value="${cfg.fee}" min="0" max="1000" step="0.5"></label>
        <label title="Slippage charged at every entry and exit, basis points">Slippage bps <input type="number" id="schSlip" value="${cfg.slippage}" min="0" max="1000" step="0.5"></label>
        <label title="Short decisions only close a long"><input type="checkbox" id="schLong"${cfg.longOnly ? ' checked' : ''}> long only</label>
      </div>
      <div class="p-muted small">Over ${esc(range?.over || '–')}${range ? `: ${when(range.from)} → ${when(range.to)}` : ''} · ${esc(state.period ? `${state.period.span} ${state.period.type}` : '')} bars · backtest, not orders</div>
      ${S.error ? `<div class="sch-error">${esc(S.error)}</div>` : ''}
      ${S.result && !r ? '<div class="p-muted">The chart has moved to another level or symbol: run again.</div>' : ''}
      ${r ? `
        <div class="p-sub">${esc(r.over || '')} · ${r.stats.bars?.toLocaleString?.() ?? 0} bars, ${r.warmup} before them to warm up · ${esc(r.view)} data${S.shownTo != null ? ' · <b>replay</b>: as far as it has played' : ''}</div>
        ${kv(statsRows(r.stats, trades))}
        <div class="table-wrap sch-trades"><table class="act-table"><thead><tr><th></th><th>Entry</th><th class="num">Price</th><th>Exit</th><th class="num">Price</th><th class="num">Return</th><th>Why</th></tr></thead>
        <tbody>${trades.slice().reverse().map((t) => `<tr data-time="${t.entryTime}" title="${esc(t.openedBy || '')}">
          <td class="${t.side === 'long' ? 'up' : 'down'}">${t.side === 'long' ? '▲' : '▼'}</td>
          <td>${when(t.entryTime)}</td><td class="num">${priceText(t.entry)}</td>
          <td>${t.shownExit ? when(t.exitTime) : '<i>open</i>'}</td><td class="num">${t.shownExit ? priceText(t.exit) : '–'}</td>
          <td class="num ${t.shownExit ? cls(t.ret) : ''}">${t.shownExit ? fmtPct(t.ret) : '–'}</td>
          <td class="small">${t.shownExit ? esc(t.why) : ''}</td></tr>`).join('')}</tbody></table></div>` : ''}`;
  }
  S.render = render;

  $('sidePanel').addEventListener('click', (e) => {
    if (e.target.id === 'schRun') run();
    else if (e.target.id === 'schClear') clear();
    const row = e.target.closest('#pSchemes tr[data-time]');
    if (row) chart.scrollToTimestamp(Number(row.dataset.time), 200);
  });
  $('sidePanel').addEventListener('change', (e) => {
    const t = e.target;
    if (!t.closest('#pSchemes')) return;
    if (t.id === 'schKey') { cfg.key = t.value; cfg.params = {}; }
    else if (t.dataset.param) cfg.params[t.dataset.param] = t.type === 'number' ? Number(t.value) : t.value;
    else if (t.id === 'schFee') cfg.fee = Number(t.value) || 0;
    else if (t.id === 'schSlip') cfg.slippage = Number(t.value) || 0;
    else if (t.id === 'schLong') cfg.longOnly = t.checked;
    save();
    if (t.id === 'schKey') render();
  });
  $('sidePanel').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target.closest?.('#pSchemes')) run();
  });

  S.run = run;
  S.clear = clear;
  let renderFrame = 0;
  const schedule = () => { if (!renderFrame) renderFrame = requestAnimationFrame(() => { renderFrame = 0; render(); }); };
  S.reveal = (shownTo, formingAt) => {  // the replay's clock moved: drawn on the next frame, once
    const closed = shownTo !== S.shownTo;
    S.shownTo = shownTo; S.formingAt = formingAt;
    if (closed || formingAt == null) schedule();
  };
  chart.subscribeAction('onVisibleRangeChange', () => { if (!S.result) schedule(); });  // "over" follows the view
  window.addEventListener('massive:view', (e) => { if (e.detail.pane === 'analysis') render(); });
  window.addEventListener('massive:symbol', () => { if (S.result && S.result.ticker !== state.ticker) clear(); });

  api('/api/schemes').then((res) => { S.list = res.schemes; render(); }).catch((err) => { S.error = err.message; render(); });
})();
