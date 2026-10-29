'use strict';
/* Time after the last candle, and session annotations.

   KLineChart extends its time axis past the last bar by adding whole intervals (last + n x 5 min),
   straight through nights, weekends and holidays. Here the chart's store maps every slot past the
   last bar to the start of the bar that will really be there: the server projects them with the
   trading calendar through the same binning and level-of-detail grouping as real bars
   (/api/future). So axis labels, the crosshair time, drawings and the highlight's duration in
   the future area follow real sessions, at whatever interval and zoom is on screen, as far right
   as you scroll. Projected times are fetched ahead of the visible range in pages.

   Sessions are annotated on intraday charts: a dashed line where each session opens, shaded
   pre-market (blue) and after-hours (grey) (US stocks with extended hours on), overnight (violet:
   with all four sessions on, 20:00-04:00 on Blue Ocean), and a band along the top of the current
   (or next) session. */
(() => {
  const store = chart._chartStore;
  if (!store || typeof store.dataIndexToTimestamp !== 'function') return;  // library changed: keep its default
  const libToTimestamp = store.dataIndexToTimestamp.bind(store);
  const libToIndex = store.timestampToDataIndex.bind(store);
  const PAGE_SLOTS = 2000;

  const fut = { key: null, stamps: [], pending: null, exhausted: false, lastBar: null, ask: 0 };

  function lastBar() {
    const list = store.getDataList();
    return list.length ? list[list.length - 1] : null;
  }

  // What the projection is for. Not the last bar's time: each new bar of a live chart asked for the
  // projection again, page by page; the projection moves on instead (current).
  function futureKey(last) {
    if (!last || !state.ticker) return null;
    return [state.source, state.ticker, state.period.type, state.period.span, state.session, lodFor(),
            state.chartType, state.adjust, JSON.stringify(typeQuery()), JSON.stringify(modelQuery())].join('|');
  }

  function reset(key, last) {
    fut.key = key; fut.stamps = []; fut.pending = null; fut.exhausted = false; fut.lastBar = last;
    fut.ask += 1;  // a page on its way was for what is gone
  }

  function current() {
    const last = lastBar();
    const key = futureKey(last);
    if (key !== fut.key) {
      reset(key, last);
    } else if (last && last.timestamp !== fut.lastBar?.timestamp) {
      // A new bar took the first projected slot (or slots): the rest move up. A bar that was not
      // projected (the chart loaded again elsewhere, a gap) starts the projection again.
      const k = fut.stamps.indexOf(last.timestamp);
      if (k < 0) { reset(key, last); return last; }
      fut.stamps = fut.stamps.slice(k + 1);
      fut.lastBar = last;
      fut.pending = null;
      fut.ask += 1;  // a page on its way counted its offset from the bar before
    }
    return last;
  }

  // Make sure projected times exist for the first `n` future slots (async; redraws when they arrive).
  function ensure(n) {
    if (!Number.isFinite(lodFor())) return;
    const last = current();
    if (!last || fut.pending || fut.exhausted || fut.stamps.length >= n) return;
    const ask = fut.ask;
    const count = Math.max(PAGE_SLOTS, Math.ceil((n - fut.stamps.length) / PAGE_SLOTS) * PAGE_SLOTS);
    fut.pending = api('/api/future', {
      source: state.source, ticker: state.ticker, period: state.period.type, span: state.period.span,
      session: state.session, adjust: state.adjust, lod: lodFor(), offset: fut.stamps.length, count,
      ...typeQuery(), ...modelQuery(),
    }).then((res) => {
      if (fut.ask !== ask) return;
      fut.stamps.push(...res.timestamps);
      if (res.timestamps.length < count) fut.exhausted = true;  // not projectable (e.g. Renko) or the end of time
    }).catch(() => { if (fut.ask === ask) fut.exhausted = true; })
      .finally(() => { if (fut.ask === ask) fut.pending = null; redraw(); });
  }

  let redrawQueued = false;
  function redraw() {
    if (redrawQueued) return;
    redrawQueued = true;
    requestAnimationFrame(() => {
      redrawQueued = false;
      // Rebuild the axis ticks and overlays with the new times (cheap: no data change). Only a
      // layout: setting the right offset again would scroll back to the newest bar (the library
      // reads a chart scrolled into history as offset 0).
      chart.resize();
    });
  }

  // While projected times are loading, step from the last known one by whole intervals (the
  // library's estimate); the real times replace it a moment later.
  function estimate(fromTs, steps) {
    const p = state.period;
    const unit = { second: 1e3, minute: 6e4, hour: 36e5, day: 864e5, week: 6048e5, month: 2592e6 }[p.type] || 6e4;
    return fromTs + steps * unit * p.span * Math.max(1, lodFor());
  }

  store.dataIndexToTimestamp = function (index) {
    const n = store.getDataList().length;
    if (!n || index <= n - 1 || fut.exhausted && !fut.stamps.length) return libToTimestamp(index);
    const last = current();
    const slot = index - n;  // 0 = the first slot after the last bar
    if (slot < fut.stamps.length) return fut.stamps[slot];
    ensure(slot + 1 + PAGE_SLOTS);
    const base = fut.stamps.length ? fut.stamps[fut.stamps.length - 1] : last.timestamp;
    return estimate(base, slot - fut.stamps.length + 1);
  };

  store.timestampToDataIndex = function (ts) {
    const n = store.getDataList().length;
    const last = n ? store.getDataList()[n - 1] : null;
    if (!last || ts <= last.timestamp || fut.exhausted && !fut.stamps.length) return libToIndex(ts);
    current();
    const s = fut.stamps;
    if (!s.length || ts < s[0]) { if (!s.length) ensure(PAGE_SLOTS); return n - 1; }  // inside the last bar
    if (ts > s[s.length - 1]) {
      ensure(s.length + PAGE_SLOTS);
      const step = estimate(0, 1);
      return n + s.length - 1 + Math.floor((ts - s[s.length - 1]) / step);
    }
    let lo = 0, hi = s.length - 1;  // the last slot starting at or before ts
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (s[mid] <= ts) lo = mid; else hi = mid - 1;
    }
    return n + lo;
  };

  // Scroll right without limit: the chart keeps at least one bar of data on screen on the left.
  if (typeof chart.setMaxOffsetRightDistance === 'function') {
    chart.setMaxOffsetRightDistance(Number.MAX_SAFE_INTEGER / 4);
    chart.setMaxOffsetLeftDistance(Math.max(80, chartWidth() - 40));
  }

  // Keep projected times ahead of what is on screen.
  function prefetch() {
    const { to } = chart.getVisibleRange();
    const n = store.getDataList().length;
    if (n && to > n) ensure(to - n + PAGE_SLOTS);
    else if (n) ensure(PAGE_SLOTS);
    loadSessions();
  }
  chart.subscribeAction('onScroll', prefetch);
  chart.subscribeAction('onZoom', prefetch);
  chart.subscribeAction('onVisibleRangeChange', prefetch);

  // -- sessions -----------------------------------------------------------------------
  const sess = { key: null, from: 0, to: 0, list: [], pending: null, failed: null };
  const CALENDAR_DAYS = 3600;  // the most the server answers in one request (3700) less a margin
  const CALENDAR_RETRY_MS = 30_000;  // a window that failed is not asked for again before this

  function loadSessions() {
    if (!state.ticker || ['day', 'week', 'month'].includes(state.period.type)) return;
    const { from, to } = chart.getVisibleRange();
    const n = store.getDataList().length;
    if (!n) return;
    const a = store.dataIndexToTimestamp(Math.max(0, from));
    const b = store.dataIndexToTimestamp(Math.max(from + 1, to));
    if (!Number.isFinite(a) || !Number.isFinite(b)) return;
    const key = `${state.source}|${state.ticker}|${state.session}`;  // all four sessions: each from 20:00 the evening before
    // The visible range with a screen's width either side, at most CALENDAR_DAYS: on a wider
    // range, the newest part of it.
    const span = Math.max(b - a, 864e5), most = CALENDAR_DAYS * 864e5;
    let qFrom = a - span, qTo = b + span;
    if (qTo - qFrom > most) { qTo = Math.min(qTo, Math.max(b, a + most)); qFrom = qTo - most; }
    if (sess.key === key && Math.max(a, qFrom) >= sess.from && Math.min(b, qTo) <= sess.to) return;
    if (sess.pending) return;
    const want = `${key}|${Math.round(qFrom / 864e5)}|${Math.round(qTo / 864e5)}`;
    if (sess.failed?.want === want && Date.now() - sess.failed.at < CALENDAR_RETRY_MS) return;
    sess.pending = api('/api/calendar', { source: state.source, ticker: state.ticker, session: state.session,
      from: Math.round(qFrom), to: Math.round(qTo) })
      .then((res) => {
        sess.key = key; sess.from = qFrom; sess.to = qTo; sess.list = res.sessions; sess.asset = res.asset; sess.failed = null;
        redraw();
      })
      .catch(() => { sess.failed = { want, at: Date.now() }; })  // no redraw: it would only ask again
      .finally(() => { sess.pending = null; });
  }
  state.sessions = sess;

  function currentSession(list, now) {
    return list.find((s) => s.open <= now && now < s.close) || list.find((s) => s.open > now) || null;
  }

  kc.registerIndicator({
    name: 'SESSIONS',
    shortName: '',
    series: 'price',
    figures: [],
    calc: (list) => list.map(() => ({})),
    createTooltipDataSource: () => ({ name: '', calcParamsText: '', features: [], legends: [] }),
    draw: ({ ctx, chart: c, bounding, xAxis }) => {
      if (['day', 'week', 'month'].includes(state.period.type) || !sess.list.length) return true;
      const { from, to } = c.getVisibleRange();
      const t0 = store.dataIndexToTimestamp(Math.max(0, from - 1));
      const t1 = store.dataIndexToTimestamp(to + 1);
      const xOf = (ts) => xAxis.convertToPixel(store.timestampToDataIndex(ts)) - c.getBarSpace().halfBar;
      const dark = !document.body.classList.contains('light');
      // pre-market a cool blue, after-hours grey: one shade for both made them hard to tell apart. The
      // blue and the violet at half the strength they had (the user, 2026-10-28: "a little too bright"),
      // each then scaled by its Preferences slider (prefs.js: 0-200 %, 100 standard)
      const k = (name) => Math.min(2, Math.max(0, (state.shade?.[name] ?? 100) / 100));
      const preShade = dark ? `rgba(10,32,96,${0.225 * k('pre')})` : `rgba(20,60,170,${0.065 * k('pre')})`;  // a deep navy
      const postShade = dark ? `rgba(120,123,134,${0.07 * k('post')})` : `rgba(120,123,134,${0.08 * k('post')})`;
      const nightShade = dark ? `rgba(90,40,140,${0.15 * k('night')})` : `rgba(110,60,170,${0.05 * k('night')})`;  // overnight: a violet
      const wide = state.session !== 'regular';  // extended hours or all four sessions
      const now = Date.now();
      const cur = currentSession(sess.list, now);
      ctx.save();
      for (const s of sess.list) {
        if (s.close < t0 || s.open > t1) continue;
        // Extended hours shading (US stocks with ETH on).
        if (s.regularOpen && wide) {
          const pre = s.extendedOpen || s.open;  // with the overnight: pre-market from 04:00
          for (const [a, b, shade] of [[s.open, s.extendedOpen || s.open, nightShade], [pre, s.regularOpen, preShade],
            [s.regularClose, s.close, postShade]]) {
            const xa = xOf(a), xb = xOf(b);
            if (xb > xa) { ctx.fillStyle = shade; ctx.fillRect(xa, 0, xb - xa, bounding.height); }
          }
        }
        // Session break.
        const x = Math.round(xOf(s.regularOpen && !wide ? s.regularOpen : s.open)) + 0.5;
        if (x > 0 && x < bounding.width) {
          ctx.strokeStyle = dark ? 'rgba(120,123,134,0.35)' : 'rgba(120,123,134,0.45)';
          ctx.setLineDash([3, 4]);
          ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, bounding.height); ctx.stroke();
        }
      }
      if (cur && cur.close >= t0 && cur.open <= t1) {  // current (or next) session: a band along the top
        const a = xOf(wide || !cur.regularOpen ? cur.open : cur.regularOpen);
        const b = xOf(wide || !cur.regularClose ? cur.close : cur.regularClose);
        const live = cur.open <= now;
        ctx.setLineDash([]);
        ctx.fillStyle = live ? 'rgba(41,98,255,0.55)' : 'rgba(41,98,255,0.25)';
        ctx.fillRect(Math.max(0, a), 0, Math.max(2, Math.min(bounding.width, b) - Math.max(0, a)), 3);
      }
      ctx.restore();
      return true;
    },
  });
  chart.createIndicator({ name: 'SESSIONS', paneId: 'candle_pane' }, true);
  window.TimeAxis = { future: fut, sessions: sess, ensure, currentSession, redraw };
})();
